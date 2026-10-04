#!/usr/bin/env python3
"""recent --status-summary 状态计数摘要功能的独立回归测试。

通过公开命令行入口（子进程运行 healthcheck.py）核对退出码与 JSON 输出，
样本数据放在独立临时 SQLite 数据库中，只用 Python 3 标准库，
不依赖真实服务或已有历史文件，临时资源在每个用例结束后释放。
同时核对普通 recent（记录数组）、--summary 耗时摘要与数据库结构的
既有行为保持不变。

在项目目录执行：
    python3 -m unittest test_recent_status_summary
或：
    python3 test_recent_status_summary.py
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

STATUS_SUMMARY_KEYS = {"count", "success_count", "failure_count"}

# ---- 固定样本 -------------------------------------------------------------
URL_A = "http://127.0.0.1:8765/health?detail=1"
URL_B = "http://127.0.0.1:8765/health?detail=2"
# 合法但样本中没有任何记录的目标
URL_NO_MATCH = "http://127.0.0.1:8765/no-such-path?x=9"

# id 1..5 的目标依次为 A、A、A、B、A；状态依次为
# success、failure、failure、success、success；
# 含零耗时（id 2）与各种不同失败原因（timeout/http_status）的记录；
# checked_at 随 id 递增。
REC1 = {
    "id": 1, "url": URL_A,
    "checked_at": "2026-10-02T04:40:01.000000+00:00",
    "elapsed_ms": 90, "status": "success", "http_status": 200, "reason": "ok",
}
REC2 = {
    "id": 2, "url": URL_A,
    "checked_at": "2026-10-02T04:40:02.000000+00:00",
    "elapsed_ms": 0, "status": "failure", "http_status": None,
    "reason": "timeout",
}
REC3 = {
    "id": 3, "url": URL_A,
    "checked_at": "2026-10-02T04:40:03.000000+00:00",
    "elapsed_ms": 7, "status": "failure", "http_status": 500,
    "reason": "http_status",
}
REC4 = {
    "id": 4, "url": URL_B,
    "checked_at": "2026-10-02T04:40:04.000000+00:00",
    "elapsed_ms": 60, "status": "success", "http_status": 200, "reason": "ok",
}
REC5 = {
    "id": 5, "url": URL_A,
    "checked_at": "2026-10-02T04:40:05.000000+00:00",
    "elapsed_ms": 2, "status": "success", "http_status": 200, "reason": "ok",
}

ALL_RECORDS = [REC1, REC2, REC3, REC4, REC5]

EMPTY_STATUS_SUMMARY = {"count": 0, "success_count": 0, "failure_count": 0}


def run_cli(db_path, *extra):
    """以子进程运行 healthcheck.py 命令行，返回 CompletedProcess。"""
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


def run_status_summary(db_path, *extra):
    return run_cli(db_path, "recent", "--status-summary", *extra)


def build_sample_db(path):
    """创建包含固定样本 5 条记录的临时数据库。"""
    conn = sqlite3.connect(str(path))
    conn.execute(SCHEMA_SQL)
    conn.executemany(
        "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
        "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (r["id"], r["url"], r["checked_at"], r["elapsed_ms"],
             r["status"], r["http_status"], r["reason"])
            for r in ALL_RECORDS
        ],
    )
    conn.commit()
    conn.close()


def make_empty_checks_db(path):
    """有效数据库，checks 表结构完整但没有任何记录。"""
    conn = sqlite3.connect(str(path))
    conn.execute(SCHEMA_SQL)
    conn.commit()
    conn.close()


def make_other_table_db(path):
    """有效数据库，但只有与 checks 无关的表。"""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE other_t (name TEXT NOT NULL, n INTEGER NOT NULL)")
    conn.execute("INSERT INTO other_t (name, n) VALUES ('kept', 42)")
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


class RecentStatusSummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-status-summary-"))
        self.db = self.tmp / "monitor.sqlite"
        build_sample_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_state_unchanged(self, before, label, db_path=None):
        """状态摘要查询不得改变目录文件集合、表结构或任何已有记录。"""
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

    def assert_status_summary_ok(self, proc, expected, label):
        """退出码 0、stderr 为空、单行 JSON 对象且恰含三个计数字段。"""
        self.assertEqual(
            proc.returncode, 0,
            f"输入 {label}：期望退出码 0，实际 {proc.returncode}，"
            f"stderr={proc.stderr!r}",
        )
        self.assertEqual(
            proc.stderr, "",
            f"输入 {label}：期望 stderr 为空，实际 {proc.stderr!r}",
        )
        self.assertEqual(
            proc.stdout.count("\n"), 1,
            f"输入 {label}：状态摘要应为一行 JSON 对象，实际 {proc.stdout!r}",
        )
        summary = json.loads(proc.stdout)
        self.assertEqual(
            set(summary.keys()), STATUS_SUMMARY_KEYS,
            f"输入 {label}：状态摘要应仅含字段 {STATUS_SUMMARY_KEYS}，"
            f"实际 {set(summary.keys())}",
        )
        for key in STATUS_SUMMARY_KEYS:
            self.assertIsInstance(
                summary[key], int,
                f"输入 {label}：{key} 应为整数，实际 {summary[key]!r}",
            )
        self.assertEqual(
            summary, expected,
            f"输入 {label}：期望状态摘要 {expected}，实际 {summary}",
        )
        return summary

    # ---- 基本计数：失败（含零耗时、各种原因）与成功记录均参与统计 ----

    def test_default_limit_counts_latest_five(self):
        label = "recent --status-summary（省略全部筛选）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_status_summary(self.db)
        # 默认 limit 5 覆盖全部样本：3 成功（id 1/4/5）、2 失败（id 2/3）
        self.assert_status_summary_ok(
            proc,
            {"count": 5, "success_count": 3, "failure_count": 2},
            label,
        )
        self.assert_state_unchanged(before, label)

    def test_limit_2_counts_latest_two(self):
        label = "recent --status-summary --limit 2"
        before = snapshot_state(self.tmp, self.db)
        proc = run_status_summary(self.db, "--limit", "2")
        # 最新两条为 id 5（success）与 id 4（success）
        self.assert_status_summary_ok(
            proc,
            {"count": 2, "success_count": 2, "failure_count": 0},
            label,
        )
        self.assert_state_unchanged(before, label)

    def test_url_filter_limit_2(self):
        label = f"recent --status-summary --url {URL_A!r} --limit 2"
        before = snapshot_state(self.tmp, self.db)
        proc = run_status_summary(self.db, "--url", URL_A, "--limit", "2")
        # 最新两条 A 记录为 id 5（success）与 id 3（failure）
        self.assert_status_summary_ok(
            proc,
            {"count": 2, "success_count": 1, "failure_count": 1},
            label,
        )
        self.assert_state_unchanged(before, label)

    def test_status_filter_intersection(self):
        label = "recent --status-summary --status failure"
        before = snapshot_state(self.tmp, self.db)
        proc = run_status_summary(self.db, "--status", "failure")
        # 仅两条 failure（id 2 零耗时 timeout、id 3 http_status）
        self.assert_status_summary_ok(
            proc,
            {"count": 2, "success_count": 0, "failure_count": 2},
            label,
        )
        self.assert_state_unchanged(before, label)

    def test_reason_filter_intersection(self):
        label = "recent --status-summary --reason timeout"
        before = snapshot_state(self.tmp, self.db)
        proc = run_status_summary(self.db, "--reason", "timeout")
        self.assert_status_summary_ok(
            proc,
            {"count": 1, "success_count": 0, "failure_count": 1},
            label,
        )
        self.assert_state_unchanged(before, label)

    def test_time_window_z_and_offset_equivalent(self):
        label = ("recent --status-summary --since ...Z "
                 "--until ...+00:00（端点包含）")
        before = snapshot_state(self.tmp, self.db)
        proc = run_status_summary(
            self.db,
            "--since", "2026-10-02T04:40:02Z",
            "--until", "2026-10-02T04:40:04.000000+00:00",
        )
        # 窗口含 id 2（failure）、id 3（failure）、id 4（success）
        self.assert_status_summary_ok(
            proc,
            {"count": 3, "success_count": 1, "failure_count": 2},
            label,
        )
        self.assert_state_unchanged(before, label)

    # ---- 四种空结果：三个计数均为 0、退出码 0 ----

    def test_valid_url_without_matching_records(self):
        label = f"recent --status-summary --url {URL_NO_MATCH!r}（无匹配记录）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_status_summary(self.db, "--url", URL_NO_MATCH)
        self.assert_status_summary_ok(proc, EMPTY_STATUS_SUMMARY, label)
        self.assert_state_unchanged(before, label)

    def test_empty_checks_table(self):
        db = self.tmp / "empty.sqlite"
        make_empty_checks_db(db)
        label = "recent --status-summary（空 checks 表）"
        before = snapshot_state(self.tmp, db)
        proc = run_status_summary(db)
        self.assert_status_summary_ok(proc, EMPTY_STATUS_SUMMARY, label)
        self.assert_state_unchanged(before, label, db)

    def test_database_with_only_other_tables(self):
        db = self.tmp / "other-only.sqlite"
        make_other_table_db(db)
        label = "recent --status-summary（只有其他表）"
        before = snapshot_state(self.tmp, db)
        proc = run_status_summary(db)
        self.assert_status_summary_ok(proc, EMPTY_STATUS_SUMMARY, label)
        after = snapshot_state(self.tmp, db)
        self.assertEqual(
            set(after[1]), {"other_t"},
            f"输入 {label}：不应创建 checks 表，实际表 {set(after[1])}",
        )
        self.assertEqual(after, before, f"输入 {label}：库内容被修改")

    def test_missing_database_and_parent(self):
        missing_root = self.tmp / "does-not-exist"
        db = missing_root / "nested" / "monitor.sqlite"
        label = "recent --status-summary（数据库与父目录均不存在）"
        before = snapshot_state(self.tmp, db)
        proc = run_status_summary(db)
        self.assert_status_summary_ok(proc, EMPTY_STATUS_SUMMARY, label)
        self.assertFalse(
            missing_root.exists(),
            f"输入 {label}：查询不应创建缺失的目录 {missing_root}",
        )
        self.assertFalse(
            db.exists(),
            f"输入 {label}：查询不应创建数据库文件 {db}",
        )
        self.assert_state_unchanged(before, label, db)

    # ---- 与 --summary 互斥：退出码 2、stdout 为空、不访问数据库 ----

    def test_summary_and_status_summary_are_mutually_exclusive(self):
        label = "recent --summary --status-summary（互斥）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_cli(self.db, "recent", "--summary", "--status-summary")
        self.assertEqual(
            proc.returncode, 2,
            f"输入 {label}：期望退出码 2，实际 {proc.returncode}",
        )
        self.assertEqual(
            proc.stdout, "",
            f"输入 {label}：期望 stdout 为空，实际 {proc.stdout!r}",
        )
        self.assertIn(
            "互斥", proc.stderr,
            f"输入 {label}：stderr 应指出两者互斥，实际 {proc.stderr!r}",
        )
        self.assertNotIn(
            "Traceback", proc.stderr,
            f"输入 {label}：stderr 不应包含 Python 回溯，实际 {proc.stderr!r}",
        )
        self.assert_state_unchanged(before, label)

    def test_mutual_exclusion_checked_before_database_access(self):
        """两个摘要选项同用时即使数据库路径不存在也报互斥，不创建任何文件。"""
        missing_root = self.tmp / "no-such-dir"
        db = missing_root / "monitor.sqlite"
        label = "recent --summary --status-summary（数据库不存在）"
        proc = run_cli(db, "recent", "--summary", "--status-summary")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(proc.stdout, "")
        self.assertIn("互斥", proc.stderr)
        self.assertFalse(
            missing_root.exists(),
            f"输入 {label}：互斥拒绝不应创建目录 {missing_root}",
        )

    # ---- 既有拒绝行为沿用 recent ----

    def test_directory_db_path_rejected(self):
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir()
        label = "recent --status-summary（数据库路径为目录）"
        before = snapshot_state(self.tmp, db_dir)
        proc = run_status_summary(db_dir)
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(proc.stdout, "")
        self.assertIn("目录", proc.stderr)
        self.assert_state_unchanged(before, label, db_dir)

    def test_invalid_sqlite_file_rejected(self):
        db = self.tmp / "not-a-db.sqlite"
        db.write_text("this is not sqlite", encoding="utf-8")
        label = "recent --status-summary（无效 SQLite 文件）"
        proc = run_status_summary(db)
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(proc.stdout, "")
        self.assertNotIn("Traceback", proc.stderr)

    def test_missing_columns_rejected(self):
        db = self.tmp / "missing-cols.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE checks (id INTEGER PRIMARY KEY, url TEXT)")
        conn.commit()
        conn.close()
        label = "recent --status-summary（checks 表缺字段）"
        proc = run_status_summary(db)
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(proc.stdout, "")
        self.assertIn("缺少字段", proc.stderr)

    def test_bad_checked_at_with_time_filter_rejected(self):
        db = self.tmp / "bad-ts.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
            "http_status, reason) VALUES (1, ?, 'not-a-time', 0, "
            "'success', 200, 'ok')",
            (URL_A,),
        )
        conn.commit()
        conn.close()
        label = "recent --status-summary --since（非法 checked_at）"
        proc = run_status_summary(
            db, "--since", "2026-10-02T04:40:00Z"
        )
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(proc.stdout, "")
        self.assertIn("checked_at", proc.stderr)

    # ---- 只读性：大小写不同的历史表名、可读不可写的数据库 ----

    def test_uppercase_table_name(self):
        db = self.tmp / "case.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute(SCHEMA_SQL.replace("checks", "CHECKS", 1))
        conn.execute(
            "INSERT INTO CHECKS (id, url, checked_at, elapsed_ms, status, "
            "http_status, reason) VALUES (1, ?, "
            "'2026-10-02T04:40:01.000000+00:00', 0, 'failure', NULL, "
            "'timeout')",
            (URL_A,),
        )
        conn.commit()
        conn.close()
        label = "recent --status-summary（表名 CHECKS）"
        before = snapshot_state(self.tmp, db)
        proc = run_status_summary(db)
        self.assert_status_summary_ok(
            proc,
            {"count": 1, "success_count": 0, "failure_count": 1},
            label,
        )
        self.assert_state_unchanged(before, label, db)

    def test_readonly_database_file(self):
        label = "recent --status-summary（只读数据库文件）"
        before = snapshot_state(self.tmp, self.db)
        self.db.chmod(0o444)
        try:
            proc = run_status_summary(self.db)
            self.assert_status_summary_ok(
                proc,
                {"count": 5, "success_count": 3, "failure_count": 2},
                label,
            )
        finally:
            self.db.chmod(0o644)
        self.assert_state_unchanged(before, label)

    # ---- 既有行为保持：普通 recent 与 --summary 输出不变 ----

    def test_plain_recent_and_summary_unchanged(self):
        label = "普通 recent 与 --summary（回归）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_cli(self.db, "recent", "--url", URL_A, "--limit", "2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        records = json.loads(proc.stdout)
        self.assertEqual([r["id"] for r in records], [5, 3])

        proc = run_cli(self.db, "recent", "--summary",
                       "--url", URL_A, "--limit", "2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout)
        self.assertEqual(
            summary,
            {"count": 2, "min_elapsed_ms": 2,
             "max_elapsed_ms": 7, "avg_elapsed_ms": 4.5},
            f"输入 {label}：--summary 耗时摘要应保持原样，实际 {summary}",
        )
        self.assert_state_unchanged(before, label)


if __name__ == "__main__":
    unittest.main(verbosity=2)
