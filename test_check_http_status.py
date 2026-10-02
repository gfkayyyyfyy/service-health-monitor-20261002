#!/usr/bin/env python3
"""healthcheck.py check 收到 HTTP 响应后状态码分类的命令行回归测试。

覆盖收到真实 HTTP 响应（而非超时/连接失败）的合法场景：
  * 200 / 204 / 299 → 退出码 0、status=success、reason=ok，
    http_status 保留实际状态码（2xx 边界内均算成功）；
  * 300 / 404 / 500 → 退出码 1、status=failure、reason=http_status，
    http_status 同样保留实际状态码；
  * 302 携带指向同一本机服务 /landing 的 Location（该路径若被访问会返回
    200）→ 仍记录为 302 失败，不跟随重定向，/landing 访问次数为零。

每个场景共同约定：
  * 每次检查只向登记路径发送一次 GET，路径与查询参数原样到达；
  * stdout 恰有一行可解析 JSON，stderr 为空；
  * 数据库恰好新增一条记录，字段与 stdout 逐字段一致，保留原始 URL、
    UTC 检查时间、非负整数毫秒 elapsed_ms（只核对类型与非负性）；
  * 随后 recent 只读查询：退出码 0、返回包含同一记录的 JSON 数组，
    期间不发新请求，记录数量与内容不变。

为便于从报告中定位问题，断言按三类分别落在独立方法中：
状态码归类（assert_status_classification）、重定向访问
（assert_redirect_not_followed）、持久化结果
（assert_persistence_and_recent）。

全部使用独立临时目录中的数据库与绑定 127.0.0.1 的随机端口服务，
不访问公网、不依赖固定空闲端口或既有历史；重复执行结果相互独立。
运行：python3 test_check_http_status.py
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
from datetime import datetime, timedelta
from http.client import HTTPConnection
from urllib.parse import parse_qs, urlparse

SCRIPT = pathlib.Path(__file__).resolve().parent / "healthcheck.py"

SUCCESS_CODES = (200, 204, 299)
FAILURE_CODES = (300, 404, 500)
REDIRECT_CODE = 302
CONTROLLABLE_CODES = frozenset(SUCCESS_CODES + FAILURE_CODES + (REDIRECT_CODE,))

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


class _StatusHandler(http.server.BaseHTTPRequestHandler):
    """按路径与查询参数返回可控状态码，并记录每次收到的请求路径。

    GET /health?case=<code>：返回 <code>（见 CONTROLLABLE_CODES）；
    其中 case=302 时附带 Location: http://127.0.0.1:<本服务端口>/landing。
    GET /landing：返回 200（用于证明重定向目标本身可达）。
    """

    def log_message(self, *args):
        pass

    def do_GET(self):
        # 原样记录请求目标（含查询参数），用于核对请求次数与路径
        self.server.requests.append(self.path)

        parsed = urlparse(self.path)
        if parsed.path == "/health":
            values = parse_qs(parsed.query).get("case", [])
            try:
                code = int(values[0]) if values else -1
            except ValueError:
                code = -1
            if code in CONTROLLABLE_CODES:
                if code == REDIRECT_CODE:
                    location = (
                        f"http://127.0.0.1:"
                        f"{self.server.server_address[1]}/landing"
                    )
                    self._reply(code, location=location)
                else:
                    self._reply(code)
                return
        elif parsed.path == "/landing":
            self._reply(200)
            return

        self._reply(400)

    def _reply(self, code, location=None):
        # 204 不允许消息体；其余状态码给一个定长小响应体
        body = b"" if code == 204 else b"body"
        try:
            self.send_response(code)
            if location is not None:
                self.send_header("Location", location)
            if code != 204:
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            if body:
                self.wfile.write(body)
        except OSError:
            # 客户端读完响应头即关闭（不跟随重定向、不读响应体）：忽略写失败
            pass


def start_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StatusHandler)
    server.daemon_threads = True
    server.requests = []  # 已收到的请求目标列表，用于核对请求次数与路径
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def get_once(port, target):
    """测试进程自身直接发一次 GET，返回状态码（用于验证 /landing 本身可达）。"""
    conn = HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        conn.request("GET", target)
        return conn.getresponse().status
    finally:
        conn.close()


class HttpStatusClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-status-test-"))
        self._servers = []

    def tearDown(self):
        # 即使用例断言失败也必须释放本机服务与临时数据
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

    # ---- 分类一：输出与公共字段 ----

    def assert_single_json_line(self, proc):
        """stdout 恰好是一行可解析的 JSON 记录，stderr 为空。"""
        self.assertEqual(proc.stderr, "", "[输出] stderr 必须为空")
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1, "[输出] stdout 必须恰有一行 JSON")
        record = json.loads(lines[0])
        self.assertEqual(
            sorted(record.keys()), sorted(RECORD_FIELDS),
            "[输出] JSON 字段集合必须与约定一致",
        )
        return record

    def assert_common_record_fields(self, record, url):
        """原始 URL、UTC 检查时间、非负整数毫秒耗时（不核对具体毫秒数）。"""
        self.assertEqual(record["id"], 1, "[持久化] 新库首条记录 id 必须为 1")
        self.assertEqual(record["url"], url, "[持久化] 必须保留登记的原始 URL")
        self.assertIsInstance(record["elapsed_ms"], int)
        self.assertNotIsInstance(record["elapsed_ms"], bool)
        self.assertGreaterEqual(record["elapsed_ms"], 0)
        checked_at = datetime.fromisoformat(record["checked_at"])
        self.assertIsNotNone(checked_at.tzinfo, "[持久化] checked_at 必须带时区")
        self.assertEqual(
            checked_at.utcoffset(), timedelta(0),
            "[持久化] checked_at 必须是 UTC 时间",
        )

    # ---- 分类二：状态码归类 ----

    def assert_status_classification(self, record, returncode, code):
        """退出码 / status / reason / http_status 的归类必须正确。"""
        if 200 <= code < 300:
            expected_status, expected_reason, expected_rc = (
                "success", "ok", 0
            )
        else:
            expected_status, expected_reason, expected_rc = (
                "failure", "http_status", 1
            )
        self.assertEqual(
            returncode, expected_rc,
            f"[状态码归类] {code} 的退出码应为 {expected_rc}",
        )
        self.assertEqual(
            record["status"], expected_status,
            f"[状态码归类] {code} 的 status 应为 {expected_status}",
        )
        self.assertEqual(
            record["reason"], expected_reason,
            f"[状态码归类] {code} 的 reason 应为 {expected_reason}"
            "（既不能误判成功，也不能记成连接错误）",
        )
        self.assertEqual(
            record["http_status"], code,
            f"[状态码归类] http_status 必须保留实际状态码 {code}",
        )

    # ---- 分类三：重定向访问 ----

    def assert_redirect_not_followed(self, server, registered_target, landing):
        """重定向场景：登记路径恰好一次，/landing 访问次数为零。"""
        targets = list(server.requests)
        self.assertEqual(
            targets, [registered_target],
            "[重定向访问] 必须只向登记路径发送一次 GET，"
            "路径与查询参数原样到达",
        )
        landing_hits = sum(1 for target in targets if urlparse(target).path == landing)
        self.assertEqual(
            landing_hits, 0,
            "[重定向访问] 不得跟随 302 访问 /landing",
        )

    # ---- 分类四：持久化结果与 recent 只读往返 ----

    def assert_persistence_and_recent(self, db, record, server, requests_before):
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
            "[持久化] recent 必须返回包含同一条记录的数组",
        )

        # recent 之后记录数量与内容不变，且没有新请求到达服务
        self.assertEqual(
            read_checks_rows(db), rows,
            "[持久化] recent 不得改变记录数量与内容",
        )
        self.assertEqual(
            server.requests, requests_before,
            "[持久化] recent 期间不得发起任何网络请求",
        )

    # ---- 单个状态码场景的完整核对 ----

    def _run_one_status_scenario(self, code):
        server = self._start()
        port = server.server_address[1]
        db = self.tmp / f"status-{code}.sqlite"
        target = f"/health?case={code}"
        url = f"http://127.0.0.1:{port}{target}"

        proc = run_cli(db, "check", "--url", url)
        record = self.assert_single_json_line(proc)

        # 状态码归类（退出码 / status / reason / http_status）
        self.assert_status_classification(record, proc.returncode, code)
        self.assert_common_record_fields(record, url)

        # 只向登记路径发送一次 GET，路径与查询参数原样到达
        self.assertEqual(
            server.requests, [target],
            f"[状态码归类] {code}：必须只请求一次登记路径，查询参数原样到达",
        )

        # 落库与 recent 只读往返
        self.assert_persistence_and_recent(db, record, server, [target])

    # ---- 2xx：200 / 204 / 299 均为成功 ----

    def test_success_family_200_204_299(self):
        for code in SUCCESS_CODES:
            with self.subTest(code=code):
                self._run_one_status_scenario(code)

    # ---- 非 2xx：300 / 404 / 500 均为已记录失败 ----

    def test_failure_family_300_404_500(self):
        for code in FAILURE_CODES:
            with self.subTest(code=code):
                self._run_one_status_scenario(code)

    # ---- 302：记录为失败且绝不跟随到 /landing ----

    def test_redirect_302_recorded_as_failure_without_follow(self):
        server = self._start()
        port = server.server_address[1]
        db = self.tmp / "redirect.sqlite"
        target = "/health?case=302"
        url = f"http://127.0.0.1:{port}{target}"

        # 先证明重定向目标本身可达：测试进程直接访问 /landing 会得到 200
        self.assertEqual(get_once(port, "/landing"), 200)
        self.assertEqual(server.requests, ["/landing"])

        # 清空测试探测造成的计数，只观察 check 进程的行为
        server.requests.clear()

        proc = run_cli(db, "check", "--url", url)
        record = self.assert_single_json_line(proc)

        # 必须记录为 302 失败：不是成功、也不是连接错误
        self.assert_status_classification(record, proc.returncode, REDIRECT_CODE)
        self.assertEqual(record["reason"], "http_status")
        self.assertIsNotNone(record["http_status"])
        self.assert_common_record_fields(record, url)

        # 不跟随重定向：登记路径恰好一次，/landing 访问次数为零
        self.assert_redirect_not_followed(server, target, "/landing")

        # 落库内容仍是已记录的 302 失败；recent 同样不触发 /landing
        requests_after_check = list(server.requests)
        self.assert_persistence_and_recent(
            db, record, server, requests_after_check
        )
        # 再显式确认整个场景（含 recent）结束后 /landing 仍从未被 check 访问
        self.assertNotIn("/landing", server.requests)


if __name__ == "__main__":
    unittest.main(verbosity=2)
