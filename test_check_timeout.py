#!/usr/bin/env python3
"""healthcheck.py check 超时路径的回归测试（命令行 → SQLite）。

覆盖两类本机场景：
  * 慢响应服务（>=0.6s 才发完整响应头）+ --timeout 0.2 → failure/timeout；
  * 立即返回 200 的服务 → success/ok（同一流程的对照）。

全部使用独立临时目录中的数据库与本机 127.0.0.1 服务，
不访问公网、不依赖既有数据库文件。
运行：python3 test_check_timeout.py
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
import time
import unittest
from datetime import datetime, timedelta

SCRIPT = pathlib.Path(__file__).resolve().parent / "healthcheck.py"

SLOW_DELAY = 0.6
CHECK_TIMEOUT = "0.2"

RECORD_FIELDS = ("id", "url", "checked_at", "elapsed_ms",
                 "status", "http_status", "reason")


def run_cli(db_path, *args):
    """以子进程运行 healthcheck.py，返回 CompletedProcess。"""
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *args]
    return subprocess.run(cmd, capture_output=True, text=True)


def read_checks_rows(db_path):
    """直接读取 checks 表全部行（按 id 升序），用于核对落库内容。"""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT id, url, checked_at, elapsed_ms, status, "
            "http_status, reason FROM checks ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def rows_as_records(rows):
    return [dict(zip(RECORD_FIELDS, row)) for row in rows]


class _BaseHandler(http.server.BaseHTTPRequestHandler):
    """记录请求并抑制客户端断开后的写错误与日志噪音。"""

    def log_message(self, *args):
        pass

    def _count_and_mark(self):
        self.server.requests.append(self.path)

    def _send_ok(self):
        try:
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            # 客户端已因超时断开：忽略写失败，不影响请求计数
            pass


class _SlowHandler(_BaseHandler):
    """收到 GET 后至少延迟 SLOW_DELAY 秒才发送完整响应头。"""

    def do_GET(self):
        self._count_and_mark()
        time.sleep(SLOW_DELAY)
        self._send_ok()


class _FastHandler(_BaseHandler):
    """收到 GET 后立即返回 200。"""

    def do_GET(self):
        self._count_and_mark()
        self._send_ok()


def start_server(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    server.requests = []  # 已收到的请求路径列表，用于核对请求次数与目标
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class CheckTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-timeout-test-"))
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

    def _start(self, handler):
        server, thread = start_server(handler)
        self._servers.append((server, thread))
        return server

    # ---- 公共断言 ----

    def assert_single_json_line(self, proc):
        """stdout 恰好是一条可解析的 JSON 记录。"""
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1, proc.stdout)
        record = json.loads(lines[0])
        self.assertEqual(sorted(record.keys()), sorted(RECORD_FIELDS))
        return record

    def assert_common_record_fields(self, record, url):
        self.assertEqual(record["id"], 1)
        self.assertEqual(record["url"], url)
        self.assertIsInstance(record["elapsed_ms"], int)
        self.assertNotIsInstance(record["elapsed_ms"], bool)
        self.assertGreaterEqual(record["elapsed_ms"], 0)
        checked_at = datetime.fromisoformat(record["checked_at"])
        self.assertIsNotNone(checked_at.tzinfo)
        self.assertEqual(checked_at.utcoffset(), timedelta(0))

    def assert_db_matches(self, db, record):
        """数据库恰含一条记录，且与 check 的 stdout 完全一致。"""
        rows = read_checks_rows(db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows_as_records(rows), [record])

    def assert_recent_roundtrip(self, db, record, server):
        """recent 只读返回同一记录，且不改库、不发新请求。"""
        rows_before = read_checks_rows(db)
        requests_before = list(server.requests)

        proc = run_cli(db, "recent")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(json.loads(proc.stdout), [record])

        # 查询前后记录数与内容不变，本地服务未收到新请求
        self.assertEqual(read_checks_rows(db), rows_before)
        self.assertEqual(server.requests, requests_before)

    # ---- 超时场景 ----

    def test_check_timeout_records_failure(self):
        server = self._start(_SlowHandler)
        port = server.server_address[1]
        db = self.tmp / "timeout.sqlite"
        url = f"http://127.0.0.1:{port}/slow?case=timeout"

        proc = run_cli(
            db, "check", "--url", url, "--timeout", CHECK_TIMEOUT
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(proc.stderr, "")

        record = self.assert_single_json_line(proc)
        self.assertEqual(record["status"], "failure")
        # 必须是 timeout：得到其他失败原因（如 connection_error）即失败
        self.assertEqual(record["reason"], "timeout")
        self.assertIsNone(record["http_status"])
        self.assert_common_record_fields(record, url)

        # 只发了一次请求、只访问了登记的目标，不重试
        self.assertEqual(server.requests, ["/slow?case=timeout"])

        # 超时只新增一条 checks 记录，内容与 stdout 一致
        self.assert_db_matches(db, record)

        # recent 只读看到同一条记录，库与请求计数均不变
        self.assert_recent_roundtrip(db, record, server)

    # ---- 对照场景：及时返回 200 ----

    def test_check_fast_ok_records_success(self):
        server = self._start(_FastHandler)
        port = server.server_address[1]
        db = self.tmp / "fast.sqlite"
        url = f"http://127.0.0.1:{port}/slow?case=timeout"

        proc = run_cli(
            db, "check", "--url", url, "--timeout", CHECK_TIMEOUT
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")

        record = self.assert_single_json_line(proc)
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["reason"], "ok")
        self.assertEqual(record["http_status"], 200)
        self.assert_common_record_fields(record, url)

        # 同样只发一次请求、只落一条记录
        self.assertEqual(server.requests, ["/slow?case=timeout"])
        self.assert_db_matches(db, record)

        self.assert_recent_roundtrip(db, record, server)


if __name__ == "__main__":
    unittest.main(verbosity=2)
