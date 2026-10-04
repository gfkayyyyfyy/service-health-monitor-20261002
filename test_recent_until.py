#!/usr/bin/env python3
"""recent --until UTC 终点筛选的独立回归测试。

仅验证 --until（及其与 --since 组合）的行为，通过公开命令行入口（子进程
运行 healthcheck.py）核对退出码与 stdout/stderr 两路输出，样本数据放在
独立临时 SQLite 数据库中，只用 Python 3 标准库，不依赖演示服务或公网，
临时资源在每个用例结束后释放。check 的探测落库与省略 --until 时的查询
行为不在本文件范围内，仅核对其保持原样。

验收固定样本（同一合法 URL 的三条记录）：
- http://127.0.0.1:8765/
- id 1：2026-10-04T00:00:01Z，耗时 0ms；
- id 2：2026-10-04T00:00:02.000000+00:00（与 Z 写法同一时刻），耗时 10ms；
- id 3：2026-10-04T00:00:03Z，耗时 20ms。

以第一条和第二条的时刻作为起止边界、limit 为 2 查询，应返回 id 2、1 的
完整记录；同条件加 --summary，应得到 count=2、min=0、max=10、avg=5 的
既有摘要对象。

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
URL_B = "http://127.0.0.1:8765/ready"

T1 = "2026-10-04T00:00:01Z"
T2 = "2026-10-04T00:00:02.000000+00:00"
T2_Z = "2026-10-04T00:00:02Z"
T3 = "2026-10-04T00:00:03Z"

REC1 = {
    "id": 1, "url": URL_A, "checked_at": T1,
    "elapsed_ms": 0, "status": "success", "http_status": 200,
    "reason": "ok",
}
REC2 = {
    "id": 2, "url": URL_A, "checked_at": T2,
    "elapsed_ms": 10, "status": "failure", "http_status": None,
    "reason": "timeout",
}
REC3 = {
    "id": 3, "url": URL_A, "checked_at": T3,
    "elapsed_ms": 20, "status": "success", "http_status": 200,
    "reason": "ok",
}

ALL_RECORDS = [REC1, REC2, REC3]

EMPTY_SUMMARY = {
    "count": 0,
    "min_elapsed_ms": None,
    "max_elapsed_ms": None,
    "avg_elapsed_ms": None,
}

# 非法 --until 取值：(取值, stderr 应包含的原因关键词)
INVALID_UNTIL_CASES = [
    ("", "不能为空"),
    ("2026-10-04T00:00:02", "缺少时区"),
    ("2026-10-04T00:00:02+08:00", "仅接受 UTC"),
    ("2026-02-30T00:00:02Z", "真实存在"),
    ("2026-10-04T00:00:02.0000000Z", "格式"),
    (" 2026-10-04T00:00:02Z ", "前后不允许有空白"),
]


def run_cli(db_path, *extra):
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


def run_recent(db_path, *extra):
    return run_cli(db_path, "recent", *extra)


def build_sample_db(path, records=None):
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
        db_path = self.db if db_path is None else pathlib.Path(db_path)
        after = snapshot_state(self.tmp, db_path)
        self.assertEqual(after[0], before[0], f"{label}: 目录文件集合变化")
        self.assertEqual(after[1], before[1], f"{label}: 表或记录内容变化")

    def assert_ok_empty_stderr(self, proc, label):
        self.assertEqual(
            proc.returncode, 0,
            f"输入 {label}：期望退出码 0，实际 {proc.returncode}，"
            f"stderr={proc.stderr!r}",
        )
        self.assertEqual(proc.stderr, "", f"输入 {label}：{proc.stderr!r}")

    def assert_rejected(self, proc, label, *keywords):
        """退出码 2、stdout 为空、stderr 指出 until 与原因、无回溯。"""
        self.assertEqual(
            proc.returncode, 2,
            f"输入 {label}：期望退出码 2，实际 {proc.returncode}，"
            f"stdout={proc.stdout!r}",
        )
        self.assertEqual(proc.stdout, "", f"输入 {label}：{proc.stdout!r}")
        self.assertIn(
            "until", proc.stderr,
            f"输入 {label}：stderr 应指出 until，实际 {proc.stderr!r}",
        )
        for keyword in keywords:
            self.assertIn(
                keyword, proc.stderr,
                f"输入 {label}：stderr 应含 {keyword!r}，实际 {proc.stderr!r}",
            )
        self.assertNotIn("Traceback", proc.stderr)

    # ---- 用户验收主场景：闭合窗口 + limit ----

    def test_inclusive_window_limit_returns_id_2_1_full_records(self):
        label = f"recent --since {T1} --until {T2} --limit 2（验收主场景）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, "--url", URL_A,
            "--since", T1, "--until", T2, "--limit", "2",
        )
        self.assert_ok_empty_stderr(proc, label)
        records = json.loads(proc.stdout)
        self.assertEqual([r["id"] for r in records], [2, 1])
        self.assertEqual(records, [REC2, REC1])
        # checked_at 原始字符串原样保留（不统一改写成 Z 或 +00:00）
        self.assertEqual(records[0]["checked_at"], T2)
        self.assertEqual(records[1]["checked_at"], T1)
        for record in records:
            self.assertEqual(set(record.keys()), set(COLUMNS))
        self.assert_state_unchanged(before, label)

    def test_inclusive_window_summary_count_min_max_avg(self):
        label = f"recent --summary --since {T1} --until {T2} --limit 2"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, "--summary", "--url", URL_A,
            "--since", T1, "--until", T2, "--limit", "2",
        )
        self.assert_ok_empty_stderr(proc, label)
        self.assertEqual(
            json.loads(proc.stdout),
            {"count": 2, "min_elapsed_ms": 0,
             "max_elapsed_ms": 10, "avg_elapsed_ms": 5.0},
        )
        self.assert_state_unchanged(before, label)

    # ---- --until 单独使用：不设下界 ----

    def test_until_alone_has_no_lower_bound(self):
        label = "recent --until 00:00:02（单独使用）"
        proc = run_recent(self.db, "--url", URL_A, "--until", T2_Z)
        self.assert_ok_empty_stderr(proc, label)
        self.assertEqual(
            json.loads(proc.stdout), [REC2, REC1],
            f"输入 {label}：应保留不晚于终点的 id 2、1",
        )

    def test_until_alone_summary_counts_zero_elapsed(self):
        label = "recent --summary --until 00:00:10（全部，含零耗时）"
        proc = run_recent(
            self.db, "--summary", "--url", URL_A,
            "--until", "2026-10-04T00:00:10Z",
        )
        self.assert_ok_empty_stderr(proc, label)
        self.assertEqual(
            json.loads(proc.stdout),
            {"count": 3, "min_elapsed_ms": 0,
             "max_elapsed_ms": 20, "avg_elapsed_ms": 10.0},
        )

    def test_until_equivalent_forms_compare_as_same_instant(self):
        for value in (T2, T2_Z):
            with self.subTest(value=value):
                proc = run_recent(self.db, "--url", URL_A, "--until", value)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(
                    [r["id"] for r in json.loads(proc.stdout)], [2, 1],
                )

    # ---- 端点包含、相等合法 ----

    def test_upper_endpoint_inclusive(self):
        proc = run_recent(self.db, "--url", URL_A, "--until", T3)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            [r["id"] for r in json.loads(proc.stdout)], [3, 2, 1],
            "checked_at 等于 --until 的记录应被包含",
        )

    def test_equal_since_and_until_is_valid(self):
        label = "recent --since 00:00:02Z --until 00:00:02.000000+00:00"
        proc = run_recent(
            self.db, "--url", URL_A, "--since", T2_Z, "--until", T2,
        )
        self.assert_ok_empty_stderr(proc, label)
        self.assertEqual(json.loads(proc.stdout), [REC2])

    def test_since_later_than_until_rejected(self):
        label = "recent --since 00:00:03 --until 00:00:01（起点晚于终点）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, "--since", T3, "--until", T1,
        )
        self.assertEqual(proc.returncode, 2, f"stdout={proc.stdout!r}")
        self.assertEqual(proc.stdout, "")
        self.assertIn("--since", proc.stderr)
        self.assertIn("--until", proc.stderr)
        self.assertIn("晚于", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assert_state_unchanged(before, label)

    # ---- 非法 --until：退出码 2、stdout 为空、stderr 指出 until 与原因 ----

    def test_until_bare_missing_value_rejected(self):
        # 裸 --until 会吞掉后面的参数，置于末尾
        label = "recent --until（裸用、缺值）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--until")
        self.assert_rejected(proc, label, "缺少值")
        self.assert_state_unchanged(before, label)

    def test_until_invalid_values_rejected(self):
        for value, keyword in INVALID_UNTIL_CASES:
            with self.subTest(value=value):
                label = f"recent --until {value!r}"
                before = snapshot_state(self.tmp, self.db)
                proc = run_recent(self.db, "--until", value)
                self.assert_rejected(proc, label, keyword)
                self.assert_state_unchanged(before, label)

    def test_invalid_until_with_directory_db_reports_until_first(self):
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir()
        before = snapshot_state(self.tmp, db_dir)
        proc = run_recent(db_dir, "--until", "2026-10-04T00:00:02")
        self.assert_rejected(proc, "目录库+非法 until", "缺少时区")
        self.assertNotIn("目录", proc.stderr)
        self.assert_state_unchanged(before, "目录库+非法 until", db_dir)

    def test_invalid_since_reported_before_invalid_until(self):
        proc = run_recent(self.db, "--since", "bad", "--until", "worse")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("since", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    # ---- 记录 checked_at 非法：全部符合其余筛选的记录，先于窗口与 limit ----

    def test_bad_checked_at_before_since_still_rejected(self):
        # 坏记录时刻无法解析、且即便合法也早于 since 落在窗口之外，仍拒绝
        bad_db = self.tmp / "bad-before-since.sqlite"
        records = [
            {"id": 1, "url": URL_A, "checked_at": "not-a-timestamp",
             "elapsed_ms": 5, "status": "failure", "http_status": None,
             "reason": "timeout"},
            {"id": 2, "url": URL_A, "checked_at": "2026-10-04T00:00:05Z",
             "elapsed_ms": 6, "status": "failure", "http_status": None,
             "reason": "timeout"},
        ]
        build_sample_db(bad_db, records)
        label = "窗口之外的坏记录（早于 since）"
        before = snapshot_state(self.tmp, bad_db)
        proc = run_recent(
            bad_db, "--url", URL_A,
            "--since", "2026-10-04T00:00:04Z",
            "--until", "2026-10-04T00:00:06Z",
        )
        self.assertEqual(proc.returncode, 2, f"stdout={proc.stdout!r}")
        self.assertEqual(proc.stdout, "")
        self.assertIn("id=1", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assert_state_unchanged(before, label, bad_db)

    def test_bad_checked_at_after_until_still_rejected(self):
        bad_db = self.tmp / "bad-after-until.sqlite"
        records = [
            {"id": 1, "url": URL_A, "checked_at": "not-a-timestamp",
             "elapsed_ms": 5, "status": "failure", "http_status": None,
             "reason": "timeout"},
            {"id": 2, "url": URL_A, "checked_at": "2026-10-04T00:00:05Z",
             "elapsed_ms": 6, "status": "failure", "http_status": None,
             "reason": "timeout"},
        ]
        build_sample_db(bad_db, records)
        proc = run_recent(
            bad_db, "--url", URL_A, "--until", "2026-10-04T00:00:06Z",
        )
        self.assertEqual(proc.returncode, 2, f"stdout={proc.stdout!r}")
        self.assertIn("id=1", proc.stderr)

    def test_bad_checked_at_beyond_limit_still_rejected(self):
        bad_db = self.tmp / "bad-beyond-limit.sqlite"
        records = [
            {"id": 1, "url": URL_A, "checked_at": "not-a-timestamp",
             "elapsed_ms": 5, "status": "failure", "http_status": None,
             "reason": "timeout"},
            {"id": 2, "url": URL_A, "checked_at": "2026-10-04T00:00:05Z",
             "elapsed_ms": 6, "status": "failure", "http_status": None,
             "reason": "timeout"},
            {"id": 3, "url": URL_A, "checked_at": "2026-10-04T00:00:06Z",
             "elapsed_ms": 7, "status": "failure", "http_status": None,
             "reason": "timeout"},
        ]
        build_sample_db(bad_db, records)
        proc = run_recent(
            bad_db, "--url", URL_A,
            "--until", "2026-10-04T00:00:10Z", "--limit", "1",
        )
        self.assertEqual(proc.returncode, 2, f"stdout={proc.stdout!r}")
        self.assertIn("id=1", proc.stderr)

    def test_bad_checked_at_excluded_by_other_filter_ignored(self):
        excluded_db = self.tmp / "bad-excluded.sqlite"
        records = [
            {"id": 1, "url": URL_B, "checked_at": "not-a-timestamp",
             "elapsed_ms": 5, "status": "failure", "http_status": None,
             "reason": "timeout"},
            {"id": 2, "url": URL_A, "checked_at": "2026-10-04T00:00:05Z",
             "elapsed_ms": 6, "status": "failure", "http_status": None,
             "reason": "timeout"},
        ]
        build_sample_db(excluded_db, records)
        proc = run_recent(
            excluded_db, "--url", URL_A,
            "--since", "2026-10-04T00:00:00Z",
            "--until", "2026-10-04T00:00:10Z",
        )
        self.assert_ok_empty_stderr(proc, "坏记录被 --url 排除")
        self.assertEqual(
            [r["id"] for r in json.loads(proc.stdout)], [2],
        )

    # ---- 省略 --until / 空结果 / 缺库等既有约定 ----

    def test_without_until_results_unchanged(self):
        # 与主场景同一批参数但省略 --until：id 3 不被上界排除，limit 2
        # 按 id 倒序取 [3, 2]
        proc = run_recent(
            self.db, "--url", URL_A, "--since", T1, "--limit", "2",
        )
        self.assert_ok_empty_stderr(proc, "省略 --until")
        self.assertEqual(
            [r["id"] for r in json.loads(proc.stdout)], [3, 2],
        )

    def test_until_no_match_empty(self):
        for summary in (False, True):
            with self.subTest(summary=summary):
                extra = ("--summary",) if summary else ()
                proc = run_recent(
                    self.db, *extra, "--url", URL_A,
                    "--until", "2026-10-04T00:00:00Z",
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                if summary:
                    self.assertEqual(json.loads(proc.stdout), EMPTY_SUMMARY)
                else:
                    self.assertEqual(proc.stdout, "[]\n")

    def test_missing_db_returns_empty_and_creates_nothing(self):
        missing = self.tmp / "nope" / "monitor.sqlite"
        before = snapshot_state(self.tmp, missing)
        proc = run_recent(missing, "--until", T2)
        self.assert_ok_empty_stderr(proc, "缺库")
        self.assertEqual(proc.stdout, "[]\n")
        self.assertFalse(missing.exists())
        self.assertFalse(missing.parent.exists())
        self.assert_state_unchanged(before, "缺库", missing)

    def test_directory_db_rejected(self):
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir()
        proc = run_recent(db_dir, "--until", T2)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("目录", proc.stderr)

    def test_invalid_db_file_rejected(self):
        broken = self.tmp / "broken.sqlite"
        broken.write_text("not a sqlite db", encoding="utf-8")
        proc = run_recent(broken, "--until", T2)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")

    def test_no_checks_table_returns_empty(self):
        other = self.tmp / "other.sqlite"
        conn = sqlite3.connect(str(other))
        conn.execute("CREATE TABLE x (a)")
        conn.commit()
        conn.close()
        proc = run_recent(other, "--until", T2)
        self.assert_ok_empty_stderr(proc, "无 checks 表")
        self.assertEqual(proc.stdout, "[]\n")

    def test_missing_columns_rejected(self):
        bad_schema = self.tmp / "missing-cols.sqlite"
        conn = sqlite3.connect(str(bad_schema))
        conn.execute("CREATE TABLE checks (id INTEGER PRIMARY KEY, url TEXT)")
        conn.commit()
        conn.close()
        proc = run_recent(bad_schema, "--until", T2)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("缺少字段", proc.stderr)

    # ---- check 命令不新增 --until ----

    def test_check_does_not_accept_until(self):
        proc = run_cli(
            self.tmp / "x.sqlite", "check",
            "--url", URL_A, "--until", T2,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unrecognized arguments", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
