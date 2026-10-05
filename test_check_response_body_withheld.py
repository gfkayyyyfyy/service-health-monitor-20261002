#!/usr/bin/env python3
"""healthcheck.py check「收到完整响应头即结束探测」的回归测试。

保护既有行为：check 只凭 HTTP 响应头（状态行 + 头部）判定结果，
从不等待响应体。通过公开命令行入口（子进程）覆盖三个相互对照的场景，
目标路径统一为 /health?case=body，显式传入 --timeout 0.2：

  * 服务发出完整 200 响应头（Content-Length 声明非空正文）后按住正文不发，
    直到 check 进程结束 → 退出码 0，success/ok，http_status 200；
  * 同样按住正文，但响应头状态码为 503
    → 退出码 1，failure/http_status，http_status 503；
  * 收到 GET 后完全不发送响应头 → 超时，退出码 1，failure/timeout，
    http_status 为 null。

每种情形均核对：服务只收到一次登记路径的 GET；stdout 恰好一行既有
七字段 JSON；stderr 为空；数据库恰好保存一条与输出逐字段一致的记录；
随后 recent 只读取回仅含该记录的数组（退出码 0），库内容与请求次数不变。

仅使用 Python 标准库；每个用例使用独立临时 SQLite 数据库与绑定
127.0.0.1 随机端口的本机服务，重复执行相互独立，不访问公网、不依赖
固定端口、已有数据库或人为启动的服务。
运行：python3 test_check_response_body_withheld.py
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

# 显式指定的探测超时（秒）
CHECK_TIMEOUT = "0.2"

# 三个场景共同登记的原始 URL 路径（含查询）
PATH_AND_QUERY = "/health?case=body"

# Content-Length 声明的非空正文；在允许发送前一直按住不发
BODY = b"hello-body"

# check 子进程必须结束的等待上限；超时未结束即明确失败
PROC_WAIT_TIMEOUT = 5.0

# 服务端线程同步事件的等待上限
EVENT_WAIT_TIMEOUT = 3.0

# 无响应头场景中服务端占住连接的最长时间（远大于 0.2s 超时即可）
HOLD_CONNECTION_FOR = 3.0

RECORD_FIELDS = ("id", "url", "checked_at", "elapsed_ms",
                 "status", "http_status", "reason")

SCENARIO_HEADERS_200 = "headers-200"
SCENARIO_HEADERS_503 = "headers-503"
SCENARIO_NO_HEADERS = "no-headers"


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


class _ControlledHandler(http.server.BaseHTTPRequestHandler):
    """按 server.scenario 控制响应节奏的处理器。

    headers-* 场景：发出完整响应头（Content-Length 声明非空正文）后
    阻塞在 server.body_allowed 事件上，正文在测试放行前绝不发送；
    no-headers 场景：收到 GET 后不发送任何响应字节，只占住连接，
    直到客户端超时关闭或服务关闭。
    """

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append(self.path)

        if self.server.scenario == SCENARIO_NO_HEADERS:
            # 不写任何响应字节，仅占住连接；客户端会在 --timeout 后放弃
            deadline = time.monotonic() + HOLD_CONNECTION_FOR
            while not self.server.close_event.is_set() and time.monotonic() < deadline:
                time.sleep(0.05)
            return

        status_code = (
            200 if self.server.scenario == SCENARIO_HEADERS_200 else 503
        )
        try:
            self.send_response(status_code)
            # 声明非空正文但先不发送：探测应在只读完响应头时即结束
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.flush()
        except OSError:
            # 对端提前关闭等写错误：标记头部状态后结束，不影响请求计数核对
            self.server.headers_sent.set()
            return

        self.server.headers_sent.set()

        # 正文保持未发送，直到测试显式放行（放行时刻晚于 check 进程结束）
        if self.server.body_allowed.wait(timeout=EVENT_WAIT_TIMEOUT):
            try:
                self.wfile.write(BODY)
                self.wfile.flush()
            except OSError:
                # check 读完响应头即关闭连接，未读正文，写正文可能失败——预期
                pass
        self.server.body_sent.set()


def start_server(scenario):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ControlledHandler)
    server.daemon_threads = True
    server.scenario = scenario
    server.requests = []  # 已收到的请求路径列表，用于核对请求次数与目标
    server.headers_sent = threading.Event()
    server.body_allowed = threading.Event()
    server.body_sent = threading.Event()
    server.close_event = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class CheckResponseBodyWithheldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-body-test-"))
        self._servers = []

    def tearDown(self):
        # 即便用例中途失败，也放行正文线程并关闭服务、回收临时数据
        for server, thread in self._servers:
            server.body_allowed.set()
            server.close_event.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                os.chmod(os.path.join(root, name), stat.S_IRWXU)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start(self, scenario):
        server, thread = start_server(scenario)
        self._servers.append((server, thread))
        return server

    # ---- 子进程与输出断言 ----

    def _run_check_with_bound(self, db_path, url):
        """以子进程运行 check，在有限上限内等待其结束；超时即明确失败。"""
        cmd = [
            sys.executable, str(SCRIPT), "--db", str(db_path),
            "check", "--url", url, "--timeout", CHECK_TIMEOUT,
        ]
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        try:
            stdout, stderr = proc.communicate(timeout=PROC_WAIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            self.fail(
                f"check 进程在 {PROC_WAIT_TIMEOUT}s 内未结束（疑似等待响应体）"
            )
        return proc.returncode, stdout, stderr

    def assert_single_json_line(self, stdout):
        """stdout 恰好是一行可解析的既有七字段 JSON。"""
        lines = stdout.splitlines()
        self.assertEqual(len(lines), 1, stdout)
        record = json.loads(lines[0])
        self.assertEqual(sorted(record.keys()), sorted(RECORD_FIELDS))
        return record

    def assert_common_record_fields(self, record, url):
        self.assertEqual(record["id"], 1)
        # 原始 URL 原样保存，不做任何归一化
        self.assertEqual(record["url"], url)
        self.assertIsInstance(record["elapsed_ms"], int)
        self.assertNotIsInstance(record["elapsed_ms"], bool)
        self.assertGreaterEqual(record["elapsed_ms"], 0)
        # checked_at 是带 UTC 时区的有效时间
        checked_at = datetime.fromisoformat(record["checked_at"])
        self.assertIsNotNone(checked_at.tzinfo)
        self.assertEqual(checked_at.utcoffset(), timedelta(0))

    def assert_db_holds_only(self, db_path, record):
        """数据库恰好保存一条记录，且与 check 的 stdout 逐字段一致。"""
        rows = read_checks_rows(db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows_as_records(rows), [record])

    def assert_recent_roundtrip(self, db_path, record, server):
        """recent 只读返回仅含该记录的数组，且不改库、不发新请求。"""
        rows_before = read_checks_rows(db_path)
        requests_before = list(server.requests)

        cmd = [
            sys.executable, str(SCRIPT), "--db", str(db_path), "recent",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(json.loads(proc.stdout), [record])

        # 查询前后记录数与内容不变，本地服务未收到新请求
        self.assertEqual(read_checks_rows(db_path), rows_before)
        self.assertEqual(server.requests, requests_before)

    def _assert_one_registered_get(self, server):
        self.assertEqual(server.requests, [PATH_AND_QUERY])

    # ---- 场景一：完整响应头已送达，正文始终未发送 ----

    def _run_headers_withheld_body(self, scenario):
        server = self._start(scenario)
        port = server.server_address[1]
        db_path = self.tmp / f"{scenario}.sqlite"
        url = f"http://127.0.0.1:{port}{PATH_AND_QUERY}"

        returncode, stdout, stderr = self._run_check_with_bound(db_path, url)

        # 响应头确实已经送达；check 结束时正文仍未发送
        self.assertTrue(
            server.headers_sent.wait(timeout=EVENT_WAIT_TIMEOUT),
            "服务端未在限定时间内发出完整响应头",
        )
        self.assertFalse(
            server.body_sent.is_set(),
            "check 结束前正文已被发送，无法证明探测不等正文",
        )

        self.assertEqual(stderr, "")
        return server, db_path, url, returncode, stdout

    def _release_body_after_check(self, server):
        """check 结束后放行正文并等待服务端写完（或发现对端已关闭）。"""
        server.body_allowed.set()
        self.assertTrue(
            server.body_sent.wait(timeout=EVENT_WAIT_TIMEOUT),
            "放行后服务端正文线程未结束",
        )

    def test_200_headers_without_body_is_success_ok(self):
        server, db_path, url, returncode, stdout = (
            self._run_headers_withheld_body(SCENARIO_HEADERS_200)
        )

        self.assertEqual(returncode, 0)
        record = self.assert_single_json_line(stdout)
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["reason"], "ok")
        self.assertEqual(record["http_status"], 200)
        self.assert_common_record_fields(record, url)

        # 只发了一次登记路径的 GET；正文未到不得记成 timeout/connection_error
        self._assert_one_registered_get(server)
        self.assert_db_holds_only(db_path, record)

        self._release_body_after_check(server)
        self.assert_recent_roundtrip(db_path, record, server)
        self._assert_one_registered_get(server)

    def test_503_headers_without_body_is_failure_http_status(self):
        server, db_path, url, returncode, stdout = (
            self._run_headers_withheld_body(SCENARIO_HEADERS_503)
        )

        self.assertEqual(returncode, 1)
        record = self.assert_single_json_line(stdout)
        self.assertEqual(record["status"], "failure")
        self.assertEqual(record["reason"], "http_status")
        self.assertEqual(record["http_status"], 503)
        self.assert_common_record_fields(record, url)

        # 同样只发一次请求；正文未到不得记成 timeout/connection_error
        self._assert_one_registered_get(server)
        self.assert_db_holds_only(db_path, record)

        self._release_body_after_check(server)
        self.assert_recent_roundtrip(db_path, record, server)
        self._assert_one_registered_get(server)

    # ---- 场景二：收到 GET 后从不发送响应头 → 超时 ----

    def test_no_response_headers_is_failure_timeout(self):
        server = self._start(SCENARIO_NO_HEADERS)
        port = server.server_address[1]
        db_path = self.tmp / "no-headers.sqlite"
        url = f"http://127.0.0.1:{port}{PATH_AND_QUERY}"

        returncode, stdout, stderr = self._run_check_with_bound(db_path, url)

        self.assertEqual(returncode, 1)
        self.assertEqual(stderr, "")
        self.assertFalse(
            server.headers_sent.is_set(),
            "服务端不应发出任何响应头",
        )

        record = self.assert_single_json_line(stdout)
        self.assertEqual(record["status"], "failure")
        self.assertEqual(record["reason"], "timeout")
        self.assertIsNone(record["http_status"])
        self.assert_common_record_fields(record, url)

        self._assert_one_registered_get(server)
        self.assert_db_holds_only(db_path, record)

        self.assert_recent_roundtrip(db_path, record, server)
        self._assert_one_registered_get(server)


if __name__ == "__main__":
    unittest.main(verbosity=2)
