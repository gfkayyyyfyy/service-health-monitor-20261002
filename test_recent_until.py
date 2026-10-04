#!/usr/bin/env python3
"""recent --until UTC 终点筛选及与 --since 组合窗口的独立回归测试。

仅验证新增的 --until 行为，通过公开命令行入口（子进程运行 healthcheck.py）
核对退出码与 stdout/stderr 两路输出，样本数据放在独立临时 SQLite 数据库中，
只用 Python 3 标准库，不依赖演示服务或公网，临时资源在每个用例结束后释放。
不修改产品代码、表结构及文档；check 的探测落库与省略 --until 时的查询
行为不在本文件的修改范围内，仅核对其保持原样。

验收固定样本（三条记录，同一合法 URL，全部 failure/timeout）：
- 目标 A：http://127.0.0.1:8765/health
- id 1：checked_at 2026-10-04T00:00:01Z，elapsed_ms 0
- id 2：checked_at 2026-10-04T00:00:02.000000+00:00，elapsed_ms 10
- id 3：checked_at 2026-10-04T00:00:03Z，elapsed_ms 20

以第一条与第二条的时刻作为起止边界、limit 为 2 查询，应返回 id 2、1
的完整记录；同条件加 --summary 应得 count 2、最小 0、最大 10、平均 5。

在项目目录执行：
    python3 -m unittest test_recent_until
或：
    python3 test_recent_until.py
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

# ---- 固定样本 -------------------------------------------------------------
URL_A = "http://127.0.0.1:8765/health"

# 三条记录的时刻：00:00:01、00:00:02（带零小数与 +00:00 写法）、00:00:03
T1_Z = "2026-10-04T00:00:01Z"
T2_OFFSET = "2026-10-04T00:00:02.000000+00:00"
T2_Z = "2026-10-04T00:00:02Z"
T3_Z = "2026-10-04T00:00:03Z"

REC1 = {
    "id": 1, "url": URL_A,
    "checked_at": T1_Z,
    "elapsed_ms": 0, "status": "failure", "http_status": None,
    "reason": "timeout",
}
REC2 = {
    "id": 2, "url": URL_A,
    "checked_at": T2_OFFSET,
    "elapsed_ms": 10, "status": "failure", "http_status": None,
    "reason": "timeout",
}
REC3 = {
    "id": 3, "url": URL_A,
    "checked_at": T3_Z,
    "elapsed_ms": 20, "status": "failure", "http_status": None,
    "reason": "timeout",
}

ALL_RECORDS = [REC1, REC2, REC3]

# 起点 T1、终点 T2、limit 2：窗口含两个端点，id 倒序为 2、1，
# 输出数据库保存的原始 checked_at 字符串
MATCHED_ID_2_1 = [REC2, REC1]

EMPTY_SUMMARY = {
    "count": 0,
    "min_elapsed_ms": None,
    "max_elapsed_ms": None,
    "avg_elapsed_ms": None,
}

# 非法 --until 取值：(取值, stderr 应包含的原因关键词)
INVALID_UNTIL_CASES = [
    # 空值（命令行上显式给出空串）
    ("", "不能为空"),
    # 前后空白
    (" 2026-10-04T00:00:02Z", "空白"),
    # 缺少时区
    ("2026-10-04T00:00:02", "缺少时区"),
    # 非 UTC 偏移
    ("2026-10-04T00:00:02+08:00", "仅接受 UTC"),
    # 结构合法但日期不存在
    ("2026-02-30T00:00:02Z", "真实存在"),
    # 七位小数（最多六位）
    ("2026-10-04T00:00:02.0000000Z", "格式"),
]


def run_cli(db_path, *extra):
    """以子进程运行 healthcheck.py 命令行，返回 CompletedProcess。"""
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


def run_recent(db_path, *extra):
    return run_cli(db_path, "recent", *extra)


def build_sample_db(path, records=None):
    """创建包含固定样本（默认三条）的临时数据库。"""
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


class RecentUntilTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-until-"))
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

    def assert_rejected(self, proc, label, *keywords):
        """退出码 2、stdout 为空、stderr 指出 until 与原因、无回溯。"""
        self.assertEqual(
            proc.returncode, 2,
            f"输入 {label}：期望退出码 2，实际 {proc.returncode}，"
            f"stdout={proc.stdout!r}",
        )
        self.assertEqual(
            proc.stdout, "",
            f"输入 {label}：期望 stdout 为空，实际 {proc.stdout!r}",
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

    # ---- 验收主场景：起点 T1、终点 T2、limit 2，返回 id 2、1 ----

    def test_window_since_t1_until_t2_returns_id_2_1(self):
        label = (f"recent --url A --since {T1_Z} --until {T2_OFFSET} "
                 "--limit 2（普通输出）")
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, "--url", URL_A,
            "--since", T1_Z, "--until", T2_OFFSET, "--limit", "2",
        )
        self.assert_ok_empty_stderr(proc, label)
        records = json.loads(proc.stdout)
        self.assertEqual(
            [r["id"] for r in records], [2, 1],
            f"输入 {label}：窗口含两个端点，期望 id 顺序 [2, 1]，"
            f"实际 {[r['id'] for r in records]}",
        )
        self.assertEqual(
            records, MATCHED_ID_2_1,
            f"输入 {label}：应返回完整原始记录且不改写时间字符串，"
            f"实际 {records}",
        )
        # 时间字符串必须原样保留（id 2 的 .000000+00:00 不改写）
        self.assertEqual(records[0]["checked_at"], T2_OFFSET)
        self.assertEqual(records[1]["checked_at"], T1_Z)
        for record in records:
            self.assertEqual(
                set(record.keys()), set(COLUMNS),
                f"输入 {label}：记录应包含完整字段 {COLUMNS}",
            )
        self.assert_state_unchanged(before, label)

    def test_window_summary_count_min_max_avg(self):
        label = (f"recent --summary --url A --since {T1_Z} "
                 f"--until {T2_OFFSET} --limit 2")
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, "--summary", "--url", URL_A,
            "--since", T1_Z, "--until", T2_OFFSET, "--limit", "2",
        )
        self.assert_ok_empty_stderr(proc, label)
        summary = json.loads(proc.stdout)
        # id 2、1 耗时 10、0：count 2、最小 0、最大 10、平均 5.0
        self.assertEqual(
            summary,
            {"count": 2, "min_elapsed_ms": 0,
             "max_elapsed_ms": 10, "avg_elapsed_ms": 5.0},
            f"输入 {label}：摘要应基于 id 2、1 的耗时 10、0，实际 {summary}",
        )
        self.assert_state_unchanged(before, label)

    def test_window_equivalent_utc_forms_same_result(self):
        # 终点改用 Z 写法（同一时刻），结果必须与 +00:00 写法一致
        label = f"recent --url A --since {T1_Z} --until {T2_Z} --limit 2"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, "--url", URL_A,
            "--since", T1_Z, "--until", T2_Z, "--limit", "2",
        )
        self.assert_ok_empty_stderr(proc, label)
        self.assertEqual(
            json.loads(proc.stdout), MATCHED_ID_2_1,
            f"输入 {label}：Z 与 .000000+00:00 应按同一时刻比较",
        )
        self.assert_state_unchanged(before, label)

    # ---- 单独使用 --until：不设下界 ----

    def test_until_alone_no_lower_bound(self):
        label = f"recent --url A --until {T2_Z}（不设下界，默认 limit 5）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--url", URL_A, "--until", T2_Z)
        self.assert_ok_empty_stderr(proc, label)
        records = json.loads(proc.stdout)
        self.assertEqual(
            [r["id"] for r in records], [2, 1],
            f"输入 {label}：单独 --until 应保留不晚于终点的全部记录，"
            f"实际 {[r['id'] for r in records]}",
        )
        self.assert_state_unchanged(before, label)

    def test_until_before_all_records_empty(self):
        label = "recent --url A --until 2026-10-04T00:00:00Z（终点早于全部）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, "--url", URL_A,
            "--until", "2026-10-04T00:00:00Z",
        )
        self.assert_ok_empty_stderr(proc, label)
        self.assertEqual(
            proc.stdout, "[]\n",
            f"输入 {label}：终点早于全部记录，应输出 []，"
            f"实际 {proc.stdout!r}",
        )
        self.assert_state_unchanged(before, label)

    def test_until_before_all_records_summary_nulls(self):
        label = ("recent --summary --url A "
                 "--until 2026-10-04T00:00:00Z（终点早于全部）")
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, "--summary", "--url", URL_A,
            "--until", "2026-10-04T00:00:00Z",
        )
        self.assert_ok_empty_stderr(proc, label)
        self.assertEqual(
            json.loads(proc.stdout), EMPTY_SUMMARY,
            f"输入 {label}：空结果摘要 count 为 0、三项为 null",
        )
        self.assert_state_unchanged(before, label)

    # ---- 与 --since 组合的边界关系 ----

    def test_since_equal_until_legal_single_instant(self):
        # 起点等于终点合法：窗口退化为同一时刻，只含 id 2
        label = f"recent --url A --since {T2_Z} --until {T2_OFFSET}"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, "--url", URL_A,
            "--since", T2_Z, "--until", T2_OFFSET,
        )
        self.assert_ok_empty_stderr(proc, label)
        records = json.loads(proc.stdout)
        self.assertEqual(
            records, [REC2],
            f"输入 {label}：起点等于终点合法，应只含该时刻的 id 2，"
            f"实际 {records}",
        )
        self.assert_state_unchanged(before, label)

    def test_since_later_than_until_rejected(self):
        # 起点晚于终点：参数错误，退出码 2，说明二者关系，不读数据库
        label = f"recent --url A --since {T3_Z} --until {T1_Z}（起点晚于终点）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, "--url", URL_A,
            "--since", T3_Z, "--until", T1_Z,
        )
        self.assert_rejected(proc, label, "since", "until", "晚于")
        self.assert_state_unchanged(before, label)

    def test_since_later_than_until_rejected_before_db_access(self):
        # 起点晚于终点与目录数据库路径同时出现：优先报告时间范围错误
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir()
        label = (f"recent --since {T3_Z} --until {T1_Z}"
                 f"（数据库路径为目录 {db_dir}）")
        before = snapshot_state(self.tmp, db_dir)
        proc = run_recent(db_dir, "--since", T3_Z, "--until", T1_Z)
        self.assert_rejected(proc, label, "since", "until", "晚于")
        self.assertNotIn(
            "目录", proc.stderr,
            f"输入 {label}：时间范围错误应先于目录错误报告，"
            f"实际 {proc.stderr!r}",
        )
        self.assert_state_unchanged(before, label, db_dir)

    # ---- 非法 --until：退出码 2、stdout 为空、stderr 指出 until 与原因 ----

    def test_until_bare_missing_value_rejected(self):
        # 裸 --until：argparse 下它会吞掉后面的 --limit，故置于参数末尾
        label = "recent --url A --until（裸用、缺值）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--url", URL_A, "--until")
        self.assert_rejected(proc, label, "until", "缺少值")
        self.assert_state_unchanged(before, label)

    def test_until_invalid_values_rejected(self):
        for value, reason_keyword in INVALID_UNTIL_CASES:
            with self.subTest(value=value):
                label = f"recent --url A --until {value!r}"
                before = snapshot_state(self.tmp, self.db)
                proc = run_recent(
                    self.db, "--url", URL_A, "--until", value,
                )
                self.assert_rejected(proc, label, "until", reason_keyword)
                self.assert_state_unchanged(before, label)

    def test_invalid_until_with_directory_db_reports_until_first(self):
        # 非法终点与目录数据库路径同时出现：参数校验先于数据库访问
        db_dir = self.tmp / "b-directory"
        db_dir.mkdir()
        label = f"recent --until 缺时区（数据库路径为目录 {db_dir}）"
        before = snapshot_state(self.tmp, db_dir)
        proc = run_recent(db_dir, "--until", "2026-10-04T00:00:02")
        self.assert_rejected(proc, label, "until", "缺少时区")
        self.assertNotIn(
            "目录", proc.stderr,
            f"输入 {label}：非法终点与目录路径同时出现时应优先报告 "
            f"终点错误，实际 {proc.stderr!r}",
        )
        self.assert_state_unchanged(before, label, db_dir)

    # ---- 记录 checked_at 非法：校验全部符合其余筛选的记录，先于 limit ----

    def test_bad_checked_at_beyond_limit_still_rejected_with_until(self):
        # 坏记录 id 1 按 id 倒序排在 --limit 2 之外，仍必须被发现并拒绝
        bad_db = self.tmp / "bad-beyond-limit.sqlite"
        records = [
            {
                "id": 1, "url": URL_A,
                "checked_at": "not-a-timestamp",
                "elapsed_ms": 5, "status": "failure", "http_status": None,
                "reason": "timeout",
            },
            {
                "id": 2, "url": URL_A,
                "checked_at": "2026-10-04T00:00:05Z",
                "elapsed_ms": 6, "status": "failure", "http_status": None,
                "reason": "timeout",
            },
            {
                "id": 3, "url": URL_A,
                "checked_at": "2026-10-04T00:00:06Z",
                "elapsed_ms": 7, "status": "failure", "http_status": None,
                "reason": "timeout",
            },
        ]
        build_sample_db(bad_db, records)
        label = ("recent --url A --until 2026-10-04T00:00:07Z --limit 2"
                 "（坏记录 id=1 排在 limit 之外）")
        before = snapshot_state(self.tmp, bad_db)
        proc = run_recent(
            bad_db, "--url", URL_A,
            "--until", "2026-10-04T00:00:07Z", "--limit", "2",
        )
        self.assertEqual(
            proc.returncode, 2,
            f"输入 {label}：坏记录即使在 limit 之外也应退出 2，"
            f"实际 {proc.returncode}，stdout={proc.stdout!r}",
        )
        self.assertEqual(
            proc.stdout, "",
            f"输入 {label}：期望 stdout 为空，实际 {proc.stdout!r}",
        )
        self.assertIn(
            "id=1", proc.stderr,
            f"输入 {label}：stderr 应指出坏记录 id=1，"
            f"实际 {proc.stderr!r}",
        )
        self.assertNotIn("Traceback", proc.stderr)
        self.assert_state_unchanged(before, label, bad_db)

    def test_bad_checked_at_excluded_by_other_filter_ignored_with_until(self):
        # 坏记录属于另一 URL，被 --url A 排除；不影响查询结果
        excluded_db = self.tmp / "bad-excluded.sqlite"
        records = [
            {
                "id": 1, "url": "http://127.0.0.1:8765/ready",
                "checked_at": "not-a-timestamp",
                "elapsed_ms": 5, "status": "failure", "http_status": None,
                "reason": "timeout",
            },
            {
                "id": 2, "url": URL_A,
                "checked_at": "2026-10-04T00:00:05Z",
                "elapsed_ms": 6, "status": "failure", "http_status": None,
                "reason": "timeout",
            },
        ]
        build_sample_db(excluded_db, records)
        label = ("recent --url A --until 2026-10-04T00:00:06Z"
                 "（坏记录被 --url 排除）")
        before = snapshot_state(self.tmp, excluded_db)
        proc = run_recent(
            excluded_db, "--url", URL_A,
            "--until", "2026-10-04T00:00:06Z",
        )
        self.assert_ok_empty_stderr(proc, label)
        records_out = json.loads(proc.stdout)
        self.assertEqual(
            [r["id"] for r in records_out], [2],
            f"输入 {label}：被其他筛选排除的坏记录不应影响查询，"
            f"实际 {[r['id'] for r in records_out]}",
        )
        self.assert_state_unchanged(before, label, excluded_db)

    # ---- 省略 --until：原有结果不变、不做额外时间校验 ----

    def test_without_until_unchanged(self):
        # 省略 --until：默认 limit 5，按 id 倒序返回全部三条 3、2、1
        label = "recent --url A（省略 --until）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--url", URL_A)
        self.assert_ok_empty_stderr(proc, label)
        records = json.loads(proc.stdout)
        self.assertEqual(
            [r["id"] for r in records], [3, 2, 1],
            f"输入 {label}：省略 --until 时不设上界，"
            f"应按 id 倒序返回 [3, 2, 1]，实际 {[r['id'] for r in records]}",
        )
        self.assert_state_unchanged(before, label)

    def test_without_until_bad_checked_at_not_validated(self):
        # 省略 --until（也不给 --since）时不增加时间校验：坏记录照常返回
        bad_db = self.tmp / "bad-no-until.sqlite"
        records = [
            {
                "id": 1, "url": URL_A,
                "checked_at": "not-a-timestamp",
                "elapsed_ms": 5, "status": "failure", "http_status": None,
                "reason": "timeout",
            },
            {
                "id": 2, "url": URL_A,
                "checked_at": "2026-10-04T00:00:05Z",
                "elapsed_ms": 6, "status": "failure", "http_status": None,
                "reason": "timeout",
            },
        ]
        build_sample_db(bad_db, records)
        label = "recent --url A（省略 --until，含坏 checked_at）"
        before = snapshot_state(self.tmp, bad_db)
        proc = run_recent(bad_db, "--url", URL_A)
        self.assert_ok_empty_stderr(proc, label)
        records_out = json.loads(proc.stdout)
        self.assertEqual(
            [r["id"] for r in records_out], [2, 1],
            f"输入 {label}：省略时间边界不应校验 checked_at，坏记录照常返回",
        )
        self.assertEqual(records_out[1]["checked_at"], "not-a-timestamp")
        self.assert_state_unchanged(before, label, bad_db)

    # ---- 只读性与既有空结果约定 ----

    def test_missing_db_with_until_returns_empty(self):
        # 数据库文件不存在：不创建文件，返回既有空结果
        missing = self.tmp / "no-such.sqlite"
        label = f"recent --until {T2_Z}（数据库不存在）"
        before = snapshot_state(self.tmp, missing)
        proc = run_recent(missing, "--until", T2_Z)
        self.assert_ok_empty_stderr(proc, label)
        self.assertEqual(proc.stdout, "[]\n")
        proc = run_recent(missing, "--summary", "--until", T2_Z)
        self.assert_ok_empty_stderr(proc, label)
        self.assertEqual(json.loads(proc.stdout), EMPTY_SUMMARY)
        self.assertFalse(
            missing.exists(),
            f"输入 {label}：查询不得创建数据库文件",
        )
        self.assert_state_unchanged(before, label, missing)

    def test_no_checks_table_with_until_returns_empty(self):
        # 空数据库（无 checks 表）：返回既有空结果，不建表
        empty_db = self.tmp / "empty.sqlite"
        conn = sqlite3.connect(str(empty_db))
        conn.close()
        label = f"recent --until {T2_Z}（无 checks 表）"
        before = snapshot_state(self.tmp, empty_db)
        proc = run_recent(empty_db, "--until", T2_Z)
        self.assert_ok_empty_stderr(proc, label)
        self.assertEqual(proc.stdout, "[]\n")
        self.assert_state_unchanged(before, label, empty_db)

    def test_schema_remains_compatible(self):
        proc = run_recent(
            self.db, "--url", URL_A,
            "--since", T1_Z, "--until", T2_OFFSET, "--limit", "2",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        conn = sqlite3.connect(str(self.db))
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(checks)")]
        finally:
            conn.close()
        self.assertEqual(
            cols, COLUMNS,
            f"checks 表列结构应保持兼容：{COLUMNS}，实际 {cols}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
