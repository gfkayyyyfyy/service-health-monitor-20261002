#!/usr/bin/env python3
"""healthcheck.py check 连接异常路径的命令行回归测试（命令行 → SQLite）。

在“服务停止后连接失败”之外，补充两个已实现的本机边界：
  * 服务完整接收 GET 请求后不发送任何响应，直接关闭连接（对端断开）；
  * 服务立即返回无法解析的 HTTP 状态行（NOT_HTTP 后接两个 CRLF）再关闭。

两种输入都必须归类为已记录的探测失败：
退出码 1、stderr 为空、stdout 恰有一行 JSON，
status=failure、reason=connection_error、http_status=null，
且在探测超时之前完成（不得落入 timeout 分支）。

每个场景共同约定：
  * 登记合法本机 URL（/health?detail=1），端口由服务实际分配，
    路径与查询参数原样到达，服务只收到一次 GET；
  * 每次检查使用新的临时数据库，恰好新增一条与 stdout 逐字段一致的记录，
    保留原始 URL、带 UTC 时区的 checked_at、非负整数 elapsed_ms
    （不核对精确毫秒数或固定时间值）；
  * 随后 recent 只读查询：退出码 0、stderr 为空、返回仅含该记录的数组，
    数据库内容与服务请求计数均不变。

为便于从失败报告中定位问题，断言按三类分别落在独立方法中：
归类错误（assert_connection_error_classification）、重复请求
（assert_single_get_received）、持久化不一致（assert_persistence_and_recent）。

全部通过真实本机通信与公开命令入口（子进程运行 healthcheck.py）观察结果，
不伪造探测返回值；服务只绑定 127.0.0.1，不访问公网，
不依赖固定空闲端口或既有历史；每次运行自行准备并释放服务、子进程与
临时资源，断言失败时 tearDown 同样完成清理，重复运行互不影响。
运行：python3 -m unittest test_check_connection_error
"""

import json
import os
import pathlib
import shutil
import socket
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

# 登记的路径与查询参数：必须原样到达服务
TARGET = "/health?detail=1"
REQUEST_LINE = f"GET {TARGET} HTTP/1.1".encode("ascii")
HEADER_END = b"\r\n\r\n"

# 默认探测超时 1 秒：两个场景都应在此之前完成，而不是超时
DEFAULT_TIMEOUT_MS = 1000

MODE_CLOSE = "close"
MODE_MALFORMED = "malformed"

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


class _EdgeHandler(socketserver.BaseRequestHandler):
    """逐字节处理的最小 TCP 服务，不套用 HTTP 解析框架。

    完整接收一个 GET 请求（读到首部结束的空行）后按服务模式行事：
      * close：不发送任何字节，直接关闭连接；
      * malformed：立即发送 NOT_HTTP 后接两个 CRLF，再关闭连接。
    服务把完整收到的原始请求字节记入 server.requests，用于核对
    请求次数、方法以及路径与查询参数是否原样到达。
    """

    def handle(self):
        sock = self.request
        sock.settimeout(2.0)
        received = b""
        try:
            while HEADER_END not in received:
                chunk = sock.recv(4096)
                if not chunk:
                    # 对端未发完就断开：不计入请求数
                    return
                received += chunk
        except OSError:
            return

        # 完整接收请求后才登记，保证计数只反映完整 GET
        self.server.requests.append(received)

        if self.server.mode == MODE_MALFORMED:
            # 无法解析的“状态行”：NOT_HTTP 后接两个 CRLF，立即发出
            try:
                sock.sendall(b"NOT_HTTP" + HEADER_END)
            except OSError:
                pass

        # close 模式：什么都不发；两种模式最终都关闭连接（先发 FIN）
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _EdgeTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_server(mode):
    server = _EdgeTCPServer(("127.0.0.1", 0), _EdgeHandler)
    server.mode = mode
    server.requests = []  # 完整收到的原始请求字节列表
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class ConnectionErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-connerr-test-"))
        self._servers = []

    def tearDown(self):
        # 断言失败也必须释放本机服务与临时数据
        for server, thread in self._servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                os.chmod(os.path.join(root, name), stat.S_IRWXU)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start(self, mode):
        server, thread = start_server(mode)
        self._servers.append((server, thread))
        return server

    # ---- 分类一：归类（退出码 / stdout / status / reason / http_status）----

    def assert_connection_error_classification(self, proc, url):
        """归类为已记录的连接异常失败，返回 stdout 解析出的记录。"""
        self.assertEqual(
            proc.returncode, 1,
            "[归类] check 必须以退出码 1 结束（已记录的探测失败）",
        )
        self.assertEqual(proc.stderr, "", "[归类] stderr 必须为空")

        lines = proc.stdout.splitlines()
        self.assertEqual(
            len(lines), 1, "[归类] stdout 必须恰有一行 JSON"
        )
        record = json.loads(lines[0])
        self.assertEqual(
            sorted(record.keys()), sorted(RECORD_FIELDS),
            "[归类] JSON 字段集合必须与约定一致",
        )

        self.assertEqual(
            record["status"], "failure",
            "[归类] status 必须为 failure（不能误判成功）",
        )
        self.assertEqual(
            record["reason"], "connection_error",
            "[归类] reason 必须为 connection_error"
            "（既不能是 ok/http_status，也不能是 timeout）",
        )
        self.assertIsNone(
            record["http_status"],
            "[归类] 没有可解析的 HTTP 响应，http_status 必须为 null",
        )

        # 公共字段：原始 URL、UTC 时间、非负整数毫秒耗时
        self.assertEqual(record["id"], 1, "[持久化] 新库首条记录 id 必须为 1")
        self.assertEqual(
            record["url"], url, "[持久化] 必须保留登记的原始 URL"
        )
        self.assertIsInstance(record["elapsed_ms"], int)
        self.assertNotIsInstance(record["elapsed_ms"], bool)
        self.assertGreaterEqual(record["elapsed_ms"], 0)
        self.assertLess(
            record["elapsed_ms"], DEFAULT_TIMEOUT_MS,
            "[归类] 探测必须在超时前由连接异常结束，而不是等待超时",
        )
        checked_at = datetime.fromisoformat(record["checked_at"])
        self.assertIsNotNone(
            checked_at.tzinfo, "[持久化] checked_at 必须带时区"
        )
        self.assertEqual(
            checked_at.utcoffset(), timedelta(0),
            "[持久化] checked_at 必须是 UTC 时间",
        )
        return record

    # ---- 分类二：请求次数与路径 ----

    def assert_single_get_received(self, server):
        """服务恰好收到一次完整 GET，方法、路径与查询参数原样到达。"""
        requests = list(server.requests)
        self.assertEqual(
            len(requests), 1,
            f"[重复请求] 服务必须只收到一次 GET，实际收到 {len(requests)} 次",
        )
        raw = requests[0]
        self.assertTrue(
            raw.endswith(HEADER_END),
            "[重复请求] 服务必须在完整接收请求后才动作",
        )
        first_line = raw.split(HEADER_END, 1)[0].split(b"\r\n", 1)[0]
        self.assertEqual(
            first_line, REQUEST_LINE,
            "[重复请求] 请求方法与登记的路径、查询参数必须原样到达，"
            "不得改写或重试",
        )

    # ---- 分类三：持久化与 recent 只读往返 ----

    def assert_persistence_and_recent(self, db, record, server,
                                      requests_before):
        """恰好新增一条与 stdout 逐字段一致的记录；recent 只读返回同一条。"""
        rows = read_checks_rows(db)
        self.assertEqual(
            len(rows), 1, "[持久化] 数据库必须恰好新增一条记录"
        )
        self.assertEqual(
            rows_as_records(rows), [record],
            "[持久化] 落库记录必须与 stdout 逐字段一致",
        )

        proc = run_cli(db, "recent")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "", "[持久化] recent 的 stderr 必须为空")
        self.assertEqual(
            json.loads(proc.stdout), [record],
            "[持久化] recent 必须返回仅含该记录的数组",
        )

        # recent 之后数据库内容不变，服务请求计数不变（recent 不发请求）
        self.assertEqual(
            read_checks_rows(db), rows,
            "[持久化] recent 不得改变数据库内容",
        )
        self.assertEqual(
            list(server.requests), requests_before,
            "[持久化] recent 期间服务请求计数必须保持不变",
        )

    # ---- 场景一：完整接收请求后不发响应直接关闭 ----

    def test_server_closes_without_response_after_full_request(self):
        server = self._start(MODE_CLOSE)
        port = server.server_address[1]
        db = self.tmp / "close.sqlite"
        url = f"http://127.0.0.1:{port}{TARGET}"

        proc = run_cli(db, "check", "--url", url)
        record = self.assert_connection_error_classification(proc, url)

        self.assert_single_get_received(server)
        self.assert_persistence_and_recent(
            db, record, server, list(server.requests)
        )

    # ---- 场景二：返回无法解析的 HTTP 状态行 ----

    def test_server_sends_unparseable_status_line(self):
        server = self._start(MODE_MALFORMED)
        port = server.server_address[1]
        db = self.tmp / "malformed.sqlite"
        url = f"http://127.0.0.1:{port}{TARGET}"

        proc = run_cli(db, "check", "--url", url)
        record = self.assert_connection_error_classification(proc, url)

        self.assert_single_get_received(server)
        self.assert_persistence_and_recent(
            db, record, server, list(server.requests)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
