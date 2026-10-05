#!/usr/bin/env python3
"""healthcheck.py check「收到完整响应头即结束探测」的回归测试。

保护既有行为：check 只依据状态行与响应头分类结果，elapsed_ms 也在
getresponse() 读到状态码后截止——不读取、不等待响应体（见 CHECK_FLOW.md
第 5 节）。三个场景均经公开命令行入口执行，使用独立临时 SQLite 数据库与
绑定 127.0.0.1 随机端口的本机可控服务，目标路径统一为 /health?case=body，
并显式传入 --timeout 0.2：

  1. 完整响应头已发出（Content-Length 声明非空正文），但正文在 check 结束
     之前始终不发送，状态码 200 → 退出码 0，success/ok/200，不得因正文
     未到达被记成 timeout 或 connection_error；
  2. 同一场景状态码 503 → 退出码 1，failure/http_status/503，同样不得
     记成 timeout 或 connection_error；
  3. 收到 GET 后完全不发响应头 → 超时后退出码 1，failure/timeout/null。

每种情形都核对：服务只收到一次登记路径的 GET；stdout 恰好一行既有七字段
JSON；stderr 为空；数据库恰好保存一条与 stdout 逐字段一致的记录；随后经
recent 只读取回只包含该记录的数组（退出码 0），记录内容与请求次数均不变。

测试等待有有限上限，检查进程未结束即明确失败；即使用例失败也会释放本地
服务、子进程与临时数据。仅依赖 Python 标准库，重复执行相互独立，不访问
公网、不使用固定端口、不依赖已有数据库或人工启动的服务。

运行：
    python3 test_check_headers_complete.py
    python3 -m unittest test_check_headers_complete
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

CHECK_TIMEOUT = "0.2"
REQUEST_TARGET = "/health?case=body"

# Content-Length 声明的非空正文；在测试放行之前绝不发送
PENDING_BODY = b"this-non-empty-body-is-held-back-until-the-check-has-ended"

# 子进程必须结束的有限等待上限（秒）：正常场景毫秒级返回，超时场景约 0.2s
PROCESS_DEADLINE = 10.0
# 等待服务发出完整响应头的上限（秒）
HEADERS_WAIT = 5.0
# 服务端线程的兜底停留时间（秒）：正常由事件提前放行，仅防线程永久悬挂
SERVER_HOLD_SECONDS = 30.0
# 超时场景允许的最大墙钟耗时（秒）：远大于 0.2s 的超时值，足以容纳调度
# 抖动，又能保证检查确实是「超时后」结束而非无限等待
TIMEOUT_SCENE_WALL_LIMIT = 5.0

RECORD_FIELDS = ("id", "url", "checked_at", "elapsed_ms",
                 "status", "http_status", "reason")


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
    """记录请求并抑制日志噪音；客户端提前断开后的写错误一律忽略。"""

    def log_message(self, *args):
        pass


class _HeadersWithoutBodyHandler(_BaseHandler):
    """发出完整响应头后挂住，不发送正文，直到测试放行或 check 已结束。

    状态码取自 server.headers_status（200 或 503）。Content-Length 声明
    一段非空正文，但正文在 server.release_body 被置位前绝不发送。
    """

    def do_GET(self):
        self.server.requests.append(self.path)
        try:
            self.send_response(self.server.headers_status)
            self.send_header("Content-Length", str(len(PENDING_BODY)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.flush()
        except OSError:
            # 响应头尚未发完连接就断开：无需再放行正文
            return

        # 至此完整响应头已交由内核发送；通知测试可以核对检查结果
        self.server.headers_ready.set()

        # 正文继续扣住不放，直到 check 进程结束后由测试放行（或兜底超时）
        self.server.release_body.wait(timeout=SERVER_HOLD_SECONDS)
        try:
            self.wfile.write(PENDING_BODY)
            self.wfile.flush()
        except OSError:
            # 客户端在读完响应头后即关闭连接：写正文失败正是预期
            pass


class _NoHeadersHandler(_BaseHandler):
    """收到 GET 后只登记请求，一直不发送响应头，直到测试结束放行。"""

    def do_GET(self):
        self.server.requests.append(self.path)
        self.server.release_body.wait(timeout=SERVER_HOLD_SECONDS)
        # 此时客户端早已超时关闭；尽力应答一次，失败即忽略
        try:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.flush()
        except OSError:
            pass


def start_server(handler, headers_status=None):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    server.requests = []  # 已收到的请求路径列表，用于核对请求次数与目标
    server.headers_status = headers_status
    server.headers_ready = threading.Event()
    server.release_body = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class CheckHeadersCompleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-headers-test-"))
        self._servers = []
        self._procs = []

    def tearDown(self):
        # 先放行所有可能仍挂在 do_GET 中的服务线程
        for server, _thread in self._servers:
            server.release_body.set()
        # 再回收任何未结束的检查子进程（例如用例在等待前失败）
        for proc in self._procs:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)
        for server, thread in self._servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                os.chmod(os.path.join(root, name), stat.S_IRWXU)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start(self, handler, headers_status=None):
        server, thread = start_server(handler, headers_status)
        self._servers.append((server, thread))
        return server

    # ---- 子进程与断言辅助 ----

    def _popen_cli(self, db_path, *args):
        cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *args]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._procs.append(proc)
        return proc

    def _wait_process(self, proc, deadline=PROCESS_DEADLINE):
        """有限等待子进程结束；未结束即终止并明确失败。"""
        try:
            stdout, stderr = proc.communicate(timeout=deadline)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            self.fail(f"子进程在 {deadline} 秒内未结束，已终止")
        return proc.returncode, stdout, stderr

    def assert_single_json_line(self, stdout):
        """stdout 恰好是一行末尾带换行的七字段 JSON，返回解析后的记录。"""
        self.assertEqual(stdout.count("\n"), 1, repr(stdout))
        self.assertTrue(stdout.endswith("\n"), repr(stdout))
        record = json.loads(stdout.rstrip("\n"))
        self.assertEqual(sorted(record.keys()), sorted(RECORD_FIELDS))
        return record

    def assert_common_record_fields(self, record, url):
        """原始 URL 不变；checked_at 为带 UTC 时区的有效时间；
        elapsed_ms 为非负整数（具体毫秒数不核对）。"""
        self.assertEqual(record["url"], url)
        self.assertIsInstance(record["elapsed_ms"], int)
        self.assertNotIsInstance(record["elapsed_ms"], bool)
        self.assertGreaterEqual(record["elapsed_ms"], 0)
        checked_at = datetime.fromisoformat(record["checked_at"])
        self.assertIsNotNone(checked_at.tzinfo)
        self.assertEqual(checked_at.utcoffset(), timedelta(0))

    def assert_db_holds_only(self, db_path, record):
        """数据库恰好保存一条记录，且与 check 的 stdout 逐字段一致。"""
        rows = read_checks_rows(db_path)
        self.assertEqual(rows_as_records(rows), [record])

    def assert_recent_roundtrip(self, db_path, record, server):
        """recent 只读返回同一记录组成的单元素数组，不改库、不发新请求。"""
        rows_before = read_checks_rows(db_path)
        requests_before = list(server.requests)

        proc = self._popen_cli(db_path, "recent")
        returncode, stdout, stderr = self._wait_process(proc)
        self.assertEqual(returncode, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout.rstrip("\n")), [record])
        self.assertEqual(stdout.count("\n"), 1, repr(stdout))

        # 查询前后记录内容不变，本地服务未收到新请求
        self.assertEqual(read_checks_rows(db_path), rows_before)
        self.assertEqual(server.requests, requests_before)

    def assert_single_registered_get(self, server):
        """服务只收到一次登记路径的 GET，没有重试或其他请求。"""
        self.assertEqual(server.requests, [REQUEST_TARGET])

    # ---- 场景一/二：完整响应头已到、非空正文扣住不发 ----

    def _run_headers_without_body(self, status_code, expected_returncode,
                                  expected_status, expected_reason):
        server = self._start(
            _HeadersWithoutBodyHandler, headers_status=status_code
        )
        port = server.server_address[1]
        db_path = self.tmp / f"headers-{status_code}.sqlite"
        url = f"http://127.0.0.1:{port}{REQUEST_TARGET}"

        proc = self._popen_cli(
            db_path, "check", "--url", url, "--timeout", CHECK_TIMEOUT
        )

        # 服务必须先把完整响应头发出（正文仍扣住）；否则本场景不成立
        if not server.headers_ready.wait(timeout=HEADERS_WAIT):
            proc.kill()
            proc.communicate(timeout=5)
            self.fail("服务在规定时间内未发出完整响应头")

        returncode, stdout, stderr = self._wait_process(proc)

        # check 已结束，放行正文：此时客户端通常已关闭连接，写失败可忽略
        server.release_body.set()

        self.assertEqual(returncode, expected_returncode, stderr)
        self.assertEqual(stderr, "")

        record = self.assert_single_json_line(stdout)
        self.assertEqual(record["status"], expected_status)
        self.assertEqual(record["reason"], expected_reason)
        self.assertEqual(record["http_status"], status_code)
        self.assert_common_record_fields(record, url)

        self.assert_single_registered_get(server)
        self.assert_db_holds_only(db_path, record)
        self.assert_recent_roundtrip(db_path, record, server)

    def test_200_headers_without_body_is_success(self):
        # 200：仅响应头完整即 success/ok/200、退出码 0，正文永不超时化
        self._run_headers_without_body(200, 0, "success", "ok")

    def test_503_headers_without_body_is_http_status_failure(self):
        # 503：仅响应头完整即 failure/http_status/503、退出码 1，
        # 不得因正文未到达降级成 timeout 或 connection_error
        self._run_headers_without_body(503, 1, "failure", "http_status")

    # ---- 场景三：收到 GET 后完全不发响应头 ----

    def test_no_headers_times_out(self):
        server = self._start(_NoHeadersHandler)
        port = server.server_address[1]
        db_path = self.tmp / "no-headers.sqlite"
        url = f"http://127.0.0.1:{port}{REQUEST_TARGET}"

        proc = self._popen_cli(
            db_path, "check", "--url", url, "--timeout", CHECK_TIMEOUT
        )

        started = time.monotonic()
        returncode, stdout, stderr = self._wait_process(proc)
        wall_seconds = time.monotonic() - started
        # 放行服务线程，使其不必等待兜底超时
        server.release_body.set()

        self.assertLess(
            wall_seconds, TIMEOUT_SCENE_WALL_LIMIT,
            f"检查未在超时后及时结束，墙钟耗时 {wall_seconds:.2f}s",
        )
        self.assertEqual(returncode, 1, stderr)
        self.assertEqual(stderr, "")

        record = self.assert_single_json_line(stdout)
        self.assertEqual(record["status"], "failure")
        self.assertEqual(record["reason"], "timeout")
        self.assertIsNone(record["http_status"])
        self.assert_common_record_fields(record, url)

        self.assert_single_registered_get(server)
        self.assert_db_holds_only(db_path, record)
        self.assert_recent_roundtrip(db_path, record, server)


if __name__ == "__main__":
    unittest.main(verbosity=2)
