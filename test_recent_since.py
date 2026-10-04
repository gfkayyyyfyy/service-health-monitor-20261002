#!/usr/bin/env python3
"""recent --since UTC 起点筛选已有行为的独立回归测试。

仅验证 --since 的既有行为，通过公开命令行入口（子进程运行 healthcheck.py）
核对退出码与 stdout/stderr 两路输出，样本数据放在独立临时 SQLite 数据库中，
只用 Python 3 标准库，不依赖演示服务或公网，临时资源在每个用例结束后释放。
不修改产品代码、表结构及文档；check 的探测落库与省略 --since 时的查询
行为不在本文件的修改范围内，仅核对其保持原样（省略 --since 不做时间校验）。

验收固定样本（四条记录，全部 failure/timeout、http_status 为 null）：
- 目标 A：http://127.0.0.1:8765/health
- 目标 B：http://127.0.0.1:8765/ready（仅路径不同）
- id 1、2 属于 A：checked_at 分别为 2026-10-04T00:00:02Z 与
  2026-10-04T00:00:02.000000+00:00（同一时刻、两种字符串写法）；
- id 3 属于 B：2026-10-04T00:00:04Z；
- id 4 属于 A：2026-10-04T00:00:01Z；
- elapsed_ms 依次为 0、10、20、30 毫秒。

在项目目录执行：
    python3 -m unittest test_recent_since
或：
    python3 test_recent_since.py
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
URL_B = "http://127.0.0.1:8765/ready"

SINCE_EXACT = "2026-10-04T00:00:02Z"
SINCE_EXACT_OFFSET = "2026-10-04T00:00:02.000000+00:00"
# 在同一时刻基础上推进一微秒：id 1、2 均早于它，普通结果为空
SINCE_ONE_MICROSECOND_LATER = "2026-10-04T00:00:02.000001Z"

# id 1、2 同为 A、同一时刻（00:00:02），仅时间字符串写法不同，
# 用于区分「按时刻比较」与「按字符串比较」；id 4（A）早一秒，
# id 3（B）晚两秒。耗时 0、10、20、30，全部 failure/timeout。
REC1 = {
    "id": 1, "url": URL_A,
    "checked_at": "2026-10-04T00:00:02Z",
    "elapsed_ms": 0, "status": "failure", "http_status": None,
    "reason": "timeout",
}
REC2 = {
    "id": 2, "url": URL_A,
    "checked_at": "2026-10-04T00:00:02.000000+00:00",
    "elapsed_ms": 10, "status": "failure", "http_status": None,
    "reason": "timeout",
}
REC3 = {
    "id": 3, "url": URL_B,
    "checked_at": "2026-10-04T00:00:04Z",
    "elapsed_ms": 20, "status": "failure", "http_status": None,
    "reason": "timeout",
}
REC4 = {
    "id": 4, "url": URL_A,
    "checked_at": "2026-10-04T00:00:01Z",
    "elapsed_ms": 30, "status": "failure", "http_status": None,
    "reason": "timeout",
}

ALL_RECORDS = [REC1, REC2, REC3, REC4]

# --status failure、--reason timeout、起点 00:00:02、--limit 2 时，
# A 中不早于起点的记录为 id 2、1（id 倒序），输出数据库保存的原始字符串
MATCHED_ID_2_1 = [REC2, REC1]

EMPTY_SUMMARY = {
    "count": 0,
    "min_elapsed_ms": None,
    "max_elapsed_ms": None,
    "avg_elapsed_ms": None,
}

# 非法 --since 取值：(取值, stderr 应包含的原因关键词)
INVALID_SINCE_CASES = [
    # 空值（命令行上显式给出空串）
    ("", "不能为空"),
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


def a_failure_timeout_filters(*since_and_more):
    """A + failure + timeout 的固定筛选，尾部追加 --since 等其余参数。"""
    return ("--url", URL_A, "--status", "failure", "--reason",
            "timeout", *since_and_more)


def build_sample_db(path, records=None):
    """创建包含固定样本（默认四条）的临时数据库。"""
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


class RecentSinceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-since-"))
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
        """退出码 2、stdout 为空、stderr 指出 since 与原因、无回溯。"""
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
            "since", proc.stderr,
            f"输入 {label}：stderr 应指出 since，实际 {proc.stderr!r}",
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

    # ---- 按时刻比较：Z 与 +00:00、省略零小数视为同一时刻 ----

    def test_since_z_returns_full_records_in_id_desc_order(self):
        label = (f"recent A failure/timeout --since {SINCE_EXACT} --limit 2"
                 "（普通输出）")
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, *a_failure_timeout_filters(
                "--since", SINCE_EXACT, "--limit", "2")
        )
        self.assert_ok_empty_stderr(proc, label)
        records = json.loads(proc.stdout)
        # 必须是完整原始记录：七个字段齐全、顺序为 id 2、1
        self.assertEqual(
            [r["id"] for r in records], [2, 1],
            f"输入 {label}：期望 id 顺序 [2, 1]（按时刻比较而非字符串），"
            f"实际 {[r['id'] for r in records]}",
        )
        self.assertEqual(
            records, MATCHED_ID_2_1,
            f"输入 {label}：应返回完整原始记录且不改写时间字符串，"
            f"实际 {records}",
        )
        # 时间字符串必须原样保留（不统一改写成 Z 或 +00:00）
        self.assertEqual(records[0]["checked_at"], REC2["checked_at"])
        self.assertEqual(records[1]["checked_at"], REC1["checked_at"])
        for record in records:
            self.assertEqual(
                set(record.keys()), set(COLUMNS),
                f"输入 {label}：记录应包含完整字段 {COLUMNS}",
            )
        self.assert_state_unchanged(before, label)

    def test_since_equivalent_offset_form_same_result(self):
        label = (f"recent A failure/timeout --since {SINCE_EXACT_OFFSET} "
                 "--limit 2（等价 +00:00 写法）")
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, *a_failure_timeout_filters(
                "--since", SINCE_EXACT_OFFSET, "--limit", "2")
        )
        self.assert_ok_empty_stderr(proc, label)
        records = json.loads(proc.stdout)
        self.assertEqual(
            records, MATCHED_ID_2_1,
            f"输入 {label}：.000000+00:00 与 Z 应按同一时刻比较，"
            f"实际 {[r['id'] for r in records]}",
        )
        self.assert_state_unchanged(before, label)

    def test_since_summary_count_min_max_avg(self):
        label = (f"recent --summary A failure/timeout --since {SINCE_EXACT} "
                 "--limit 2")
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, "--summary", *a_failure_timeout_filters(
                "--since", SINCE_EXACT, "--limit", "2")
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

    def test_since_equivalent_offset_form_summary_same(self):
        label = (f"recent --summary A failure/timeout "
                 f"--since {SINCE_EXACT_OFFSET} --limit 2")
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, "--summary", *a_failure_timeout_filters(
                "--since", SINCE_EXACT_OFFSET, "--limit", "2")
        )
        self.assert_ok_empty_stderr(proc, label)
        self.assertEqual(
            json.loads(proc.stdout),
            {"count": 2, "min_elapsed_ms": 0,
             "max_elapsed_ms": 10, "avg_elapsed_ms": 5.0},
        )
        self.assert_state_unchanged(before, label)

    def test_since_one_microsecond_later_plain_output_empty(self):
        label = (f"recent A failure/timeout "
                 f"--since {SINCE_ONE_MICROSECOND_LATER} --limit 2（普通输出）")
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, *a_failure_timeout_filters(
                "--since", SINCE_ONE_MICROSECOND_LATER, "--limit", "2")
        )
        self.assert_ok_empty_stderr(proc, label)
        self.assertEqual(
            proc.stdout, "[]\n",
            f"输入 {label}：推进一微秒后 id 1、2 都早于起点，"
            f"应输出 []，实际 {proc.stdout!r}",
        )
        self.assert_state_unchanged(before, label)

    def test_since_one_microsecond_later_summary_nulls(self):
        label = (f"recent --summary A failure/timeout "
                 f"--since {SINCE_ONE_MICROSECOND_LATER} --limit 2")
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, "--summary", *a_failure_timeout_filters(
                "--since", SINCE_ONE_MICROSECOND_LATER, "--limit", "2")
        )
        self.assert_ok_empty_stderr(proc, label)
        self.assertEqual(
            json.loads(proc.stdout), EMPTY_SUMMARY,
            f"输入 {label}：空结果摘要 count 为 0、三项为 null",
        )
        self.assert_state_unchanged(before, label)

    # ---- 非法 --since：退出码 2、stdout 为空、stderr 指出 since 与原因 ----

    def test_since_bare_missing_value_rejected(self):
        # 裸 --since：argparse 下它会吞掉后面的 --limit，故置于参数末尾
        label = "recent A failure/timeout --since（裸用、缺值）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, *a_failure_timeout_filters("--since")
        )
        self.assert_rejected(proc, label, "缺少值")
        self.assert_state_unchanged(before, label)

    def test_since_invalid_values_rejected(self):
        for value, reason_keyword in INVALID_SINCE_CASES:
            with self.subTest(value=value):
                label = (f"recent A failure/timeout --since {value!r} "
                         "--limit 2")
                before = snapshot_state(self.tmp, self.db)
                proc = run_recent(
                    self.db, *a_failure_timeout_filters(
                        "--since", value, "--limit", "2")
                )
                self.assert_rejected(proc, label, reason_keyword)
                self.assert_state_unchanged(before, label)

    def test_invalid_since_with_directory_db_reports_since_first(self):
        # 非法起点与目录数据库路径同时出现：参数校验先于数据库访问，
        # 必须优先报告起点错误，不报告目录
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir()
        label = f"recent --since 缺时区（数据库路径为目录 {db_dir}）"
        before = snapshot_state(self.tmp, db_dir)
        proc = run_recent(
            db_dir, "--since", "2026-10-04T00:00:02"
        )
        self.assert_rejected(proc, label, "缺少时区")
        self.assertNotIn(
            "目录", proc.stderr,
            f"输入 {label}：非法起点与目录路径同时出现时应优先报告 "
            f"起点错误，实际 {proc.stderr!r}",
        )
        self.assert_state_unchanged(before, label, db_dir)

    # ---- 记录 checked_at 非法：校验全部符合其余筛选的记录，先于 limit ----

    def test_bad_checked_at_beyond_limit_still_rejected(self):
        # 独立数据：符合其他筛选（A/failure/timeout）的记录共 3 条，
        # 坏记录 id 1 按 id 倒序排在 --limit 2 之外，仍必须被发现并拒绝，
        # stderr 指出该记录 id
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
        label = ("recent A failure/timeout --since 2026-10-04T00:00:00Z "
                 "--limit 2（坏记录 id=1 排在 limit 之外）")
        before = snapshot_state(self.tmp, bad_db)
        proc = run_recent(
            bad_db, *a_failure_timeout_filters(
                "--since", "2026-10-04T00:00:00Z", "--limit", "2")
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

    def test_bad_checked_at_excluded_by_other_filter_ignored(self):
        # 独立数据：坏记录属于 B，被 --url A 排除；A 的记录都合法。
        # 查询不得触碰被排除的坏记录，正常返回
        excluded_db = self.tmp / "bad-excluded.sqlite"
        records = [
            {
                "id": 1, "url": URL_B,
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
        label = ("recent A failure/timeout --since 2026-10-04T00:00:00Z"
                 "（坏记录属于 B 被 --url 排除）")
        before = snapshot_state(self.tmp, excluded_db)
        proc = run_recent(
            excluded_db, *a_failure_timeout_filters(
                "--since", "2026-10-04T00:00:00Z")
        )
        self.assert_ok_empty_stderr(proc, label)
        records_out = json.loads(proc.stdout)
        self.assertEqual(
            [r["id"] for r in records_out], [2],
            f"输入 {label}：被其他筛选排除的坏记录不应影响查询，"
            f"实际 {[r['id'] for r in records_out]}",
        )
        self.assert_state_unchanged(before, label, excluded_db)

    # ---- 省略 --since：不增加时间条件、不做时间校验 ----

    def test_without_since_ignores_time_and_does_not_validate(self):
        # 同样的 A/failure/timeout/limit 2，省略 --since：
        # 不做时刻过滤（id 4 的 00:00:01 仍可入选，且排在最前），
        # 仅按 id 倒序取前两条 4、2；对照加 --since 00:00:02 时为 2、1
        label = "recent A failure/timeout --limit 2（省略 --since）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, "--url", URL_A, "--status", "failure",
            "--reason", "timeout", "--limit", "2",
        )
        self.assert_ok_empty_stderr(proc, label)
        records = json.loads(proc.stdout)
        self.assertEqual(
            [r["id"] for r in records], [4, 2],
            f"输入 {label}：省略 --since 时不按时刻过滤，"
            f"00:00:01 的 id 4 应入选并按 id 倒序得到 [4, 2]",
        )
        self.assert_state_unchanged(before, label)

    def test_without_since_bad_checked_at_not_validated(self):
        # 省略 --since 时不增加时间校验：坏 checked_at 记录照常返回
        bad_db = self.tmp / "bad-no-since.sqlite"
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
        label = "recent A failure/timeout（省略 --since，含坏 checked_at）"
        before = snapshot_state(self.tmp, bad_db)
        proc = run_recent(
            bad_db, "--url", URL_A, "--status", "failure",
            "--reason", "timeout",
        )
        self.assert_ok_empty_stderr(proc, label)
        records_out = json.loads(proc.stdout)
        self.assertEqual(
            [r["id"] for r in records_out], [2, 1],
            f"输入 {label}：省略 --since 不应校验 checked_at，坏记录照常返回",
        )
        self.assertEqual(records_out[1]["checked_at"], "not-a-timestamp")
        self.assert_state_unchanged(before, label, bad_db)

    # ---- 表结构保持不变 ----

    def test_schema_remains_compatible(self):
        proc = run_recent(
            self.db, *a_failure_timeout_filters(
                "--since", SINCE_EXACT, "--limit", "2")
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
