#!/usr/bin/env python3
"""healthcheck.py check 响应头超过解析限制时的命令行回归测试。

现有产品代码把 HTTP 响应解析异常（http.client.HTTPException 一族）统一
归为 connection_error，既有测试已覆盖对端直接断开与无法解析的状态行；
本模块只补充「响应头超过 http.client 解析上限」的两个本机边界，确认这类
失败仍按既有协议保存并可经 recent 查询：

  1. 单条响应头过长：X-Pad 的值是连续 70000 个 ASCII 字符 'a'
     （整行超过 http.client._MAXLINE=65536，读首行/首部时抛 LineTooLong）；
  2. 响应头条数过多：101 条名称各不相同的短响应头，每条值均为 'a'
     （超过 http.client._MAXHEADERS=100，读首部时抛 HTTPException）。

两种响应都以 HTTP/1.1 200 OK 状态行开始、CRLF 分行、以空行结束首部，
且不发送响应体；服务完整接收一次 GET（/health?detail=1）后立即发出。
两种场景分别只执行一次 check（--timeout 2），预期完全一致：
退出码 1、stderr 为空、stdout 恰好一行既有七字段 JSON，
status=failure、reason=connection_error、http_status=null；
服务只收到一次 GET，方法、路径与查询参数与登记值一致。

每个场景使用父目录已存在的全新临时数据库：检查结束后仅新增一条记录
（id=1），逐字段与 stdout 一致；原始 URL 保持不变，checked_at 是可解析
且带 UTC 时区的时间，elapsed_ms 为非负整数（不核对固定时间或精确耗时）。
随后对同一数据库调用 recent：退出码 0、stderr 为空，输出数组仅含刚保存
的记录；查询前后表结构、数据内容与服务请求次数均不变。

每次命令都必须在 5 秒内取得结果，超过即判失败；断言失败时 tearDown
同样释放服务、已启动的子进程与临时资源。全部经真实本机 127.0.0.1 通信
与公开命令入口（子进程运行 healthcheck.py）观察，服务只绑定环回地址、
端口由系统分配，不访问公网、不依赖固定空闲端口或上次运行留下的数据：
每个场景自建临时库与服务，连续执行两次得到相同的分类与记录数量。

运行：
    python3 test_check_header_limits.py
    python3 -m unittest test_check_header_limits
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

# 场景显式使用的探测超时（秒）
CHECK_TIMEOUT = "2"

# 场景一：单条响应头值连续 70000 个 'a'，整行超过解析上限（_MAXLINE=65536）
PAD_VALUE_LENGTH = 70000
# 场景二：101 条名称各不相同的短响应头（解析上限 _MAXHEADERS=100）
MANY_HEADER_COUNT = 101

# 每个子进程必须取得结果的等待上限（秒）
PROCESS_DEADLINE = 5.0

MODE_LONG_HEADER = "long_header"
MODE_MANY_HEADERS = "many_headers"

RECORD_FIELDS = ("id", "url", "checked_at", "elapsed_ms",
                 "status", "http_status", "reason")

# 两种超限响应：状态行 + CRLF 分行的首部 + 空行，不发送响应体
LONG_HEADER_RESPONSE = (
    b"HTTP/1.1 200 OK\r\n"
    b"X-Pad: " + b"a" * PAD_VALUE_LENGTH + b"\r\n"
    b"\r\n"
)
MANY_HEADERS_RESPONSE = (
    b"HTTP/1.1 200 OK\r\n"
    + b"".join(
        f"X-Hdr-{index:03d}: a\r\n".encode("ascii")
        for index in range(MANY_HEADER_COUNT)
    )
    + b"\r\n"
)

RESPONSE_BY_MODE = {
    MODE_LONG_HEADER: LONG_HEADER_RESPONSE,
    MODE_MANY_HEADERS: MANY_HEADERS_RESPONSE,
}


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


def read_table_info(db_path):
    """读取 checks 表结构定义，用于核对 recent 前后表结构不变。"""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("PRAGMA table_info(checks)").fetchall()
    finally:
        conn.close()


def rows_as_records(rows):
    return [dict(zip(RECORD_FIELDS, row)) for row in rows]


class _OverLimitHandler(socketserver.BaseRequestHandler):
    """逐字节处理的最小 TCP 服务，不套用 HTTP 解析框架。

    完整接收一个 GET 请求（读到首部结束的空行）后登记原始请求字节，
    随后立即发出该模式指定的超限响应，再关闭连接；不发送响应体。
    server.requests 保存完整收到的原始请求，用于核对请求次数以及
    方法、路径与查询参数是否原样到达。
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

        # 立即返回指定的超限响应；客户端可能在收完前就判定解析失败
        # 并关闭连接，写失败一律忽略，不影响已登记的请求计数
        try:
            sock.sendall(self.server.payload)
        except OSError:
            pass

        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _OverLimitTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_server(mode):
    server = _OverLimitTCPServer(("127.0.0.1", 0), _OverLimitHandler)
    server.mode = mode
    server.payload = RESPONSE_BY_MODE[mode]
    server.requests = []  # 完整收到的原始请求字节列表
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class HeaderLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-header-limit-test-"))
        self._servers = []
        self._procs = []

    def tearDown(self):
        # 断言失败也必须回收已启动的子进程、本机服务与临时数据
        for proc in self._procs:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
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

    # ---- 子进程辅助 ----

    def _run_cli(self, db_path, *args):
        """运行 healthcheck.py，必须在 PROCESS_DEADLINE 秒内取得结果。

        超过期限即终止子进程并明确报告失败（而不是无限等待）。
        """
        cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *args]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._procs.append(proc)
        try:
            stdout, stderr = proc.communicate(timeout=PROCESS_DEADLINE)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            self.fail(f"子进程在 {PROCESS_DEADLINE} 秒内未结束，已终止")
        return proc.returncode, stdout, stderr

    # ---- 分类一：归类（退出码 / stdout / status / reason / http_status）----

    def assert_connection_error_classification(self, returncode, stdout,
                                               stderr, url):
        """归类为已记录的连接异常失败，返回 stdout 解析出的记录。"""
        self.assertEqual(
            returncode, 1,
            "[归类] check 必须以退出码 1 结束（已记录的探测失败）",
        )
        self.assertEqual(stderr, "", "[归类] stderr 必须为空")

        # stdout 恰好一行：只有一个换行且位于行尾
        self.assertEqual(stdout.count("\n"), 1, "[归类] stdout 必须恰有一行")
        self.assertTrue(stdout.endswith("\n"), "[归类] 该行必须以换行结束")
        record = json.loads(stdout.rstrip("\n"))
        self.assertEqual(
            sorted(record.keys()), sorted(RECORD_FIELDS),
            "[归类] JSON 字段集合必须与既有七字段一致",
        )

        self.assertEqual(
            record["status"], "failure",
            "[归类] status 必须为 failure（不能误判成功）",
        )
        self.assertEqual(
            record["reason"], "connection_error",
            "[归类] reason 必须为 connection_error"
            "（响应头超解析上限属于响应解析异常，不能是 ok/http_status/timeout）",
        )
        self.assertIsNone(
            record["http_status"],
            "[归类] 响应未能完成解析，http_status 必须为 null",
        )

        # 公共字段：id=1、原始 URL、UTC 时间、非负整数毫秒耗时
        self.assertEqual(record["id"], 1, "[持久化] 新库首条记录 id 必须为 1")
        self.assertEqual(
            record["url"], url, "[持久化] 必须保留登记的原始 URL"
        )
        self.assertIsInstance(record["elapsed_ms"], int)
        self.assertNotIsInstance(record["elapsed_ms"], bool)
        self.assertGreaterEqual(record["elapsed_ms"], 0)
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

    def assert_persistence_and_recent(self, db_path, record, server,
                                      requests_before):
        """恰好新增一条与 stdout 逐字段一致的记录；recent 只读返回同一条，
        且查询前后表结构、数据内容与服务请求次数均不变。"""
        rows = read_checks_rows(db_path)
        self.assertEqual(
            len(rows), 1, "[持久化] 检查结束后数据库必须恰好新增一条记录"
        )
        self.assertEqual(
            rows_as_records(rows), [record],
            "[持久化] 落库记录必须与 stdout 逐字段一致",
        )

        schema_before = read_table_info(db_path)

        returncode, stdout, stderr = self._run_cli(db_path, "recent")
        self.assertEqual(returncode, 0, stderr)
        self.assertEqual(stderr, "", "[持久化] recent 的 stderr 必须为空")
        self.assertEqual(stdout.count("\n"), 1, repr(stdout))
        self.assertTrue(stdout.endswith("\n"), repr(stdout))
        self.assertEqual(
            json.loads(stdout.rstrip("\n")), [record],
            "[持久化] recent 必须返回仅含该记录的数组",
        )

        # recent 严格只读：表结构、数据内容、服务请求次数查询前后均不变
        self.assertEqual(
            read_table_info(db_path), schema_before,
            "[持久化] recent 不得改变表结构",
        )
        self.assertEqual(
            read_checks_rows(db_path), rows,
            "[持久化] recent 不得改变数据内容",
        )
        self.assertEqual(
            list(server.requests), requests_before,
            "[持久化] recent 期间服务请求计数必须保持不变",
        )

    # ---- 两个超限场景共用同一流程 ----

    def _run_over_limit_scenario(self, mode, db_name):
        server = self._start(mode)
        port = server.server_address[1]
        # 父目录（self.tmp）已存在；数据库文件本身全新，由 check 创建
        db_path = self.tmp / db_name
        url = f"http://127.0.0.1:{port}{TARGET}"
        self.assertFalse(
            db_path.exists(), "[前置条件] 临时数据库必须是全新文件"
        )

        returncode, stdout, stderr = self._run_cli(
            db_path, "check", "--url", url, "--timeout", CHECK_TIMEOUT
        )
        record = self.assert_connection_error_classification(
            returncode, stdout, stderr, url
        )

        self.assert_single_get_received(server)
        self.assert_persistence_and_recent(
            db_path, record, server, list(server.requests)
        )

    def test_single_header_value_over_line_limit(self):
        # 场景一：一条 X-Pad 响应头的值为连续 70000 个 'a'
        self._run_over_limit_scenario(MODE_LONG_HEADER, "long-header.sqlite")

    def test_header_count_over_header_limit(self):
        # 场景二：101 条名称各不相同的短响应头，每条值均为 'a'
        self._run_over_limit_scenario(MODE_MANY_HEADERS, "many-headers.sqlite")


if __name__ == "__main__":
    unittest.main(verbosity=2)
