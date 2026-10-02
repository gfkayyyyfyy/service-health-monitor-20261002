#!/usr/bin/env python3
"""check 超时（timeout）从命令行到 SQLite 的端到端回归测试。

覆盖两类本机场景，均在独立临时目录中完成，不访问公网、不依赖既有数据库：

1. 可控本机服务收到 GET 后延迟 0.6 秒才发送完整响应头，check 以
   --timeout 0.2 发起：退出码 1、stdout 一行 failure/timeout/null JSON、
   stderr 为空，且仅落一条 checks 记录、不重试、不访问其他目标；随后
   recent 只读返回同一条记录且不触发新请求。
2. 对照：同一流程下及时返回 HTTP 200，空数据库中仅保存一条 success/ok
   记录，退出码 0。

运行：python3 test_check_timeout.py
"""

import http.server
import json
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta

SCRIPT = pathlib.Path(__file__).resolve().parent / "healthcheck.py"

SLOW_TARGET = "/slow?case=timeout"
OK_TARGET = "/ok"
SLOW_DELAY_SEC = 0.6      # 服务端至少延迟这么久才发出完整响应头
CHECK_TIMEOUT = "0.2"     # 检查端超时：0.2 秒，远小于 0.6 秒

RECORD_FIELDS = (
    "id", "url", "checked_at", "elapsed_ms",
    "status", "http_status", "reason",
)


class RecordingHandler(http.server.BaseHTTPRequestHandler):
    """记录请求路径；慢接口先睡足 SLOW_DELAY_SEC 再发完整响应头。"""

    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        # 保持测试输出干净；断言针对的是子进程 stderr，不是本服务日志
        pass

    def do_GET(self):
        self.server.record_request(self.path)
        if self.path == SLOW_TARGET:
            # 收到请求后不发送任何字节，睡过客户端超时点之后再发响应头
            time.sleep(SLOW_DELAY_SEC)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
            except OSError:
                # 客户端早已超时关闭连接：写入可能失败，不影响计数与服务
                return
            return

        body = b"ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RecordingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = threading.Lock()
        self._paths = []

    def record_request(self, path):
        with self._lock:
            self._paths.append(path)

    def request_paths(self):
        with self._lock:
            return list(self._paths)


def run_check(db_path, url, *extra):
    cmd = [
        sys.executable, str(SCRIPT), "--db", str(db_path),
        "check", "--url", url, *extra,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def run_recent(db_path):
    cmd = [
        sys.executable, str(SCRIPT), "--db", str(db_path), "recent",
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def fetch_check_rows(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT id, url, checked_at, elapsed_ms, status, "
            "http_status, reason FROM checks ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def record_to_tuple(record):
    return tuple(record[name] for name in RECORD_FIELDS)


class CheckTimeoutRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-timeout-"))
        self.server = RecordingHTTPServer(
            ("127.0.0.1", 0), RecordingHandler
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    def tearDown(self):
        # 无论断言是否失败，都释放本机服务并清理临时数据
        try:
            self.server.shutdown()
            self.server.server_close()
        finally:
            self.thread.join(timeout=2)
            shutil.rmtree(self.tmp, ignore_errors=True)

    def base_url(self, target):
        return f"http://127.0.0.1:{self.port}{target}"

    def parse_single_json_line(self, stdout):
        """stdout 必须只有一行可解析 JSON，且字段集合与既有契约一致。"""
        lines = stdout.splitlines()
        self.assertEqual(len(lines), 1, repr(stdout))
        record = json.loads(lines[0])
        self.assertEqual(set(record), set(RECORD_FIELDS))
        return record

    def assert_checked_at_utc(self, value):
        parsed = json.loads(json.dumps(value))  # 确认是可 JSON 表达的字符串
        self.assertIsInstance(parsed, str)
        dt = datetime.fromisoformat(parsed)
        self.assertIsNotNone(dt.tzinfo)
        self.assertEqual(dt.utcoffset(), timedelta(0))

    def assert_common_record_shape(self, record, url):
        self.assertEqual(record["id"], 1)
        self.assertEqual(record["url"], url)  # 与输入原文逐字一致
        self.assertIsInstance(record["elapsed_ms"], int)
        self.assertNotIsInstance(record["elapsed_ms"], bool)
        self.assertGreaterEqual(record["elapsed_ms"], 0)
        self.assert_checked_at_utc(record["checked_at"])
        # http_status 要么是整数状态码，要么是 None（JSON null）
        self.assertTrue(
            record["http_status"] is None
            or isinstance(record["http_status"], int)
        )

    def assert_one_row_matches(self, db_path, record):
        rows = fetch_check_rows(db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], record_to_tuple(record))
        return rows

    # ---- 场景一：超时记录为 failure/timeout，贯穿命令行与 SQLite ----

    def test_timeout_check_flows_cli_to_sqlite_and_recent(self):
        db = self.tmp / "timeout.sqlite"
        url = self.base_url(SLOW_TARGET)

        proc = run_check(db, url, "--timeout", CHECK_TIMEOUT)

        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(proc.stderr, "")
        record = self.parse_single_json_line(proc.stdout)
        self.assertEqual(record["status"], "failure")
        self.assertEqual(record["reason"], "timeout")
        self.assertIsNone(record["http_status"])
        self.assert_common_record_shape(record, url)

        # 超时只新增一条 checks 记录，内容与 stdout 完全一致
        rows = self.assert_one_row_matches(db, record)

        # 仅一次 GET，目标就是指定的慢接口，没有重试或其他访问
        self.assertEqual(self.server.request_paths(), [SLOW_TARGET])

        # recent 对同一数据库只读返回这一条记录
        proc_recent = run_recent(db)
        self.assertEqual(proc_recent.returncode, 0, proc_recent.stderr)
        self.assertEqual(proc_recent.stderr, "")
        recent_records = json.loads(proc_recent.stdout)
        self.assertEqual(recent_records, [record])

        # 查询前后记录数与内容不变
        self.assertEqual(fetch_check_rows(db), rows)
        # recent 不发任何网络请求
        self.assertEqual(self.server.request_paths(), [SLOW_TARGET])

    # ---- 场景二（对照）：及时 200 -> success/ok，空库仅一条记录 ----

    def test_prompt_200_control_success_single_record(self):
        db = self.tmp / "ok.sqlite"
        url = self.base_url(OK_TARGET)

        proc = run_check(db, url)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        record = self.parse_single_json_line(proc.stdout)
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["reason"], "ok")
        self.assertEqual(record["http_status"], 200)
        self.assert_common_record_shape(record, url)

        rows = self.assert_one_row_matches(db, record)
        self.assertEqual(self.server.request_paths(), [OK_TARGET])

        proc_recent = run_recent(db)
        self.assertEqual(proc_recent.returncode, 0, proc_recent.stderr)
        self.assertEqual(proc_recent.stderr, "")
        self.assertEqual(json.loads(proc_recent.stdout), [record])

        self.assertEqual(fetch_check_rows(db), rows)
        self.assertEqual(self.server.request_paths(), [OK_TARGET])


if __name__ == "__main__":
    unittest.main(verbosity=2)
