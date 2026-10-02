#!/usr/bin/env python3
"""healthcheck.py check 连接异常路径的命令行回归测试（真实本机通信 → SQLite）。

覆盖两个已实现的边界（服务停止导致的连接拒绝已由其他测试覆盖）：
  * 服务完整接收 GET 请求后，不发送任何响应，直接关闭连接；
  * 服务接受请求后立即发送无法解析的 HTTP 状态行
    （NOT_HTTP 后接两个 CRLF），随后关闭连接。

两种输入下 check 都应以退出码 1 结束、stderr 为空、stdout 恰有一行 JSON：
status=failure、reason=connection_error、http_status=null；
数据库恰好新增一条与 stdout 逐字段一致的记录，recent 只读返回该记录且
库内容与服务请求计数保持不变。

全部使用临时目录中的全新数据库与 127.0.0.1 临时端口服务，不访问公网、
不依赖固定空闲端口或既有历史。

运行：python3 -m unittest test_check_connection_error
"""

import json
import os
import pathlib
import shutil
import socketserver
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta

SCRIPT = pathlib.Path(__file__).resolve().parent / "healthcheck.py"

# 本机回环通信在毫秒级即可完成，给足余量以与 timeout 归类明确区分
CHECK_TIMEOUT = "2"

# 登记到 check 的目标路径（含查询参数），服务端必须原样收到
TARGET_PATH = "/health?detail=1"
EXPECTED_REQUEST_LINE = f"GET {TARGET_PATH} HTTP/1.1"

# 畸形响应：无法解析的状态行 + 两个 CRLF
MALFORMED_REPLY = b"NOT_HTTP\r\n\r\n"

RECORD_FIELDS = ("id", "url", "checked_at", "elapsed_ms",
                 "status", "http_status", "reason")


def run_cli(db_path, *args):
    """以子进程运行 healthcheck.py 的公开命令入口，返回 CompletedProcess。"""
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


class _RawHttpHandler(socketserver.StreamRequestHandler):
    """逐字节读取完整 HTTP 请求后，按服务端模式制造连接异常。

    记录的是原始请求行（如 "GET /health?detail=1 HTTP/1.1"），
    可同时核对请求方法、次数以及路径与查询参数是否原样到达。
    """

    def _read_full_request(self):
        """读取直到请求头结束；若对端中途断开则返回 None。

        GET 无请求体；为稳妥起见仍按 Content-Length 排空可能的请求体，
        确保本端是在完整接收请求后才关闭连接。
        """
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            chunk = self.rfile.read(1)
            if not chunk:
                return None
            buf += chunk
        head, _, extra = bytes(buf).partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        request_line = lines[0].decode("iso-8859-1")

        content_length = 0
        for header in lines[1:]:
            if header.lower().startswith(b"content-length:"):
                content_length = int(header.split(b":", 1)[1].strip())
        remaining = content_length - len(extra)
        if remaining > 0:
            self.rfile.read(remaining)
        return request_line

    def handle(self):
        try:
            request_line = self._read_full_request()
        except (OSError, ValueError):
            return
        if request_line is None:
            return
        self.server.requests.append(request_line)

        if self.server.mode == "disconnect":
            # 已完整接收请求：不发送任何响应，handle 返回即关闭连接
            return

        # malformed：立即写出无法解析的状态行，再关闭连接
        try:
            self.wfile.write(MALFORMED_REPLY)
            self.wfile.flush()
        except OSError:
            pass


class _RawTcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, mode):
        super().__init__(("127.0.0.1", 0), _RawHttpHandler)
        # mode: "disconnect"（收完请求直接关闭）或 "malformed"（畸形状态行）
        self.mode = mode
        self.requests = []  # 已收到的原始请求行列表，核对请求次数与目标


class ConnectionErrorCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-connerr-test-"))
        self._servers = []

    def tearDown(self):
        # 即使断言失败，也要释放服务线程、端口与临时目录
        for server, thread in self._servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                os.chmod(os.path.join(root, name), stat.S_IRWXU)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start_server(self, mode):
        server = _RawTcpServer(mode)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._servers.append((server, thread))
        return server

    # ---- 公共断言 ----

    def assert_single_json_line(self, proc):
        """stdout 恰好是一行可解析、字段完整的 JSON 记录。"""
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1, f"stdout 应恰有一行 JSON: {proc.stdout!r}")
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
        """数据库恰含一条记录，且与 check 的 stdout 逐字段一致。"""
        rows = read_checks_rows(db)
        self.assertEqual(len(rows), 1, f"应恰好新增一条记录: {rows!r}")
        self.assertEqual(rows_as_records(rows), [record])

    def assert_recent_roundtrip(self, db, record, server):
        """recent 只读返回同一记录，且不改库、不发新请求。"""
        rows_before = read_checks_rows(db)
        requests_before = list(server.requests)

        proc = run_cli(db, "recent")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(json.loads(proc.stdout), [record])

        # recent 前后数据库内容与服务请求计数均保持不变
        self.assertEqual(read_checks_rows(db), rows_before)
        self.assertEqual(server.requests, requests_before)

    def _run_connection_error_scenario(self, mode, db_name):
        """两个场景共用的完整核对流程；失败信息标明出错环节。"""
        server = self._start_server(mode)
        port = server.server_address[1]
        db = self.tmp / db_name
        url = f"http://127.0.0.1:{port}{TARGET_PATH}"

        proc = run_cli(db, "check", "--url", url, "--timeout", CHECK_TIMEOUT)

        # ---- 归类：必须判为 connection_error，而非 timeout 等其他原因 ----
        self.assertEqual(proc.returncode, 1, f"退出码应为 1: {proc.stderr!r}")
        self.assertEqual(proc.stderr, "", "stderr 应为空")
        record = self.assert_single_json_line(proc)
        self.assertEqual(record["status"], "failure", "status 应为 failure")
        self.assertEqual(
            record["reason"], "connection_error",
            f"reason 应为 connection_error（归类错误）: {record!r}",
        )
        self.assertIsNone(record["http_status"], "http_status 应为 null")
        self.assert_common_record_fields(record, url)

        # ---- 请求次数与目标：服务只收到一次 GET，路径与查询原样到达 ----
        self.assertEqual(
            server.requests, [EXPECTED_REQUEST_LINE],
            "服务应只收到一次 GET，且路径与查询参数原样到达（重复请求或目标不符）",
        )

        # ---- 持久化：恰好新增一条与 stdout 逐字段一致的记录 ----
        self.assert_db_matches(db, record)

        # ---- recent 只读往返：退出码 0、stderr 空、内容一致且无副作用 ----
        self.assert_recent_roundtrip(db, record, server)

    # ---- 场景一：完整接收请求后不发响应直接关闭连接 ----

    def test_check_close_without_response_is_connection_error(self):
        self._run_connection_error_scenario(
            "disconnect", "close_without_response.sqlite"
        )

    # ---- 场景二：返回无法解析的 HTTP 状态行 ----

    def test_check_malformed_status_line_is_connection_error(self):
        self._run_connection_error_scenario(
            "malformed", "malformed_status_line.sqlite"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
