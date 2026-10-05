#!/usr/bin/env python3
"""streak 查询不发起探测的回归测试。

用一个绑定 127.0.0.1、端口由本机分配、路径为 /health 的本地演示 HTTP
服务观察请求：服务实际响应并记录收到的请求次数，每个用例开始前都先以
一次真实 GET 验证服务可用且计数生效，再归零计数执行 streak 查询——
「查询未发起探测」的结论来自服务端的零请求计数，而非仅凭输出内容推断。

覆盖：
- 样本一：同一目标 id 1-3 依次为 success/failure/failure，服务当前返回
  200。省略阈值时查询返回 latest_id 3、consecutive_failures 2；
  --threshold 2 时返回整数 threshold 2、threshold_reached true；
  两种查询服务请求数始终为零。
- 样本二：上述历史之后保存 id 4 success，服务当前返回 503。查询返回
  latest_id 4、consecutive_failures 0，--threshold 2 判断为 false，
  服务请求数仍为零。
- 无历史：数据库及其父目录不存在时返回输入 URL 原文、latest_id null、
  连续失败 0，--threshold 2 判断为 false；不创建路径、不请求服务。
- 参数拒绝：--threshold 0 退出码 2、stdout 为空、stderr 含 threshold，
  服务请求数仍为零。
- 正常查询均以退出码 0 结束、stderr 为空、stdout 恰好一行紧凑 JSON。

只使用临时 SQLite 数据与明确登记的本机地址，不依赖公网或固定端口；
测试结束释放全部临时资源。check、recent 与原有 streak 行为不受影响。

运行：python3 -m unittest test_streak_no_probe
"""

import http.client
import http.server
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "healthcheck.py"

SCHEMA = """
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


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """记录请求次数并按服务当前状态码响应的演示处理器。"""

    def do_GET(self):
        self.server.request_count += 1
        body = b"demo\n"
        self.send_response(self.server.status_code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # 保持测试输出安静


class DemoServer:
    """绑定 127.0.0.1、端口由系统分配、可记录请求次数的本地演示服务。"""

    def __init__(self):
        self.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _RecordingHandler)
        self.httpd.request_count = 0
        self.httpd.status_code = 200
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/health"
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        return False

    @property
    def request_count(self):
        return self.httpd.request_count

    def set_status(self, status_code):
        self.httpd.status_code = status_code

    def reset_count(self):
        self.httpd.request_count = 0


def run_streak(db_path, url, *extra):
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path),
           "streak", "--url", url, *extra]
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8")


def build_db(path, rows):
    """rows: (id, url, status, http_status, reason) 元组序列。"""
    conn = sqlite3.connect(str(path))
    conn.execute(SCHEMA)
    conn.executemany(
        "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
        "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(i, u, f"2026-10-04T00:00:{i:02d}.000000+00:00", 1, st, hs, rs)
         for (i, u, st, hs, rs) in rows],
    )
    conn.commit()
    conn.close()


def append_row(path, record_id, url, status, http_status, reason):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
        "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (record_id, url,
         f"2026-10-04T00:00:{record_id:02d}.000000+00:00", 1,
         status, http_status, reason),
    )
    conn.commit()
    conn.close()


class StreakNoProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-streak-noprobe-"))
        self.server = DemoServer()
        self.server.__enter__()
        self.url = self.server.url
        # 先以一次真实 GET 验证演示服务确实响应并记录请求，
        # 使后续「零请求」结论建立在可观察的服务端计数上
        self.assertEqual(self._probe_status(), 200)
        self.assertEqual(self.server.request_count, 1)
        self.server.reset_count()

    def tearDown(self):
        self.server.__exit__()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _probe_status(self):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.port, timeout=5)
        try:
            conn.request("GET", "/health")
            return conn.getresponse().status
        finally:
            conn.close()

    def assertQueryOk(self, proc, expected):
        """正常查询：退出码 0、stderr 为空、stdout 恰好一行紧凑 JSON。"""
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertTrue(proc.stdout.endswith("\n"))
        self.assertEqual(proc.stdout.count("\n"), 1)
        self.assertEqual(json.loads(proc.stdout), expected)

    def assertNoRequests(self):
        self.assertEqual(self.server.request_count, 0)

    # ---- 样本一：最新两条失败，服务当前返回 200 ----

    def test_failure_streak_queries_do_not_probe(self):
        db = self.tmp / "m.sqlite"
        build_db(db, [
            (1, self.url, "success", 200, "ok"),
            (2, self.url, "failure", 500, "http_status"),
            (3, self.url, "failure", None, "timeout"),
        ])
        self.server.set_status(200)

        # 省略阈值：原有三字段
        self.assertQueryOk(
            run_streak(db, self.url),
            {"url": self.url, "latest_id": 3, "consecutive_failures": 2})
        self.assertNoRequests()

        # --threshold 2：追加整数 threshold 与布尔 threshold_reached
        proc = run_streak(db, self.url, "--threshold", "2")
        self.assertQueryOk(
            proc,
            {"url": self.url, "latest_id": 3, "consecutive_failures": 2,
             "threshold": 2, "threshold_reached": True})
        data = json.loads(proc.stdout)
        self.assertIsInstance(data["threshold"], int)
        self.assertIsInstance(data["threshold_reached"], bool)
        self.assertNoRequests()

    # ---- 样本二：随后保存 id 4 success，服务当前返回 503 ----

    def test_latest_success_after_history_queries_do_not_probe(self):
        db = self.tmp / "m.sqlite"
        build_db(db, [
            (1, self.url, "success", 200, "ok"),
            (2, self.url, "failure", 500, "http_status"),
            (3, self.url, "failure", None, "timeout"),
        ])
        append_row(db, 4, self.url, "success", 200, "ok")
        self.server.set_status(503)
        # 服务确实以 503 响应（再验证一次服务可用后归零计数）
        self.assertEqual(self._probe_status(), 503)
        self.assertEqual(self.server.request_count, 1)
        self.server.reset_count()

        self.assertQueryOk(
            run_streak(db, self.url),
            {"url": self.url, "latest_id": 4, "consecutive_failures": 0})
        self.assertNoRequests()

        self.assertQueryOk(
            run_streak(db, self.url, "--threshold", "2"),
            {"url": self.url, "latest_id": 4, "consecutive_failures": 0,
             "threshold": 2, "threshold_reached": False})
        self.assertNoRequests()

    # ---- 无历史：数据库及其父目录不存在 ----

    def test_missing_db_and_parent_no_probe_no_creation(self):
        missing_parent = self.tmp / "nope"
        db = missing_parent / "deep" / "m.sqlite"

        self.assertQueryOk(
            run_streak(db, self.url),
            {"url": self.url, "latest_id": None,
             "consecutive_failures": 0})
        self.assertNoRequests()

        self.assertQueryOk(
            run_streak(db, self.url, "--threshold", "2"),
            {"url": self.url, "latest_id": None,
             "consecutive_failures": 0,
             "threshold": 2, "threshold_reached": False})
        self.assertNoRequests()

        # 不创建数据库路径及其父目录
        self.assertFalse(os.path.exists(missing_parent))

    # ---- 参数拒绝：--threshold 0 ----

    def test_threshold_zero_rejected_before_any_probe(self):
        db = self.tmp / "m.sqlite"
        build_db(db, [(1, self.url, "failure", None, "timeout")])

        proc = run_streak(db, self.url, "--threshold", "0")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(proc.stdout, "")
        self.assertIn("threshold", proc.stderr)
        self.assertNoRequests()


if __name__ == "__main__":
    unittest.main(verbosity=2)
