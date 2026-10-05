#!/usr/bin/env python3
"""recent 时间参数（--since/--until）校验局部重构的兼容性回归。

重构把 --since 与 --until 各自同构的「缺值哨兵 + 格式校验 + 错误封装」
合并为同一个哨兵 TIME_BOUND_MISSING 与唯一入口
validate_time_bound(value, name)，参数名称只注入错误信息。本测试只通过
公开命令行入口（子进程运行 healthcheck.py）核对对外行为与重构前完全
等价，不导入产品代码内部符号：

- 用户验收样本：同一目标两条记录，id 1 的 checked_at 为
  2026-10-04T00:00:02Z、id 2 为 2026-10-04T00:00:02.000000+00:00；
  以这两个值作为 --since/--until 边界并 --limit 1，普通查询只返回 id 2
  的原始七字段记录，退出码 0；两种 UTC 写法互换、零小数与省略小数按
  同一时刻处理，起止相等合法；
- 省略任一边界不设该边界：--since 单用按下界、--until 单用按上界筛选，
  同用为闭合窗口，筛选后按 id 倒序限量；--summary / --status-summary
  统计同一结果集合，合法无命中时输出 [] / count 0 摘要；
- 每个边界的缺值（裸参数）、空字符串、前后空白、缺时区、非 UTC 偏移、
  七位小数、不存在的日期与不存在的时间，均退出码 2、stdout 为空、
  stderr 保留对应参数名称（--since/--until）、具体原因及取值或缺值说明，
  且不出现回溯；
- 两边界同时非法时先报告 --since；时间参数错误优先于目录数据库路径
  错误；非时间参数的既有优先级（status → URL → reason → 时间）保持；
- 起点晚于终点仍按「时间范围参数错误」处理：退出码 2、stdout 为空；
- 查询不发网络请求、不创建文件或目录、不改动已有数据（前后快照比对）；
- check 命令不新增 --since/--until 入口。

在项目目录执行：
    python3 -m unittest test_recent_time_bound_refactor
或：
    python3 test_recent_time_bound_refactor.py
"""

import json
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "healthcheck.py"

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

URL_A = "http://127.0.0.1:8765/health"

T1_Z = "2026-10-04T00:00:02Z"
T1_OFFSET = "2026-10-04T00:00:02.000000+00:00"
T_EARLY = "2026-10-04T00:00:01Z"
T_LATE = "2026-10-04T00:00:04Z"

# 用户验收固定样本：同一目标仅两条记录，同一时刻的两种 UTC 写法
ACCEPT_REC1 = {
    "id": 1, "url": URL_A, "checked_at": T1_Z,
    "elapsed_ms": 0, "status": "failure", "http_status": None,
    "reason": "timeout",
}
ACCEPT_REC2 = {
    "id": 2, "url": URL_A, "checked_at": T1_OFFSET,
    "elapsed_ms": 10, "status": "failure", "http_status": None,
    "reason": "timeout",
}
ACCEPT_RECORDS = [ACCEPT_REC1, ACCEPT_REC2]

# 方向筛选的扩展样本：在验收两条之外补更早（id 4）与更晚（id 3）各一条
EXT_REC3 = {
    "id": 3, "url": URL_A, "checked_at": T_LATE,
    "elapsed_ms": 20, "status": "failure", "http_status": None,
    "reason": "timeout",
}
EXT_REC4 = {
    "id": 4, "url": URL_A, "checked_at": T_EARLY,
    "elapsed_ms": 30, "status": "failure", "http_status": None,
    "reason": "timeout",
}
EXTENDED_RECORDS = [ACCEPT_REC1, ACCEPT_REC2, EXT_REC3, EXT_REC4]

EMPTY_SUMMARY = {
    "count": 0,
    "min_elapsed_ms": None,
    "max_elapsed_ms": None,
    "avg_elapsed_ms": None,
}

# (参数名, 命令行选项)：两个边界共用同一套规则，故统一参数化
BOUND_ARGS = (("since", "--since"), ("until", "--until"))

# 每个边界的非法取值：(取值, stderr 应包含的原因关键词)
INVALID_VALUE_CASES = [
    ("", "不能为空"),
    (" 2026-10-04T00:00:02Z ", "前后不允许有空白"),
    ("2026-10-04T00:00:02", "缺少时区"),
    ("2026-10-04T00:00:02+08:00", "仅接受 UTC"),
    ("2026-10-04T00:00:02.0000000Z", "格式"),
    ("2026-02-30T00:00:02Z", "真实存在"),
    ("2026-10-04T25:00:02Z", "真实存在"),
]


def run_cli(db_path, *extra):
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


def run_recent(db_path, *extra):
    return run_cli(db_path, "recent", *extra)


def build_sample_db(path, records):
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


class TimeBoundRefactorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-timebound-"))
        self.accept_db = self.tmp / "accept.sqlite"
        build_sample_db(self.accept_db, ACCEPT_RECORDS)
        self.ext_db = self.tmp / "extended.sqlite"
        build_sample_db(self.ext_db, EXTENDED_RECORDS)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_state_unchanged(self, before, label, db_path=None):
        db_path = self.accept_db if db_path is None else pathlib.Path(db_path)
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

    def assert_ok(self, proc, label):
        self.assertEqual(
            proc.returncode, 0,
            f"输入 {label}：期望退出码 0，实际 {proc.returncode}，"
            f"stderr={proc.stderr!r}",
        )
        self.assertEqual(
            proc.stderr, "",
            f"输入 {label}：期望 stderr 为空，实际 {proc.stderr!r}",
        )

    def assert_time_error(self, proc, label, name, value):
        """退出码 2、stdout 为空、stderr 指出具体参数名/原因/取值、无回溯。"""
        self.assertEqual(
            proc.returncode, 2,
            f"输入 {label}：期望退出码 2，实际 {proc.returncode}，"
            f"stdout={proc.stdout!r}",
        )
        self.assertEqual(
            proc.stdout, "",
            f"输入 {label}：期望 stdout 为空，实际 {proc.stdout!r}",
        )
        self.assertIn(
            f"{name} 参数错误", proc.stderr,
            f"输入 {label}：stderr 应指出 {name}，实际 {proc.stderr!r}",
        )
        self.assertIn(
            f"--{name}", proc.stderr,
            f"输入 {label}：stderr 应回显 --{name}，实际 {proc.stderr!r}",
        )
        # 取值错误必须回显取值；缺值必须说明缺少值
        if value is None:
            self.assertIn("实际缺少值", proc.stderr)
        else:
            self.assertIn(
                repr(value), proc.stderr,
                f"输入 {label}：stderr 应回显实际值 {value!r}，"
                f"实际 {proc.stderr!r}",
            )
        self.assertNotIn(
            "Traceback", proc.stderr,
            f"输入 {label}：stderr 不应出现回溯，实际 {proc.stderr!r}",
        )

    # ---- 用户验收主场景：闭合窗口 + limit 1 只返回 id 2 原记录 ----

    def test_acceptance_closed_window_limit_1_returns_id_2(self):
        label = (f"recent --since {T1_Z} --until {T1_OFFSET} "
                 "--limit 1（验收主场景）")
        before = snapshot_state(self.tmp, self.accept_db)
        proc = run_recent(
            self.accept_db, "--url", URL_A,
            "--since", T1_Z, "--until", T1_OFFSET, "--limit", "1",
        )
        self.assert_ok(proc, label)
        records = json.loads(proc.stdout)
        self.assertEqual(
            records, [ACCEPT_REC2],
            f"输入 {label}：同一时刻两条记录按 id 倒序限量应只返回 id 2 "
            f"原始七字段记录，实际 {records}",
        )
        self.assertEqual(set(records[0].keys()), set(COLUMNS))
        # 时间字符串原样保留，不归一化
        self.assertEqual(records[0]["checked_at"], T1_OFFSET)
        self.assert_state_unchanged(before, label)

    def test_acceptance_window_utc_forms_swappable(self):
        # 起止的 UTC 写法互换（含省略小数与 .000000）：同一时刻、同一结果
        label = f"recent --since {T1_OFFSET} --until {T1_Z} --limit 1"
        proc = run_recent(
            self.accept_db, "--url", URL_A,
            "--since", T1_OFFSET, "--until", T1_Z, "--limit", "1",
        )
        self.assert_ok(proc, label)
        self.assertEqual(json.loads(proc.stdout), [ACCEPT_REC2])

    def test_acceptance_equal_bounds_valid_and_limit_1_id_2(self):
        for since_value, until_value in ((T1_Z, T1_OFFSET),
                                        (T1_OFFSET, T1_Z)):
            with self.subTest(since=since_value, until=until_value):
                proc = run_recent(
                    self.accept_db, "--url", URL_A,
                    "--since", since_value, "--until", until_value,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(
                    json.loads(proc.stdout), [ACCEPT_REC2, ACCEPT_REC1],
                    "起止相等是只含该时刻的合法闭合窗口，按 id 倒序",
                )

    # ---- 省略边界：单用按方向筛选、同用闭合窗口、倒序限量 ----

    def test_since_alone_filters_lower_bound(self):
        proc = run_recent(self.ext_db, "--url", URL_A, "--since", T1_Z)
        self.assert_ok(proc, "--since 单用")
        self.assertEqual(
            [r["id"] for r in json.loads(proc.stdout)], [3, 2, 1],
            "--since 单用：保留不早于下界（含相等时刻）的记录",
        )

    def test_until_alone_filters_upper_bound(self):
        proc = run_recent(self.ext_db, "--url", URL_A, "--until", T1_Z)
        self.assert_ok(proc, "--until 单用")
        self.assertEqual(
            [r["id"] for r in json.loads(proc.stdout)], [4, 2, 1],
            "--until 单用：不设下界，保留不晚于上界（含相等时刻）的记录，"
            "按 id 倒序",
        )

    def test_closed_window_and_id_desc_limit(self):
        proc = run_recent(
            self.ext_db, "--url", URL_A,
            "--since", T_EARLY, "--until", T1_OFFSET, "--limit", "2",
        )
        self.assert_ok(proc, "闭合窗口 + limit 2")
        self.assertEqual(
            [r["id"] for r in json.loads(proc.stdout)], [4, 2],
            "先按时刻窗口过滤（含 id 4 的 00:00:01 端点），再按 id 倒序限量",
        )

    def test_summaries_share_the_same_result_set(self):
        extra = ("--url", URL_A, "--since", T_EARLY, "--until", T1_Z)
        # 窗口内为 id 2（10ms）、1（0ms）、4（30ms）
        proc = run_recent(self.ext_db, "--summary", *extra)
        self.assert_ok(proc, "窗口 --summary")
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["min_elapsed_ms"], 0)
        self.assertEqual(summary["max_elapsed_ms"], 30)
        self.assertAlmostEqual(summary["avg_elapsed_ms"], 40 / 3)

        proc_s = run_recent(self.ext_db, "--status-summary", *extra)
        self.assert_ok(proc_s, "窗口 --status-summary")
        self.assertEqual(
            json.loads(proc_s.stdout),
            {"count": 3, "success_count": 0, "failure_count": 3},
            "状态摘要必须与普通/耗时摘要统计同一结果集合",
        )

    def test_legal_query_no_match_outputs_empty(self):
        for summary in (False, True):
            with self.subTest(summary=summary):
                extra = ("--summary",) if summary else ()
                proc = run_recent(
                    self.ext_db, *extra, "--url", URL_A,
                    "--since", "2026-10-04T00:00:10Z",
                )
                self.assert_ok(proc, "合法无命中")
                if summary:
                    self.assertEqual(json.loads(proc.stdout), EMPTY_SUMMARY)
                else:
                    self.assertEqual(proc.stdout, "[]\n")
        proc = run_recent(
            self.ext_db, "--status-summary", "--url", URL_A,
            "--until", "2026-10-04T00:00:00Z",
        )
        self.assert_ok(proc, "合法无命中 status-summary")
        self.assertEqual(
            json.loads(proc.stdout),
            {"count": 0, "success_count": 0, "failure_count": 0},
        )

    # ---- 每个边界：裸参数缺值与各类非法取值 ----

    def test_bare_bound_missing_value_rejected(self):
        for name, option in BOUND_ARGS:
            with self.subTest(bound=name):
                # 裸参数会吞掉后续 token，置于末尾
                label = f"recent {option}（裸用、缺值）"
                before = snapshot_state(self.tmp, self.ext_db)
                proc = run_recent(self.ext_db, "--url", URL_A, option)
                self.assert_time_error(proc, label, name, None)
                self.assert_state_unchanged(before, label, self.ext_db)

    def test_invalid_bound_values_rejected(self):
        for name, option in BOUND_ARGS:
            for value, keyword in INVALID_VALUE_CASES:
                with self.subTest(bound=name, value=value):
                    label = f"recent {option} {value!r}"
                    before = snapshot_state(self.tmp, self.ext_db)
                    proc = run_recent(
                        self.ext_db, "--url", URL_A, option, value,
                    )
                    self.assert_time_error(proc, label, name, value)
                    self.assertIn(keyword, proc.stderr)
                    self.assert_state_unchanged(before, label, self.ext_db)

    # ---- 报错优先级 ----

    def test_both_bounds_invalid_reports_since_first(self):
        cases = [
            ("bad", "worse"),
            ("2026-10-04T00:00:02", "2026-10-04T00:00:03"),
        ]
        for since_value, until_value in cases:
            with self.subTest(since=since_value, until=until_value):
                proc = run_recent(
                    self.ext_db,
                    "--since", since_value, "--until", until_value,
                )
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, "")
                self.assertIn("since", proc.stderr)
                self.assertNotIn(
                    "until 参数错误", proc.stderr,
                    f"两边界同时非法必须先报 --since：{proc.stderr!r}",
                )
                self.assertNotIn("Traceback", proc.stderr)

    def test_bare_both_bounds_reports_since_first(self):
        # 两个裸参数相邻：argparse 把第二个选项名作为 --since 的值，
        # 先暴露的仍是 --since 取值非法；用两个独立裸用例的顺序语义同样
        # 由 since 先校验保证，这里直接核对 --since 缺值路径不被 --until 抢占
        proc = run_recent(self.ext_db, "--until", "bad", "--since")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("since", proc.stderr)
        self.assertNotIn("until 参数错误", proc.stderr)

    def test_time_error_takes_priority_over_directory_db(self):
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir()
        for name, option, value in (
            ("since", "--since", "2026-10-04T00:00:02"),
            ("until", "--until", "2026-02-30T00:00:02Z"),
        ):
            with self.subTest(bound=name):
                before = snapshot_state(self.tmp, db_dir)
                proc = run_recent(db_dir, option, value)
                self.assert_time_error(
                    proc, f"目录库 + 非法 {name}", name, value,
                )
                self.assertNotIn(
                    "目录", proc.stderr,
                    "时间参数错误必须优先于目录数据库路径错误",
                )
                self.assert_state_unchanged(
                    before, f"目录库 + 非法 {name}", db_dir,
                )
        # 两边界同时非法且库路径为目录：仍只报 --since
        proc = run_recent(
            db_dir, "--since", "bad", "--until", "worse",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("since", proc.stderr)
        self.assertNotIn("until 参数错误", proc.stderr)
        self.assertNotIn("目录", proc.stderr)

    def test_non_time_validation_priority_unchanged(self):
        db_dir = self.tmp / "order-dir"
        db_dir.mkdir()
        # 非法 status 与非法 --since 同时出现：仍先报 status
        proc = run_recent(
            db_dir, "--status", "nope",
            "--since", "2026-10-04T00:00:02",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("status", proc.stderr)
        self.assertNotIn("since 参数错误", proc.stderr)
        self.assertNotIn("目录", proc.stderr)
        # 非法 reason 优先于非法 --until
        proc = run_recent(
            db_dir, "--url", URL_A, "--reason", "bad",
            "--until", "2026-10-04T00:00:02",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("reason", proc.stderr)
        self.assertNotIn("until 参数错误", proc.stderr)
        self.assertNotIn("目录", proc.stderr)

    # ---- 起点晚于终点：沿用原时间范围参数错误 ----

    def test_since_later_than_until_rejected(self):
        label = "recent --since 00:00:04 --until 00:00:01（起点晚于终点）"
        before = snapshot_state(self.tmp, self.ext_db)
        proc = run_recent(
            self.ext_db, "--url", URL_A,
            "--since", T_LATE, "--until", T_EARLY,
        )
        self.assertEqual(
            proc.returncode, 2,
            f"{label}: 期望退出码 2，实际 {proc.returncode}",
        )
        self.assertEqual(proc.stdout, "")
        self.assertIn("时间范围参数错误", proc.stderr)
        self.assertIn("--since", proc.stderr)
        self.assertIn("--until", proc.stderr)
        self.assertIn(repr(T_LATE), proc.stderr)
        self.assertIn(repr(T_EARLY), proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assert_state_unchanged(before, label, self.ext_db)

    # ---- 查询无副作用；check 命令入口不变 ----

    def test_query_creates_nothing_for_missing_db(self):
        missing = self.tmp / "nope" / "monitor.sqlite"
        before = snapshot_state(self.tmp, missing)
        proc = run_recent(
            missing, "--since", T1_Z, "--until", T1_OFFSET,
        )
        self.assert_ok(proc, "缺库 + 时间窗口")
        self.assertEqual(proc.stdout, "[]\n")
        self.assertFalse(missing.exists())
        self.assertFalse(missing.parent.exists())
        self.assert_state_unchanged(before, "缺库 + 时间窗口", missing)

    def test_check_command_does_not_accept_time_bounds(self):
        for option in ("--since", "--until"):
            with self.subTest(option=option):
                proc = run_cli(
                    self.tmp / "x.sqlite", "check",
                    "--url", URL_A, option, T1_Z,
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("unrecognized arguments", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
