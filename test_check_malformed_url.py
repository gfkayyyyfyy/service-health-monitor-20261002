#!/usr/bin/env python3
"""healthcheck.py check 对畸形 URL 的统一错误处理回归测试。

覆盖 README 约定（非法 URL → 退出码 2、stdout 为空、stderr 说明原因、
无 Python 回溯；无效输入不发请求、不打开/创建数据库、不写记录、不建表）：

  * 未配对右方括号（如 http://[127.0.0.1:8765/）：
      - 数据库路径不存在且父目录也不存在 → 不产生任何文件或目录；
      - 数据库已存在且含 README 示例两条历史 → checks 旧记录、其他表原样不动；
  * 数据库路径指向目录时，URL 参数错误优先于数据库错误；
  * 其他结构解析 ValueError（http://[、http://[]/、http://[zzz]/ 等）同约定；
  * 即使端口上有存活的本机服务，畸形 URL 也不发出任何请求；
  * 对照：合法的 /health?detail=1 仍照常探测，路径与查询原样发送、原始 URL 落库。

全部使用独立临时目录与本机 127.0.0.1 可控服务，不访问公网。
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

# README 示例中的未配对右方括号输入
BAD_BRACKET_URL = "http://[127.0.0.1:8765/"
# 其他会在结构解析阶段报告 ValueError 的输入
OTHER_BAD_URLS = [
    "http://[::1/",
    "http://[",
    "http://[]/",
    "http://[zzz]/",
    "http://]127.0.0.1:8765/",
]

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

RECORD_1 = (
    1,
    "http://127.0.0.1:8765/health?detail=1",
    "2026-10-02T04:40:00.000000+00:00",
    3,
    "success",
    200,
    "ok",
)
RECORD_2 = (
    2,
    "http://127.0.0.1:8765/health?detail=1",
    "2026-10-02T04:40:05.000000+00:00",
    1,
    "failure",
    None,
    "connection_error",
)


def run_check(db_path, url, *extra):
    """以子进程运行 check，返回 CompletedProcess。"""
    cmd = [
        sys.executable, str(SCRIPT), "--db", str(db_path),
        "check", "--url", url, *extra,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def table_names(path):
    conn = sqlite3.connect(str(path))
    try:
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()


def checks_rows(path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(
            "SELECT id, url, checked_at, elapsed_ms, status, "
            "http_status, reason FROM checks ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def build_sample_db(path):
    """README 示例两条历史 + 一个其他表。"""
    conn = sqlite3.connect(str(path))
    conn.execute(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
        "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        RECORD_1,
    )
    conn.execute(
        "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
        "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        RECORD_2,
    )
    conn.execute("CREATE TABLE other_t (name TEXT, n INTEGER)")
    conn.execute("INSERT INTO other_t VALUES ('kept', 42)")
    conn.commit()
    conn.close()


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """记录请求路径，抑制日志噪音。"""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append(self.path)
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_server():
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _RecordingHandler
    )
    server.daemon_threads = True
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class MalformedUrlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-badurl-test-"))
        self._servers = []

    def tearDown(self):
        for server, thread in self._servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                os.chmod(os.path.join(root, name), stat.S_IRWXU)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start(self):
        server, thread = start_server()
        self._servers.append((server, thread))
        return server

    # ---- 公共断言 ----

    def assert_url_error(self, proc):
        """非法 URL 约定：退出码 2、stdout 空、stderr 含固定前缀与“非法 URL”，
        且不出现 Python 回溯。"""
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("healthcheck: error:", proc.stderr)
        self.assertIn("非法 URL", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    # ---- 数据库状态一：路径与父目录均不存在 ----

    def test_missing_db_and_parent_nothing_created(self):
        missing_root = self.tmp / "does-not-exist"
        db = missing_root / "nested" / "monitor.sqlite"
        self.assertFalse(os.path.exists(missing_root))

        proc = run_check(db, BAD_BRACKET_URL)
        self.assert_url_error(proc)

        # 不产生数据库文件，也不产生任何（父）目录或 -wal/-journal 旁路文件
        self.assertFalse(os.path.exists(missing_root))
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_other_structural_valueerrors_with_missing_db(self):
        db = self.tmp / "new" / "monitor.sqlite"
        for url in OTHER_BAD_URLS:
            with self.subTest(url=url):
                proc = run_check(db, url)
                self.assert_url_error(proc)
        # 所有畸形输入均未创建任何文件或目录
        self.assertFalse((self.tmp / "new").exists())
        self.assertEqual(list(self.tmp.iterdir()), [])

    # ---- 数据库状态二：已存在且含两条 README 历史 ----

    def test_existing_db_records_and_tables_untouched(self):
        db = self.tmp / "monitor.sqlite"
        build_sample_db(db)
        tables_before = table_names(db)
        rows_before = checks_rows(db)
        self.assertEqual(rows_before, [RECORD_1, RECORD_2])

        proc = run_check(db, BAD_BRACKET_URL)
        self.assert_url_error(proc)

        # checks 旧记录数量与字段值不变
        self.assertEqual(checks_rows(db), rows_before)
        # 表集合不变（不新建 checks 或其他表）
        self.assertEqual(table_names(db), tables_before)
        # 其他表的数据保持原样
        conn = sqlite3.connect(str(db))
        try:
            self.assertEqual(
                conn.execute("SELECT name, n FROM other_t").fetchall(),
                [("kept", 42)],
            )
        finally:
            conn.close()
        # 不产生 -wal/-journal 旁路文件
        self.assertEqual(
            {p.name for p in self.tmp.iterdir()}, {"monitor.sqlite"}
        )

    def test_other_structural_valueerrors_leave_existing_db_untouched(self):
        db = self.tmp / "monitor.sqlite"
        build_sample_db(db)
        rows_before = checks_rows(db)
        tables_before = table_names(db)

        for url in OTHER_BAD_URLS:
            with self.subTest(url=url):
                proc = run_check(db, url)
                self.assert_url_error(proc)
                self.assertEqual(checks_rows(db), rows_before)
                self.assertEqual(table_names(db), tables_before)

    # ---- URL 错误优先于数据库错误 ----

    def test_directory_db_path_reports_url_error_first(self):
        # --db 指向一个目录；畸形 URL 必须优先报 URL 参数错误
        proc = run_check(self.tmp, BAD_BRACKET_URL)
        self.assert_url_error(proc)

    # ---- 不发任何网络请求 ----

    def test_no_request_sent_even_with_live_local_server(self):
        server = self._start()
        port = server.server_address[1]
        db = self.tmp / "monitor.sqlite"
        # 畸形输入中使用存活服务的端口；它仍必须在发请求前被拒绝
        url = f"http://[127.0.0.1:{port}/"

        proc = run_check(db, url)
        self.assert_url_error(proc)

        self.assertEqual(server.requests, [])
        self.assertFalse(db.exists())

    # ---- 对照：合法 URL 行为保持不变 ----

    def test_valid_path_and_query_still_probed_and_saved_raw(self):
        server = self._start()
        port = server.server_address[1]
        db = self.tmp / "monitor.sqlite"
        url = f"http://127.0.0.1:{port}/health?detail=1"

        proc = run_check(db, url)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        record = json.loads(proc.stdout)
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["http_status"], 200)
        self.assertEqual(record["reason"], "ok")
        # 原始 URL 照常保存
        self.assertEqual(record["url"], url)

        # 路径与查询参数照常发送，且只有一次请求
        self.assertEqual(server.requests, ["/health?detail=1"])

        # 落库内容与 stdout 一致
        rows = checks_rows(db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], url)  # url 字段为原始输入
        self.assertEqual(rows[0][4], "success")
        self.assertEqual(rows[0][5], 200)
        self.assertEqual(rows[0][6], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
