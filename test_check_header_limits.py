#!/usr/bin/env python3
"""healthcheck.py check 响应头超过解析限制的命令行回归测试。

check 已把 HTTP 响应解析异常（http.client.HTTPException）与对端断开等
OSError 一并归类为 connection_error；test_check_connection_error 已覆盖
“对端直接断开”和“无法解析的状态行”。本模块只补充两个真实本机边界：
  * 状态行合法（HTTP/1.1 200 OK、CRLF 分行、空行结束首部且无响应体），
    但单个响应头行超长：一条 X-Pad，其值为连续 70000 个 ASCII 字符 a，
    超过 http.client 单行 65536 字节的解析限制；
  * 状态行与分行规则同上，但响应头条数过多：101 条名称各不相同的短响应头
    （每条值均为 a），超过 http.client 默认最多 100 条首部的解析限制。

两种输入都必须在读取首部时抛出 HTTPException，并仍按既有协议归类为已记录
的探测失败：退出码 1、stderr 为空、stdout 恰有一行既有七字段 JSON，
status=failure、reason=connection_error、http_status=null；
失败由解析异常立即触发，而非等待 --timeout 2 超时。

每个场景共同约定：
  * 登记合法本机 URL（/health?detail=1），端口由服务实际分配，
    方法、路径与查询参数原样到达，服务只收到一次 GET；
  * 使用父目录已存在的全新临时数据库，检查后恰好新增一条 id=1 的记录，
    各字段与 stdout 逐项一致：保留原始 URL、checked_at 为可解析的 UTC 时间、
    elapsed_ms 为非负整数（不核对固定时间或精确耗时）；
  * 随后对同一数据库 recent 只读查询：退出码 0、stderr 为空、
    返回仅含该记录的数组；查询前后表结构、数据内容与服务请求计数均不变。

全部通过真实本机通信与公开命令入口（子进程运行 healthcheck.py）观察结果，
不伪造探测返回值；服务只绑定 127.0.0.1，不访问公网，不依赖固定空闲端口或
既有历史数据库。每次运行自行准备并释放服务、子进程与临时资源，
任一子进程必须在 5 秒内返回结果，否则判失败；断言失败时 tearDown 同样
完成清理。每次测试使用新临时库与系统分配端口，故连续执行两次得到相同的
分类与记录数量，互不影响。
运行：python3 -m unittest test_check_header_limits
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

# check 命令显式使用的超时秒数；解析异常必须远在该期限之前结束
TIMEOUT_SECONDS = 2

# 每条子进程命令（check / recent）必须在该期限内返回，否则判测试失败
COMMAND_DEADLINE_SECONDS = 5

# 场景一：单个响应头行的长度上限（http.client 限制 65536 字节）
PAD_LENGTH = 70000

# 场景二：响应头条数上限（http.client 默认最多 100 条）
EXTRA_HEADER_COUNT = 101

RECORD_FIELDS = ("id", "url", "checked_at", "elapsed_ms",
                 "status", "http_status", "reason")


def build_padded_response():
    """状态行合法、无响应体；一条 X-Pad，值为连续 70000 个 'a'。"""
    return (
        b"HTTP/1.1 200 OK\r\n"
        b"X-Pad: " + b"a" * PAD_LENGTH + b"\r\n"
        b"\r\n"
    )


def build_many_headers_response():
    """状态行合法、无响应体；101 条名称各不相同的短响应头，值均为 'a'。"""
    headers = b"".join(
        f"X-H-{index:03d}: a\r\n".encode("ascii")
        for index in range(EXTRA_HEADER_COUNT)
    )
    return b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n"


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


def read_checks_schema(db_path):
    """读取 checks 表结构快照（PRAGMA table_info），用于核对 recent 不改表。"""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("PRAGMA table_info(checks)").fetchall()
    finally:
        conn.close()


def rows_as_records(rows):
    return [dict(zip(RECORD_FIELDS, row)) for row in rows]


class _OversizeHeaderHandler(socketserver.BaseRequestHandler):
    """逐字节处理的最小 TCP 服务，不套用 HTTP 解析框架。

    完整接收一个 GET 请求（读到首部结束的空行）后，立即把服务预置的
    指定响应字节（合法状态行 + 超限首部 + 空行，无响应体）一次性发出，
    再关闭连接。服务把完整收到的原始请求字节记入 server.requests，
    用于核对请求次数、方法以及路径与查询参数是否原样到达。
    客户端在解析超限首部时即可能放弃连接，发送侧的 BrokenPipe 等
    OSError 不影响其已得到的解析异常，故忽略。
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

        try:
            sock.sendall(self.server.response)
        except OSError:
            pass

        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _OversizeHeaderTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class HeaderLimitTests(unittest.TestCase):
    def setUp(self):
        # 新建临时目录（父目录已存在），每个场景再在其中使用全新数据库文件
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-hdrlimit-test-"))
        self._servers = []

    def tearDown(self):
        # 断言失败也必须释放本机服务与临时数据；
        # 子进程由 subprocess.run 在超限时 kill，正常路径执行完即已退出
        for server, thread in self._servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                os.chmod(os.path.join(root, name), stat.S_IRWXU)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start_server(self, response):
        server = _OversizeHeaderTCPServer(("127.0.0.1", 0), _OversizeHeaderHandler)
        server.response = response
        server.requests = []  # 完整收到的原始请求字节列表
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._servers.append((server, thread))
        return server

    def run_cli(self, db_path, *args):
        """以子进程运行 healthcheck.py；超过 5 秒未返回即判测试失败。"""
        cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *args]
        try:
            return subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=COMMAND_DEADLINE_SECONDS,
            )
        except subprocess.TimeoutExpired:
            self.fail(
                f"命令超过 {COMMAND_DEADLINE_SECONDS} 秒仍未结束: {' '.join(args)}"
            )

    # ---- 分类一：归类（退出码 / stdout / status / reason / http_status）----

    def assert_connection_error_classification(self, proc, url):
        """超限首部归类为已记录的连接异常失败，返回 stdout 解析出的记录。"""
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
            "[归类] JSON 字段集合必须与既有七字段一致",
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
            "[归类] 首部无法解析即无可用状态码，http_status 必须为 null",
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
            record["elapsed_ms"], TIMEOUT_SECONDS * 1000,
            "[归类] 探测必须由首部解析异常立即结束，而不是等待 --timeout 超时",
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
            "[重复请求] 服务必须在完整接收请求后才返回响应",
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
        """恰好新增一条与 stdout 逐字段一致的记录；recent 只读返回同一条，
        且查询前后表结构、数据内容与服务请求计数均不变。"""
        rows = read_checks_rows(db)
        self.assertEqual(
            len(rows), 1, "[持久化] 全新数据库必须恰好新增一条记录"
        )
        self.assertEqual(
            rows_as_records(rows), [record],
            "[持久化] 落库记录必须与 stdout 逐字段一致",
        )
        schema_before = read_checks_schema(db)

        proc = self.run_cli(db, "recent")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "", "[持久化] recent 的 stderr 必须为空")
        self.assertEqual(
            json.loads(proc.stdout), [record],
            "[持久化] recent 必须返回仅含该记录的数组",
        )

        # recent 之后表结构、数据内容、服务请求计数均不变（recent 只读、不发请求）
        self.assertEqual(
            read_checks_schema(db), schema_before,
            "[持久化] recent 不得改变 checks 表结构",
        )
        self.assertEqual(
            read_checks_rows(db), rows,
            "[持久化] recent 不得改变数据库内容",
        )
        self.assertEqual(
            list(server.requests), requests_before,
            "[持久化] recent 期间服务请求计数必须保持不变",
        )

    def _run_header_limit_scenario(self, name, response):
        server = self._start_server(response)
        port = server.server_address[1]
        db = self.tmp / f"{name}.sqlite"
        url = f"http://127.0.0.1:{port}{TARGET}"

        proc = self.run_cli(db, "check", "--url", url,
                            "--timeout", str(TIMEOUT_SECONDS))
        record = self.assert_connection_error_classification(proc, url)

        self.assert_single_get_received(server)
        self.assert_persistence_and_recent(
            db, record, server, list(server.requests)
        )

    # ---- 场景一：单个响应头行超过解析限制（X-Pad 七万个 a）----

    def test_single_header_line_over_limit(self):
        self._run_header_limit_scenario("pad", build_padded_response())

    # ---- 场景二：响应头条数超过解析限制（101 条短首部）----

    def test_header_count_over_limit(self):
        self._run_header_limit_scenario("many", build_many_headers_response())


if __name__ == "__main__":
    unittest.main(verbosity=2)
