#!/usr/bin/env python3
"""healthcheck.py check 状态码分类的回归测试（命令行 → SQLite → recent）。

覆盖收到合法 HTTP 响应后的归类行为：
  * 2xx（200/204/299）→ 退出码 0，success/ok，http_status 保留实际状态码；
  * 非 2xx（300/404/500）→ 退出码 1，failure/http_status，http_status 保留实际状态码；
  * 302 携带指向同机 /landing 的 Location：不跟随重定向，仍记 302 失败，
    /landing 的访问次数为零。

每个场景均核对：只向登记路径发一次 GET（路径与查询参数原样到达）、
stdout 恰有一行 JSON、stderr 为空、数据库恰好新增一条与输出逐字段一致的
记录、recent 只读返回同一记录且不发新请求、不改库。

全部使用独立临时目录中的数据库与绑定 127.0.0.1 的可控服务（端口由系统
分配并显式写入登记 URL），不访问公网、不依赖固定端口或既有历史。
运行：python3 test_check_status_classification.py
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

SCRIPT = pathlib.Path(__file__).resolve().parent / "healthcheck.py"

RECORD_FIELDS = ("id", "url", "checked_at", "elapsed_ms",
                 "status", "http_status", "reason")

# 归类失败 / 重定向被跟随 / 持久化不符 三类断言消息前缀，
# 让测试报告能直接区分失败性质
MSG_CLASSIFY = "状态码归类不符"
MSG_REDIRECT = "重定向访问不符"
MSG_PERSIST = "持久化结果不符"


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
    """记录请求路径并抑制日志噪音。"""

    def log_message(self, *args):
        pass

    def _count(self):
        self.server.requests.append(self.path)


class _StatusHandler(_BaseHandler):
    """对任何 GET 返回 server.status_code 指定的状态码（空响应体）。"""

    def do_GET(self):
        self._count()
        self.send_response(self.server.status_code)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _RedirectHandler(_BaseHandler):
    """登记路径返回 302 + Location: /landing；/landing 若被访问返回 200。"""

    def do_GET(self):
        self._count()
        if self.path == "/landing":
            self.server.landing_hits += 1
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        port = self.server.server_address[1]
        self.send_response(302)
        self.send_header("Location", f"http://127.0.0.1:{port}/landing")
        self.send_header("Content-Length", "0")
        self.end_headers()


def start_server(handler, **attrs):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    server.requests = []  # 已收到的请求路径（含查询参数），按到达顺序
    server.landing_hits = 0
    for name, value in attrs.items():
        setattr(server, name, value)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class CheckStatusClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-status-test-"))
        self._servers = []

    def tearDown(self):
        # 用例失败时同样释放本机服务与临时数据
        for server, thread in self._servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                os.chmod(os.path.join(root, name), stat.S_IRWXU)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start(self, handler, **attrs):
        server, thread = start_server(handler, **attrs)
        self._servers.append((server, thread))
        return server

    # ---- 公共断言 ----

    def assert_single_json_line(self, proc):
        """stdout 恰好是一条可解析的 JSON 记录，字段齐全。"""
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1, f"{MSG_PERSIST}: stdout 不是单行: "
                                        f"{proc.stdout!r}")
        record = json.loads(lines[0])
        self.assertEqual(sorted(record.keys()), sorted(RECORD_FIELDS),
                         f"{MSG_PERSIST}: 记录字段集合不符")
        return record

    def assert_common_record_fields(self, record, url):
        """原始 URL、UTC 检查时间、非负整数毫秒耗时。"""
        self.assertEqual(record["id"], 1,
                         f"{MSG_PERSIST}: 首条记录 id 应为 1")
        self.assertEqual(record["url"], url,
                         f"{MSG_PERSIST}: 未保留原始 URL")
        self.assertIsInstance(record["elapsed_ms"], int,
                              f"{MSG_PERSIST}: elapsed_ms 应为整数")
        self.assertNotIsInstance(record["elapsed_ms"], bool,
                                 f"{MSG_PERSIST}: elapsed_ms 不应为布尔")
        self.assertGreaterEqual(record["elapsed_ms"], 0,
                                f"{MSG_PERSIST}: elapsed_ms 应非负")
        checked_at = datetime.fromisoformat(record["checked_at"])
        self.assertIsNotNone(checked_at.tzinfo,
                             f"{MSG_PERSIST}: checked_at 应带时区")
        self.assertEqual(checked_at.utcoffset(), timedelta(0),
                         f"{MSG_PERSIST}: checked_at 应为 UTC")

    def assert_db_matches(self, db, record):
        """数据库恰含一条记录，且与 check 的 stdout 逐字段一致。"""
        rows = read_checks_rows(db)
        self.assertEqual(len(rows), 1,
                         f"{MSG_PERSIST}: 数据库应恰好新增一条记录，"
                         f"实际 {len(rows)} 条")
        self.assertEqual(rows_as_records(rows), [record],
                         f"{MSG_PERSIST}: 落库内容与 stdout 不一致")

    def assert_recent_roundtrip(self, db, record, server):
        """recent 退出码 0 返回同一记录，期间不改库、不发新请求。"""
        rows_before = read_checks_rows(db)
        requests_before = list(server.requests)
        landing_before = server.landing_hits

        proc = run_cli(db, "recent")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(json.loads(proc.stdout), [record],
                         f"{MSG_PERSIST}: recent 返回与已落库记录不符")

        self.assertEqual(read_checks_rows(db), rows_before,
                         f"{MSG_PERSIST}: recent 改变了数据库内容")
        self.assertEqual(server.requests, requests_before,
                         f"{MSG_REDIRECT}: recent 期间服务收到新请求")
        self.assertEqual(server.landing_hits, landing_before,
                         f"{MSG_REDIRECT}: recent 期间 /landing 被访问")

    def run_status_case(self, code):
        """对给定状态码执行一次完整场景，返回 (record, server, db)。"""
        server = self._start(_StatusHandler, status_code=code)
        port = server.server_address[1]
        db = self.tmp / f"status-{code}.sqlite"
        url = f"http://127.0.0.1:{port}/health?case=status"

        proc = run_cli(db, "check", "--url", url)
        self.assertEqual(proc.stderr, "",
                         f"{MSG_CLASSIFY}: stderr 应为空: {proc.stderr!r}")

        record = self.assert_single_json_line(proc)
        self.assert_common_record_fields(record, url)

        # 只向登记路径发一次 GET，路径与查询参数原样到达
        self.assertEqual(server.requests, ["/health?case=status"],
                         f"{MSG_CLASSIFY}: 请求次数或目标不符: "
                         f"{server.requests!r}")
        return proc, record, server, db

    # ---- 2xx → success/ok，退出码 0 ----

    def test_2xx_statuses_classified_success(self):
        for code in (200, 204, 299):
            with self.subTest(code=code):
                proc, record, server, db = self.run_status_case(code)
                self.assertEqual(proc.returncode, 0,
                                 f"{MSG_CLASSIFY}: {code} 应退出 0，"
                                 f"实际 {proc.returncode}")
                self.assertEqual(record["status"], "success",
                                 f"{MSG_CLASSIFY}: {code} 应为 success")
                self.assertEqual(record["reason"], "ok",
                                 f"{MSG_CLASSIFY}: {code} 的 reason 应为 ok")
                self.assertEqual(record["http_status"], code,
                                 f"{MSG_CLASSIFY}: http_status 应保留 "
                                 f"实际状态码 {code}")
                self.assert_db_matches(db, record)
                self.assert_recent_roundtrip(db, record, server)

    # ---- 非 2xx → failure/http_status，退出码 1 ----

    def test_non_2xx_statuses_classified_failure(self):
        for code in (300, 404, 500):
            with self.subTest(code=code):
                proc, record, server, db = self.run_status_case(code)
                self.assertEqual(proc.returncode, 1,
                                 f"{MSG_CLASSIFY}: {code} 应退出 1，"
                                 f"实际 {proc.returncode}")
                self.assertEqual(record["status"], "failure",
                                 f"{MSG_CLASSIFY}: {code} 应为 failure")
                self.assertEqual(record["reason"], "http_status",
                                 f"{MSG_CLASSIFY}: {code} 的 reason 应为 "
                                 f"http_status")
                self.assertEqual(record["http_status"], code,
                                 f"{MSG_CLASSIFY}: http_status 应保留 "
                                 f"实际状态码 {code}")
                self.assert_db_matches(db, record)
                self.assert_recent_roundtrip(db, record, server)

    # ---- 302 重定向：不跟随，/landing 访问次数为零 ----

    def test_redirect_302_not_followed(self):
        server = self._start(_RedirectHandler)
        port = server.server_address[1]
        db = self.tmp / "redirect.sqlite"
        url = f"http://127.0.0.1:{port}/health?case=redirect"

        proc = run_cli(db, "check", "--url", url)
        self.assertEqual(proc.returncode, 1,
                         f"{MSG_CLASSIFY}: 302 应退出 1，"
                         f"实际 {proc.returncode}")
        self.assertEqual(proc.stderr, "",
                         f"{MSG_CLASSIFY}: stderr 应为空: {proc.stderr!r}")

        record = self.assert_single_json_line(proc)
        self.assertEqual(record["status"], "failure",
                         f"{MSG_CLASSIFY}: 302 应记为 failure 而非 success")
        self.assertEqual(record["reason"], "http_status",
                         f"{MSG_CLASSIFY}: 302 的 reason 应为 http_status "
                         f"而非 connection_error")
        self.assertEqual(record["http_status"], 302,
                         f"{MSG_CLASSIFY}: 应记录实际状态码 302")
        self.assert_common_record_fields(record, url)

        # 只访问登记路径一次；/landing 从未被访问（未跟随重定向）
        self.assertEqual(server.requests, ["/health?case=redirect"],
                         f"{MSG_REDIRECT}: 请求次数或目标不符: "
                         f"{server.requests!r}")
        self.assertEqual(server.landing_hits, 0,
                         f"{MSG_REDIRECT}: /landing 被访问了 "
                         f"{server.landing_hits} 次，应为 0")

        self.assert_db_matches(db, record)
        self.assert_recent_roundtrip(db, record, server)
        # recent 之后 /landing 仍未被访问
        self.assertEqual(server.landing_hits, 0,
                         f"{MSG_REDIRECT}: recent 后 /landing 被访问")


if __name__ == "__main__":
    unittest.main(verbosity=2)
