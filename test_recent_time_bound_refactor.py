#!/usr/bin/env python3
"""recent --since/--until 时间参数校验局部重构的兼容性回归测试。

重构把 --since 与 --until 各自一份、几乎相同的缺值处理与错误封装合并为
同一个校验入口（validate_time_bound_filter），两个缺值哨兵也合并为一个。
本文件通过公开命令行入口（子进程运行 healthcheck.py）核对重构对外行为
与重构前完全等价：

验收固定样本（同一目标的两条记录）：
- 目标：http://127.0.0.1:8765/
- id 1：checked_at 2026-10-04T00:00:02Z，耗时 0ms，success/200/ok；
- id 2：checked_at 2026-10-04T00:00:02.000000+00:00（与 id 1 同一时刻的
  另一种写法），耗时 10ms，failure/null/timeout。

覆盖点：
- 验收主用例：以上述两个值分别作为 --since/--until 并 --limit 1，普通
  查询只返回 id 2 的原始七字段记录，退出码 0，时间字符串原样保留；
- Z 与 +00:00、零小数与省略小数按同一时刻比较；省略任一边界表示不设
  该边界，单用按对应方向筛选；同用为两端都包含的闭合窗口，起止相等合法；
- 非法取值矩阵（裸参数缺值、空字符串、前后空白、缺时区、非 UTC 偏移、
  七位小数、不存在的日期与时间）对两个参数都以退出码 2 结束、stdout
  为空，stderr 保留参数名、具体原因与取值或缺值说明、无回溯；
- 两边界同时非法时先报告 --since；时间参数错误（含起晚于终）优先于
  目录数据库路径错误；既有的非时间参数错误优先级保持不变；
- 筛选后按 id 倒序限量、两种摘要统计同一结果集合、合法无命中输出；
- 查询不发网络请求、不创建文件或目录、不改动已有数据与表结构；
- check 不接受 --since/--until（时间参数仍只属于 recent）。

在项目目录执行：
    python3 -m unittest test_recent_time_bound_refactor
或：
    python3 test_recent_time_bound_refactor.py
"""

import http.server
import json
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "healthcheck.py"

# 与 healthcheck.py 中 checks 表一致的结构（兼容性基准）
SCHEMA_SQL = """
CREATE TABLE checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    elapsed_ms INTEGER NOT NULL CHECK (elapsed_ms >= 0),
    status TEXT NOT NULL CHECK (status IN ('success', 'failure')),
    http_status INTEGER,
    reason TEXT NOT NULL CHECK (reason IN
        ('ok', 'http_status', 'connection_error', 'timeout'))
)
"""

COLUMNS = ["id", "url", "checked_at", "elapsed_ms",
           "status", "http_status", "reason"]

URL_A = "http://127.0.0.1:8765/"

T_Z = "2026-10-04T00:00:02Z"
T_OFFSET = "2026-10-04T00:00:02.000000+00:00"
T_OFFSET_ZERO_3 = "2026-10-04T00:00:02.000Z"
# 同一时刻推进一微秒：两条记录都早于它
T_ONE_MICRO_LATER = "2026-10-04T00:00:02.000001Z"
# 同一时刻回退一微秒：两条记录都晚于它
T_ONE_MICRO_EARLIER = "2026-10-04T00:00:01.999999Z"

REC1 = {
    "id": 1, "url": URL_A, "checked_at": T_Z,
    "elapsed_ms": 0, "status": "success", "http_status": 200, "reason": "ok",
}
REC2 = {
    "id": 2, "url": URL_A, "checked_at": T_OFFSET,
    "elapsed_ms": 10, "status": "failure", "http_status": None,
    "reason": "timeout",
}
ALL_RECORDS = [REC1, REC2]

EMPTY_SUMMARY = {
    "count": 0,
    "min_elapsed_ms": None,
    "max_elapsed_ms": None,
    "avg_elapsed_ms": None,
}
EMPTY_STATUS_SUMMARY = {"count": 0, "success_count": 0, "failure_count": 0}

# 非法时间取值：(取值, stderr 应包含的原因关键词)
INVALID_VALUE_CASES = [
    ("", "不能为空"),
    (" 2026-10-04T00:00:02Z ", "前后不允许有空白"),
    ("2026-10-04T00:00:02", "缺少时区"),
    ("2026-10-04T00:00:02+08:00", "仅接受 UTC"),
    ("2026-10-04T00:00:02-05:30", "仅接受 UTC"),
    ("2026-10-04T00:00:02.0000000Z", "格式"),
    ("2026-02-30T00:00:02Z", "真实存在"),
    ("2026-10-04T25:00:02Z", "真实存在"),
    ("2026-10-04T00:60:02Z", "真实存在"),
]

# 裸参数缺值的完整错误文案（逐字锁定）
EXPECTED_BARE_MESSAGES = {
    "since": (
        "healthcheck: error: since 参数错误：--since 必须提供值，格式为 "
        "YYYY-MM-DDTHH:MM:SS（秒后可带一至六位小数）"
        "并以 Z 或 +00:00 结尾，实际缺少值"
    ),
    "until": (
        "healthcheck: error: until 参数错误：--until 必须提供值，格式为 "
        "YYYY-MM-DDTHH:MM:SS（秒后可带一至六位小数）"
        "并以 Z 或 +00:00 结尾，实际缺少值"
    ),
}


def run_cli(db_path, *extra):
    """以子进程运行 healthcheck.py 命令行，返回 CompletedProcess。"""
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


def run_recent(db_path, *extra):
    return run_cli(db_path, "recent", *extra)


def build_sample_db(path, records=None):
    """创建包含验收固定样本（默认两条）的临时数据库。"""
    records = ALL_RECORDS if records is None else records
    conn = sqlite3.connect(str(path))
    conn.execute(SCHEMA_SQL)
    conn.executemany(
        "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
        "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (r["id"], r["url"], r["checked_at"], r["elapsed_ms"],
             r["status"], r["http_status"], r["reason"])
            for r in records
        ],
    )
    conn.commit()
    conn.close()


def snapshot_state(tmpdir, db_path):
    """捕获目录文件集合与数据库内所有表的全部行，供前后比对。"""
    files = {
        str(p.relative_to(tmpdir))
        for p in tmpdir.rglob("*") if p.is_file()
    }
    tables = {}
    if pathlib.Path(db_path).is_file():
        conn = sqlite3.connect(str(db_path))
        try:
            names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
            for name in names:
                tables[name] = conn.execute(
                    f"SELECT * FROM {name}"
                ).fetchall()
        finally:
            conn.close()
    return files, tables


class RecentTimeBoundRefactorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-timebound-"))
        self.db = self.tmp / "monitor.sqlite"
        build_sample_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_state_unchanged(self, before, label, db_path=None):
        """查询不得改变目录文件集合、表结构或任何已有记录。"""
        db_path = self.db if db_path is None else pathlib.Path(db_path)
        after = snapshot_state(self.tmp, db_path)
        self.assertEqual(
            after[0], before[0],
            f"{label}: 查询前后目录文件集合发生变化："
            f"新增 {sorted(after[0] - before[0])}，"
            f"消失 {sorted(before[0] - after[0])}",
        )
        self.assertEqual(
            after[1], before[1],
            f"{label}: 查询前后数据库表或记录内容发生变化",
        )

    def assert_ok_empty_stderr(self, proc, label):
        self.assertEqual(
            proc.returncode, 0,
            f"输入 {label}：期望退出码 0，实际 {proc.returncode}，"
            f"stderr={proc.stderr!r}",
        )
        self.assertEqual(
            proc.stderr, "",
            f"输入 {label}：期望 stderr 为空，实际 {proc.stderr!r}",
        )

    def assert_rejected(self, proc, label, name, *keywords):
        """退出码 2、stdout 为空、stderr 指出对应参数名与原因、无回溯。"""
        self.assertEqual(
            proc.returncode, 2,
            f"输入 {label}：期望退出码 2，实际 {proc.returncode}，"
            f"stdout={proc.stdout!r}",
        )
        self.assertEqual(
            proc.stdout, "",
            f"输入 {label}：期望 stdout 为空，实际 {proc.stdout!r}",
        )
        self.assertTrue(
            proc.stderr.startswith("healthcheck: error:"),
            f"输入 {label}：stderr 应以固定错误前缀开头，"
            f"实际 {proc.stderr!r}",
        )
        self.assertIn(
            f"{name} 参数错误", proc.stderr,
            f"输入 {label}：stderr 应指出参数 {name}，实际 {proc.stderr!r}",
        )
        self.assertIn(
            f"--{name}", proc.stderr,
            f"输入 {label}：stderr 应保留参数名 --{name}，"
            f"实际 {proc.stderr!r}",
        )
        for keyword in keywords:
            self.assertIn(
                keyword, proc.stderr,
                f"输入 {label}：stderr 应说明原因（含 {keyword!r}），"
                f"实际 {proc.stderr!r}",
            )
        self.assertNotIn(
            "Traceback", proc.stderr,
            f"输入 {label}：stderr 不应包含 Python 回溯，"
            f"实际 {proc.stderr!r}",
        )

    # ---- 验收主用例：两条同刻记录为闭合窗口、limit 1，仅返回 id 2 ----

    def test_acceptance_closed_window_limit_1_returns_id_2_full_record(self):
        label = (f"recent --since {T_Z} --until {T_OFFSET} --limit 1"
                 "（验收固定样本）")
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, "--since", T_Z, "--until", T_OFFSET, "--limit", "1",
        )
        self.assert_ok_empty_stderr(proc, label)
        records = json.loads(proc.stdout)
        self.assertEqual(
            [r["id"] for r in records], [2],
            f"输入 {label}：同刻两条记录按 id 倒序限量 1 应只剩 id 2，"
            f"实际 {[r['id'] for r in records]}",
        )
        self.assertEqual(
            records, [REC2],
            f"输入 {label}：应返回 id 2 的原始七字段记录，实际 {records}",
        )
        record = records[0]
        self.assertEqual(set(record.keys()), set(COLUMNS))
        self.assertEqual(list(record.keys()), COLUMNS)
        # 时间字符串必须原样保留，不统一改写
        self.assertEqual(record["checked_at"], T_OFFSET)
        self.assert_state_unchanged(before, label)

    def test_acceptance_window_without_limit_returns_id_2_then_1(self):
        # 同一闭合窗口不限量：两条都命中，顺序 [2, 1]，各自原始字符串保留
        label = "recent --since/--until 同刻闭合窗口（默认 limit）"
        proc = run_recent(self.db, "--since", T_Z, "--until", T_OFFSET)
        self.assert_ok_empty_stderr(proc, label)
        records = json.loads(proc.stdout)
        self.assertEqual(records, [REC2, REC1])
        self.assertEqual(records[0]["checked_at"], T_OFFSET)
        self.assertEqual(records[1]["checked_at"], T_Z)

    def test_closed_window_summaries_count_same_result_set(self):
        # 摘要必须与普通查询统计同一批「窗口 + limit 1」结果（仅 id 2）
        label = "窗口 limit 1 的耗时摘要"
        proc = run_recent(
            self.db, "--summary",
            "--since", T_Z, "--until", T_OFFSET, "--limit", "1",
        )
        self.assert_ok_empty_stderr(proc, label)
        self.assertEqual(
            json.loads(proc.stdout),
            {"count": 1, "min_elapsed_ms": 10,
             "max_elapsed_ms": 10, "avg_elapsed_ms": 10.0},
        )
        proc_st = run_recent(
            self.db, "--status-summary",
            "--since", T_Z, "--until", T_OFFSET, "--limit", "1",
        )
        self.assert_ok_empty_stderr(proc_st, "窗口 limit 1 的状态摘要")
        self.assertEqual(
            json.loads(proc_st.stdout),
            {"count": 1, "success_count": 0, "failure_count": 1},
        )
        # 不限量时摘要覆盖同一窗口的两条记录（id 2 failure、id 1 success）
        proc_all = run_recent(
            self.db, "--status-summary", "--since", T_Z, "--until", T_OFFSET,
        )
        self.assert_ok_empty_stderr(proc_all, "窗口两条的状态摘要")
        self.assertEqual(
            json.loads(proc_all.stdout),
            {"count": 2, "success_count": 1, "failure_count": 1},
        )

    # ---- 两种 UTC 写法、零小数与省略小数按同一时刻处理 ----

    def test_equivalent_instant_forms_same_window(self):
        forms = (T_Z, T_OFFSET, T_OFFSET_ZERO_3)
        for since_value in forms:
            for until_value in forms:
                with self.subTest(since=since_value, until=until_value):
                    proc = run_recent(
                        self.db,
                        "--since", since_value, "--until", until_value,
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(
                        json.loads(proc.stdout), [REC2, REC1],
                        f"{since_value} 与 {until_value} 应按同一时刻处理",
                    )

    def test_equal_start_and_end_is_valid_inclusive(self):
        label = "起止相等（跨写法）为只含同一时刻的合法窗口"
        proc = run_recent(self.db, "--since", T_OFFSET, "--until", T_Z)
        self.assert_ok_empty_stderr(proc, label)
        self.assertEqual(json.loads(proc.stdout), [REC2, REC1])

    # ---- 省略任一边界：单用按对应方向筛选 ----

    def test_since_alone_filters_lower_bound_direction(self):
        # 下界等于记录时刻：两条都保留（>=，端点包含）
        proc = run_recent(self.db, "--since", T_OFFSET_ZERO_3)
        self.assert_ok_empty_stderr(proc, "since 单用=记录时刻")
        self.assertEqual(json.loads(proc.stdout), [REC2, REC1])
        # 下界晚一微秒：两条都早于它，合法无命中
        proc = run_recent(self.db, "--since", T_ONE_MICRO_LATER)
        self.assert_ok_empty_stderr(proc, "since 单用晚一微秒")
        self.assertEqual(proc.stdout, "[]\n")
        # 下界早一微秒：两条都不早于它
        proc = run_recent(self.db, "--since", T_ONE_MICRO_EARLIER)
        self.assert_ok_empty_stderr(proc, "since 单用早一微秒")
        self.assertEqual(json.loads(proc.stdout), [REC2, REC1])

    def test_until_alone_filters_upper_bound_direction(self):
        # 上界等于记录时刻：两条都保留（<=，端点包含）
        proc = run_recent(self.db, "--until", T_Z)
        self.assert_ok_empty_stderr(proc, "until 单用=记录时刻")
        self.assertEqual(json.loads(proc.stdout), [REC2, REC1])
        # 上界早一微秒：两条都晚于它，合法无命中
        proc = run_recent(self.db, "--until", T_ONE_MICRO_EARLIER)
        self.assert_ok_empty_stderr(proc, "until 单用早一微秒")
        self.assertEqual(proc.stdout, "[]\n")
        # 上界晚一微秒：两条都不晚于它
        proc = run_recent(self.db, "--until", T_ONE_MICRO_LATER)
        self.assert_ok_empty_stderr(proc, "until 单用晚一微秒")
        self.assertEqual(json.loads(proc.stdout), [REC2, REC1])

    def test_omit_both_bounds_keeps_plain_query_behavior(self):
        # 省略两个时间参数：不做任何时间过滤，仍按 id 倒序限量
        proc = run_recent(self.db, "--limit", "1")
        self.assert_ok_empty_stderr(proc, "省略两边界 limit 1")
        self.assertEqual(json.loads(proc.stdout), [REC2])

    def test_time_window_intersects_other_filters(self):
        # 窗口内再按 status=success 取交集：只剩 id 1
        proc = run_recent(
            self.db, "--status", "success",
            "--since", T_Z, "--until", T_OFFSET,
        )
        self.assert_ok_empty_stderr(proc, "窗口与 status 取交集")
        self.assertEqual(json.loads(proc.stdout), [REC1])

    # ---- 合法无命中：普通 []、耗时摘要三 null、状态摘要三 0 ----

    def test_legal_no_match_outputs_for_three_modes(self):
        for args, payload, expected in (
            ((), "[]\n", None),
            (("--summary",), None, EMPTY_SUMMARY),
            (("--status-summary",), None, EMPTY_STATUS_SUMMARY),
        ):
            with self.subTest(args=args):
                proc = run_recent(
                    self.db, *args,
                    "--since", T_ONE_MICRO_LATER, "--until", T_ONE_MICRO_LATER,
                )
                self.assert_ok_empty_stderr(proc, f"合法无命中 {args}")
                if expected is None:
                    self.assertEqual(proc.stdout, payload)
                else:
                    self.assertEqual(json.loads(proc.stdout), expected)

    # ---- 起点晚于终点：退出码 2、空 stdout、原有时间范围错误 ----

    def test_since_later_than_until_rejected_as_time_range_error(self):
        label = "since 晚于 until"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db,
            "--since", "2026-10-04T00:00:03Z",
            "--until", "2026-10-04T00:00:01Z",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("时间范围参数错误", proc.stderr)
        self.assertIn("--since", proc.stderr)
        self.assertIn("--until", proc.stderr)
        self.assertIn("晚于", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assert_state_unchanged(before, label)

    def test_time_range_error_precedes_directory_db(self):
        # 两边界各自合法但起晚于终，且数据库路径是目录：
        # 时间范围错误仍先于目录数据库路径错误
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir()
        before = snapshot_state(self.tmp, db_dir)
        proc = run_recent(
            db_dir,
            "--since", "2026-10-04T00:00:03Z",
            "--until", "2026-10-04T00:00:01Z",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("时间范围参数错误", proc.stderr)
        self.assertNotIn("目录", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assert_state_unchanged(before, "起晚于终+目录库", db_dir)

    # ---- 非法取值矩阵：两个参数共享同一套拒绝规则 ----

    def test_bare_missing_value_rejected_for_both_params(self):
        for name in ("since", "until"):
            with self.subTest(param=name):
                # 裸参数会吞掉后续参数，置于末尾
                label = f"裸 --{name}（缺值）"
                before = snapshot_state(self.tmp, self.db)
                proc = run_recent(self.db, "--url", URL_A, f"--{name}")
                self.assertEqual(proc.returncode, 2, label)
                self.assertEqual(proc.stdout, "")
                self.assertEqual(
                    proc.stderr.strip(), EXPECTED_BARE_MESSAGES[name],
                    f"输入 {label}：缺值错误文案应逐字保持",
                )
                self.assertNotIn("Traceback", proc.stderr)
                self.assert_state_unchanged(before, label)

    def test_invalid_values_rejected_for_both_params(self):
        for name in ("since", "until"):
            for value, keyword in INVALID_VALUE_CASES:
                with self.subTest(param=name, value=value):
                    label = f"--{name} {value!r}"
                    before = snapshot_state(self.tmp, self.db)
                    proc = run_recent(self.db, f"--{name}", value)
                    self.assert_rejected(proc, label, name, keyword)
                    # 非缺值错误必须回显实际取值
                    self.assertIn(
                        repr(value), proc.stderr,
                        f"输入 {label}：stderr 应回显取值 {value!r}",
                    )
                    self.assert_state_unchanged(before, label)

    def test_both_bounds_invalid_reports_since_first(self):
        # 两边界同时非法：先报告 --since，不出现 until 的报错
        label = "since 与 until 同时非法"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, "--since", "bad-since", "--until", "worse-until",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("since 参数错误", proc.stderr)
        self.assertIn("'bad-since'", proc.stderr)
        self.assertNotIn("until 参数错误", proc.stderr)
        self.assertNotIn("worse-until", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assert_state_unchanged(before, label)

        # 缺时区这一具体原因同样先报 since
        proc2 = run_recent(
            self.db,
            "--since", "2026-10-04T00:00:02",
            "--until", "2026-10-04T00:00:03",
        )
        self.assertEqual(proc2.returncode, 2)
        self.assertEqual(proc2.stdout, "")
        self.assertIn("since 参数错误", proc2.stderr)
        self.assertIn("缺少时区", proc2.stderr)
        self.assertNotIn("until 参数错误", proc2.stderr)

    def test_invalid_time_bound_precedes_directory_db(self):
        # 非法时间参数 + 目录数据库路径：时间错误优先，不报告目录
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir()
        cases = (
            ("--since", "2026-10-04T00:00:02", "since", "缺少时区"),
            ("--until", "2026-10-04T00:00:02+08:00", "until", "仅接受 UTC"),
        )
        for flag, value, name, keyword in cases:
            with self.subTest(param=name):
                before = snapshot_state(self.tmp, db_dir)
                proc = run_recent(db_dir, flag, value)
                self.assert_rejected(
                    proc, f"非法 {name} + 目录库", name, keyword,
                )
                self.assertNotIn("目录", proc.stderr)
                self.assert_state_unchanged(
                    before, f"非法 {name} + 目录库", db_dir,
                )

    # ---- 既有的非时间参数错误优先级保持不变 ----

    def test_non_time_error_precedence_unchanged(self):
        db_dir = self.tmp / "order-dir"
        db_dir.mkdir()

        # 非法 status 先于非法 since 与目录
        proc = run_recent(
            db_dir, "--status", "nope", "--since", "bad",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("status 参数错误", proc.stderr)
        self.assertNotIn("since 参数错误", proc.stderr)
        self.assertNotIn("目录", proc.stderr)

        # 非法 URL 先于非法 until 与目录
        proc = run_recent(
            db_dir, "--url", "https://127.0.0.1:8765/x",
            "--until", "bad",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("仅接受 http 协议", proc.stderr)
        self.assertNotIn("until 参数错误", proc.stderr)
        self.assertNotIn("目录", proc.stderr)

        # argparse 阶段的非法 limit 先于一切自定义时间校验
        proc = run_recent(self.db, "--limit", "0", "--since", "bad")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("limit", proc.stderr)
        self.assertNotIn("since 参数错误", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

        # 摘要互斥先于时间校验
        proc = run_recent(
            self.db, "--summary", "--status-summary", "--since", "bad",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("互斥", proc.stderr)
        self.assertNotIn("since 参数错误", proc.stderr)

        # 时间参数合法后，目录路径错误照常报告
        proc = run_recent(
            db_dir, "--since", T_Z, "--until", T_OFFSET,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("目录", proc.stderr)

    # ---- 缺库 / 表结构 / check 命令边界 ----

    def test_missing_db_with_time_filter_returns_empty_creates_nothing(self):
        missing = self.tmp / "nope" / "monitor.sqlite"
        before = snapshot_state(self.tmp, missing)
        proc = run_recent(missing, "--since", T_Z, "--until", T_OFFSET)
        self.assert_ok_empty_stderr(proc, "缺库 + 时间窗口")
        self.assertEqual(proc.stdout, "[]\n")
        self.assertFalse(missing.exists())
        self.assertFalse(missing.parent.exists())
        self.assert_state_unchanged(before, "缺库 + 时间窗口", missing)

    def test_schema_remains_compatible(self):
        proc = run_recent(
            self.db, "--since", T_Z, "--until", T_OFFSET, "--limit", "1",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        conn = sqlite3.connect(str(self.db))
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(checks)")]
        finally:
            conn.close()
        self.assertEqual(cols, COLUMNS)

    def test_check_does_not_accept_time_bound_args(self):
        # --since/--until 仍只属于 recent；check 的命令面保持原样
        for flag in ("--since", "--until"):
            with self.subTest(flag=flag):
                proc = run_cli(
                    self.tmp / "x.sqlite", "check",
                    "--url", URL_A, flag, T_Z,
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")
                self.assertIn("unrecognized arguments", proc.stderr)

    # ---- 查询不发网络请求 ----

    def test_recent_query_makes_no_network_request(self):
        hits = []

        class CountingHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        try:
            # 在记录 URL 的实际端口上起监听：recent 只查库，绝不应探测它
            server = http.server.ThreadingHTTPServer(
                ("127.0.0.1", 8765), CountingHandler
            )
        except OSError as exc:
            self.skipTest(f"无法绑定 127.0.0.1:8765 以核对网络静默：{exc}")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # 即便 --url 指向正在监听的本机服务，recent 也只查库不探测
            for extra in (
                ("--since", T_Z),
                ("--until", T_OFFSET),
                ("--since", T_Z, "--until", T_OFFSET, "--limit", "1"),
                ("--summary", "--since", T_Z, "--until", T_OFFSET),
            ):
                proc = run_recent(self.db, "--url", URL_A, *extra)
                self.assertEqual(proc.returncode, 0, proc.stderr)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(
            hits, [], f"recent 查询不应发出任何网络请求，实际收到 {hits}"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
