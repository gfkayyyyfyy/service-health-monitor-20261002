#!/usr/bin/env python3
"""healthcheck.py recent 只读行为的回归测试。

全部使用独立临时目录中的本地数据，不访问公网、不依赖既有历史文件。
运行：python3 test_healthcheck.py
"""

import http.server
import json
import os
import pathlib
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "healthcheck.py"

LEGACY_SCHEMA = """
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

RECORD_1 = {
    "id": 1,
    "url": "http://127.0.0.1:8765/health?detail=1",
    "checked_at": "2026-10-02T04:40:00.000000+00:00",
    "elapsed_ms": 3,
    "status": "success",
    "http_status": 200,
    "reason": "ok",
}
RECORD_2 = {
    "id": 2,
    "url": "http://127.0.0.1:8765/health?detail=1",
    "checked_at": "2026-10-02T04:40:05.000000+00:00",
    "elapsed_ms": 1,
    "status": "failure",
    "http_status": None,
    "reason": "connection_error",
}


def run_recent(db_path, *extra, expect_dir=None):
    """以子进程运行 recent，返回到的 CompletedProcess。"""
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), "recent", *extra]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=expect_dir)


def make_valid_db(path, with_checks=False, with_other=False):
    conn = sqlite3.connect(str(path))
    if with_checks:
        conn.execute(LEGACY_SCHEMA)
    if with_other:
        conn.execute("CREATE TABLE other_t (name TEXT, n INTEGER)")
        conn.execute("INSERT INTO other_t VALUES ('kept', 42)")
    conn.commit()
    conn.close()


def insert_legacy_records(path):
    conn = sqlite3.connect(str(path))
    conn.execute(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
        "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            RECORD_1["id"], RECORD_1["url"], RECORD_1["checked_at"],
            RECORD_1["elapsed_ms"], RECORD_1["status"],
            RECORD_1["http_status"], RECORD_1["reason"],
        ),
    )
    conn.execute(
        "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
        "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            RECORD_2["id"], RECORD_2["url"], RECORD_2["checked_at"],
            RECORD_2["elapsed_ms"], RECORD_2["status"],
            RECORD_2["http_status"], RECORD_2["reason"],
        ),
    )
    conn.commit()
    conn.close()


def table_names(path):
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


class RecentReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-test-"))

    def tearDown(self):
        # 清理可能被设为只读的文件，避免 rmtree 失败
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                os.chmod(os.path.join(root, name), stat.S_IRWXU)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 样本一：有效数据库但没有 checks 表 ----

    def test_sample1_empty_valid_db_no_checks_table(self):
        db = self.tmp / "empty.db"
        make_valid_db(db)  # 有效但完全为空的 SQLite 文件
        self.assertEqual(table_names(db), set())

        proc = run_recent(db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "[]\n")
        self.assertEqual(proc.stderr, "")
        # 查询后仍然没有 checks 表
        self.assertEqual(table_names(db), set())
        # 不应产生 -wal/-journal 旁路文件
        self.assertEqual(
            {p.name for p in self.tmp.iterdir()}, {"empty.db"}
        )

    def test_sample1_db_with_only_other_table_untouched(self):
        db = self.tmp / "other.db"
        make_valid_db(db, with_other=True)
        before = table_names(db)

        proc = run_recent(db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "[]\n")

        # 原有表与数据保持不变，且未新增 checks 表
        self.assertEqual(table_names(db), before)
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute("SELECT name, n FROM other_t").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [("kept", 42)])

    # ---- 样本二：两条旧版 check 记录 ----

    def _assert_sample2_results(self, db):
        proc = run_recent(db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = json.loads(proc.stdout)
        self.assertEqual(len(records), 2)
        # 失败记录(id=2)排在第一
        self.assertEqual(records[0], RECORD_2)
        self.assertEqual(records[1], RECORD_1)

        proc1 = run_recent(db, "--limit", "1")
        self.assertEqual(proc1.returncode, 0, proc1.stderr)
        records1 = json.loads(proc1.stdout)
        self.assertEqual(records1, [RECORD_2])

    def test_sample2_legacy_records_default_and_limit1(self):
        db = self.tmp / "monitor.sqlite"
        insert_legacy_records(db)
        self._assert_sample2_results(db)

    def test_sample2_same_results_when_db_readonly(self):
        db = self.tmp / "monitor-ro.sqlite"
        insert_legacy_records(db)
        os.chmod(db, stat.S_IRUSR)  # 400：可读不可写
        try:
            # 历史中存在失败记录，查询仍以退出码 0 成功
            self._assert_sample2_results(db)
        finally:
            os.chmod(db, stat.S_IRWXU)
        # 只读查询后内容未变（AUTOINCREMENT 自带 sqlite_sequence 表）
        self.assertIn("checks", table_names(db))

    def test_checks_table_without_rows_returns_empty(self):
        db = self.tmp / "norows.db"
        make_valid_db(db, with_checks=True)
        proc = run_recent(db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "[]\n")

    def test_default_limit_is_five_descending(self):
        db = self.tmp / "seven.db"
        conn = sqlite3.connect(str(db))
        conn.execute(LEGACY_SCHEMA)
        for i in range(1, 8):
            conn.execute(
                "INSERT INTO checks (id, url, checked_at, elapsed_ms, "
                "status, http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (i, f"http://127.0.0.1:900{i}/", f"2026-10-02T00:00:0{i}.000000+00:00",
                 i, "success", 200, "ok"),
            )
        conn.commit()
        conn.close()

        proc = run_recent(db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = json.loads(proc.stdout)
        self.assertEqual([r["id"] for r in records], [7, 6, 5, 4, 3])

    # ---- 文件/目录不存在：不创建任何东西 ----

    def test_missing_db_and_missing_parent_returns_empty(self):
        missing_parent = self.tmp / "does-not-exist"
        db = missing_parent / "nested" / "monitor.sqlite"
        proc = run_recent(db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "[]\n")
        self.assertEqual(proc.stderr, "")
        # 父目录也不得被创建
        self.assertFalse(os.path.exists(missing_parent))

    # ---- 错误边界：退出码 2、stdout 为空、stderr 说明原因 ----

    def assert_read_error(self, proc):
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(proc.stdout, "")
        self.assertNotEqual(proc.stderr, "")

    def test_path_is_a_directory(self):
        proc = run_recent(self.tmp)
        self.assert_read_error(proc)

    def test_file_is_not_sqlite(self):
        db = self.tmp / "garbage.db"
        db.write_bytes(b"this is definitely not a sqlite database\n")
        proc = run_recent(db)
        self.assert_read_error(proc)

    def test_no_read_permission(self):
        if os.geteuid() == 0:
            self.skipTest("root 绕过文件读权限，无法验证")
        db = self.tmp / "noread.db"
        insert_legacy_records(db)
        os.chmod(db, 0)
        try:
            proc = run_recent(db)
            self.assert_read_error(proc)
        finally:
            os.chmod(db, stat.S_IRWXU)

    def test_checks_table_missing_required_columns(self):
        db = self.tmp / "badcols.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE checks (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        proc = run_recent(db)
        self.assert_read_error(proc)
        # 出错不得修改/重建文件：坏表结构保持原样
        conn = sqlite3.connect(str(db))
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(checks)")}
        finally:
            conn.close()
        self.assertEqual(cols, {"id"})

    def test_invalid_limits_exit_2_with_empty_stdout(self):
        db = self.tmp / "monitor.sqlite"
        insert_legacy_records(db)
        for bad in ("0", "-1", "1.5", "abc", "+1 "):
            with self.subTest(limit=bad):
                proc = run_recent(db, "--limit", bad)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertNotEqual(proc.stderr, "")

    # ---- check 行为保持不变（本机 HTTP，成功落库 / 失败也落库）----

    def test_check_persistence_and_exit_codes_preserved(self):
        db = self.tmp / "check.sqlite"

        server = http.server.HTTPServer(
            ("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler
        )
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{port}/"
            proc_ok = subprocess.run(
                [sys.executable, str(SCRIPT), "--db", str(db),
                 "check", "--url", url],
                capture_output=True, text=True,
            )
            self.assertEqual(proc_ok.returncode, 0, proc_ok.stderr)
            rec_ok = json.loads(proc_ok.stdout)
            self.assertEqual(rec_ok["status"], "success")
            self.assertEqual(rec_ok["http_status"], 200)
            self.assertEqual(rec_ok["reason"], "ok")
            self.assertEqual(rec_ok["url"], url)
            self.assertEqual(rec_ok["id"], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        # 服务已停止：连接失败仍应记录，退出码 1
        url_dead = f"http://127.0.0.1:{port}/dead"
        proc_fail = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(db),
             "check", "--url", url_dead],
            capture_output=True, text=True,
        )
        self.assertEqual(proc_fail.returncode, 1, proc_fail.stderr)
        rec_fail = json.loads(proc_fail.stdout)
        self.assertEqual(rec_fail["status"], "failure")
        self.assertIsNone(rec_fail["http_status"])
        self.assertEqual(rec_fail["reason"], "connection_error")
        self.assertEqual(rec_fail["url"], url_dead)
        self.assertEqual(rec_fail["id"], 2)

        # recent 看到失败在前
        proc = run_recent(db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = json.loads(proc.stdout)
        self.assertEqual([r["id"] for r in records], [2, 1])
        self.assertEqual(records[0]["status"], "failure")


if __name__ == "__main__":
    unittest.main(verbosity=2)
