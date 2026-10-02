#!/usr/bin/env python3
"""healthcheck.py check 对畸形 URL（结构解析 ValueError）的回归测试。

约定（见 README）：非法 URL 属于参数错误 —— 退出码 2、标准输出为空、
标准错误含 "healthcheck: error: " 与“非法 URL”、不出现 Python 回溯；
且在发出任何网络请求或打开数据库之前即被拒绝：
  * 数据库路径不存在（父目录也不存在）时，处理后不得出现任何文件或目录；
  * 数据库已存在时，checks 表、旧记录及其他表的数据保持原样；
  * 数据库路径指向目录时，仍优先报告 URL 参数错误而非数据库错误。

全部使用独立临时目录与本机 127.0.0.1 回环服务（仅用于计数请求，
确认畸形输入不会触发任何网络请求），不访问公网。
运行：python3 test_check_malformed_url.py
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

# README 示例中的两条历史记录
RECORD_1 = (
    1, "http://127.0.0.1:8765/health?detail=1",
    "2026-10-02T04:40:00.000000+00:00", 3, "success", 200, "ok",
)
RECORD_2 = (
    2, "http://127.0.0.1:8765/health?detail=1",
    "2026-10-02T04:40:05.000000+00:00", 1, "failure", None,
    "connection_error",
)


class CountingHandler(http.server.BaseHTTPRequestHandler):
    """只记录收到的请求数，用于断言畸形 URL 不会触发任何请求。"""

    requests_received = 0

    def do_GET(self):
        type(self).requests_received += 1
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def run_check(db_path, url):
    """以子进程运行 check，返回 CompletedProcess。"""
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path),
           "check", "--url", url]
    return subprocess.run(cmd, capture_output=True, text=True)


def insert_legacy_records(path):
    conn = sqlite3.connect(str(path))
    conn.execute(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
        "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)", RECORD_1)
    conn.execute(
        "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
        "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)", RECORD_2)
    conn.execute("CREATE TABLE other_t (name TEXT, n INTEGER)")
    conn.execute("INSERT INTO other_t VALUES ('kept', 42)")
    conn.commit()
    conn.close()


def snapshot_db(path):
    """返回 (表名集合, checks 全部行, other_t 全部行) 用于前后对比。"""
    conn = sqlite3.connect(str(path))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        checks = conn.execute(
            "SELECT id, url, checked_at, elapsed_ms, status, "
            "http_status, reason FROM checks ORDER BY id").fetchall()
        other = conn.execute("SELECT name, n FROM other_t").fetchall()
    finally:
        conn.close()
    return tables, checks, other


class MalformedUrlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-badurl-"))
        # 回环计数服务：任何发往它的请求都会被记录
        CountingHandler.requests_received = 0
        self.server = http.server.HTTPServer(
            ("127.0.0.1", 0), CountingHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        # 未配对右方括号的畸形 URL（端口指向计数服务）
        self.bad_url = f"http://[127.0.0.1:{self.port}/"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                os.chmod(os.path.join(root, name), stat.S_IRWXU)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_param_error(self, proc):
        """统一的参数错误约定：退出码 2、stdout 空、stderr 说明、无回溯。"""
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(proc.stdout, "")
        self.assertIn("healthcheck: error: ", proc.stderr)
        self.assertIn("非法 URL", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def assert_no_request_made(self):
        self.assertEqual(CountingHandler.requests_received, 0)

    # ---- 数据库路径不存在（父目录也不存在）：不得创建任何文件或目录 ----

    def test_missing_db_and_parent_nothing_created(self):
        missing_parent = self.tmp / "no-such-dir"
        db = missing_parent / "nested" / "monitor.sqlite"

        proc = run_check(db, self.bad_url)
        self.assert_param_error(proc)
        self.assert_no_request_made()
        # 连父目录都不得出现
        self.assertFalse(os.path.exists(missing_parent))
        self.assertEqual({p.name for p in self.tmp.iterdir()}, set())

    def test_other_structural_value_errors_same_convention(self):
        # 其他由 URL 结构解析报告 ValueError 的输入按同一约定处理
        db = self.tmp / "gone" / "monitor.sqlite"
        for bad in (f"http://[127.0.0.1:{self.port}/",
                    "http://[::1",
                    f"http://[127.0.0.1:{self.port}"):
            with self.subTest(url=bad):
                proc = run_check(db, bad)
                self.assert_param_error(proc)
        self.assert_no_request_made()
        self.assertFalse(os.path.exists(self.tmp / "gone"))

    # ---- 数据库已存在且含两条旧记录：数据保持原样 ----

    def test_existing_db_records_and_tables_untouched(self):
        db = self.tmp / "monitor.sqlite"
        insert_legacy_records(db)
        before = snapshot_db(db)
        self.assertEqual(len(before[1]), 2)

        proc = run_check(db, self.bad_url)
        self.assert_param_error(proc)
        self.assert_no_request_made()

        after = snapshot_db(db)
        # 表集合（无新增 checks 以外的表、无重建）、旧记录与其他表数据不变
        self.assertEqual(after, before)
        self.assertEqual(after[1], [RECORD_1, RECORD_2])
        self.assertEqual(after[2], [("kept", 42)])
        # 不产生 -wal/-journal 等旁路文件
        self.assertEqual(
            {p.name for p in self.tmp.iterdir()}, {"monitor.sqlite"})

    # ---- 数据库路径指向目录：仍优先报告 URL 参数错误 ----

    def test_db_path_is_directory_still_url_error(self):
        proc = run_check(self.tmp, self.bad_url)
        self.assert_param_error(proc)
        self.assertNotIn("数据库", proc.stderr)
        self.assert_no_request_made()

    # ---- 对照：合法 URL 行为不受本次修改影响 ----

    def test_valid_url_still_probed_and_recorded(self):
        db = self.tmp / "ok.sqlite"
        url = f"http://127.0.0.1:{self.port}/health?detail=1"
        proc = run_check(db, url)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        record = json.loads(proc.stdout)
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["http_status"], 200)
        self.assertEqual(record["reason"], "ok")
        # 原始 URL（含路径与查询参数）照常保存
        self.assertEqual(record["url"], url)
        self.assertEqual(CountingHandler.requests_received, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
