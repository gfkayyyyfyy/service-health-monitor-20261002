#!/usr/bin/env python3
"""healthcheck.py check 的 --timeout 输入校验回归测试。

约定（--timeout 只接受有限正数秒）：
  * 非法值（0、-0、-1、nan、inf、-inf、1e309、abc、空字符串）必须由 argparse
    在参数解析阶段拒绝：退出码 2、stdout 为空、stderr 指出 --timeout 参数错误、
    无 Python 回溯；且在任何探测与数据库操作之前结束——不发请求、不建库建表、
    不新增失败记录；
  * 选取一个非法值（"0"）覆盖三种数据库状态：
      - 数据库与父目录均不存在：不产生任何文件或目录；
      - 已有有效数据库（含 checks 记录与无关表）：表结构与全部数据保持不变；
      - --db 指向目录：仍先报告 --timeout 参数错误；
  * 对照：省略 --timeout、--timeout 1、--timeout 1e0 面对立即返回 200 的本机
    服务，均退出码 0、stderr 为空，只发一次 GET、新增一条 success/ok 记录，
    stdout 单行 JSON 与落库记录完全一致；recent 能读回该记录且不发新请求、
    不新增记录。

全部使用独立临时目录中的数据库与 127.0.0.1 临时端口服务，不访问公网、
不依赖固定端口或既有历史。
运行：python3 test_check_timeout_validation.py
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

# 公开约定“有限正数秒”之外的全部非法输入
INVALID_TIMEOUTS = ["0", "-0", "-1", "nan", "inf", "-inf", "1e309", "abc", ""]

# 三种数据库状态测试统一选用的非法值
CHOSEN_INVALID = "0"

RECORD_FIELDS = ("id", "url", "checked_at", "elapsed_ms",
                 "status", "http_status", "reason")

LEGACY_SCHEMA = """
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

RECORD_1 = (
    1,
    "http://127.0.0.1:8765/health?detail=1",
    "2026-10-02T04:40:00.000000+00:00",
    3,
    "success",
    200,
    "ok",
)
RECORD_2 = (
    2,
    "http://127.0.0.1:8765/health?detail=1",
    "2026-10-02T04:40:05.000000+00:00",
    1,
    "failure",
    None,
    "connection_error",
)


def run_cli(db_path, *args):
    """以子进程运行 healthcheck.py，返回 CompletedProcess。"""
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *args]
    return subprocess.run(cmd, capture_output=True, text=True)


def run_check(db_path, url, timeout):
    """运行 check；timeout 为 None 时省略 --timeout，否则显式传入。"""
    extra = [] if timeout is None else ["--timeout", timeout]
    return run_cli(db_path, "check", "--url", url, *extra)


def checks_rows(path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(
            "SELECT id, url, checked_at, elapsed_ms, status, "
            "http_status, reason FROM checks ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def db_snapshot(path):
    """数据库完整快照：sqlite_master 中的结构 + 各表全部数据。

    用于证明非法 --timeout 被拒后表结构与任何数据都未变化。
    """
    conn = sqlite3.connect(str(path))
    try:
        schema = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "ORDER BY type, name"
        ).fetchall()
        data = {}
        for item_type, name, _tbl_name, _sql in schema:
            if item_type == "table":
                data[name] = conn.execute(
                    f'SELECT * FROM "{name}"'
                ).fetchall()
        return schema, data
    finally:
        conn.close()


def build_sample_db(path):
    """两条 checks 历史 + 一个无关表（含 sqlite_sequence 由 AUTOINCREMENT 产生）。"""
    conn = sqlite3.connect(str(path))
    conn.execute(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
        "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        RECORD_1,
    )
    conn.execute(
        "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
        "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        RECORD_2,
    )
    conn.execute("CREATE TABLE other_t (name TEXT, n INTEGER)")
    conn.execute("INSERT INTO other_t VALUES ('kept', 42)")
    conn.commit()
    conn.close()


class _FastHandler(http.server.BaseHTTPRequestHandler):
    """收到 GET 后立即返回 200，并记录请求路径。"""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append(self.path)
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FastHandler)
    server.daemon_threads = True
    server.requests = []  # 已收到的请求路径列表，用于核对请求次数与目标
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class _TestCaseBase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-tmo-valid-"))
        self._servers = []

    def tearDown(self):
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

    def assert_timeout_argument_error(self, proc):
        """非法 --timeout 的统一约定：

        退出码 2、stdout 为空、错误来自 argparse 对 --timeout 参数的拒绝
        （usage + argument --timeout），而非运行期 die() 或 Python 回溯。
        """
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("usage:", proc.stderr)
        self.assertIn("argument --timeout", proc.stderr)
        # 不能是运行期 die() 错误或未捕获异常
        self.assertNotIn("healthcheck: error:", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


class TimeoutValidationTests(_TestCaseBase):
    """非法 --timeout：解析阶段拒绝，先于探测与数据库操作。"""

    def test_all_invalid_values_rejected_before_probe(self):
        server = self._start()
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/probe?case=bad-timeout"

        for index, value in enumerate(INVALID_TIMEOUTS):
            with self.subTest(value=value):
                db = self.tmp / f"invalid-{index}.sqlite"
                proc = run_check(db, url, value)
                self.assert_timeout_argument_error(proc)

                # 存活的本机服务未收到任何请求
                self.assertEqual(server.requests, [])
                # 未创建数据库文件或任何旁路文件
                self.assertFalse(db.exists())
                self.assertEqual(list(self.tmp.iterdir()), [])

    def test_chosen_invalid_is_zero(self):
        # 固定本文件三种数据库状态测试所选用的非法值，确保它确实在非法集合中
        self.assertIn(CHOSEN_INVALID, INVALID_TIMEOUTS)

    def test_missing_db_and_parent_nothing_created(self):
        server = self._start()
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/probe?case=missing-parent"

        missing_root = self.tmp / "does-not-exist"
        db = missing_root / "nested" / "monitor.sqlite"
        self.assertFalse(os.path.exists(missing_root))

        proc = run_check(db, url, CHOSEN_INVALID)
        self.assert_timeout_argument_error(proc)

        # 不产生数据库文件，也不产生任何（父）目录
        self.assertFalse(os.path.exists(missing_root))
        self.assertEqual(list(self.tmp.iterdir()), [])
        # 未发出探测请求
        self.assertEqual(server.requests, [])

    def test_existing_db_schema_and_data_untouched(self):
        server = self._start()
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/probe?case=existing-db"

        db = self.tmp / "monitor.sqlite"
        build_sample_db(db)
        schema_before, data_before = db_snapshot(db)
        rows_before = checks_rows(db)
        self.assertEqual(rows_before, [RECORD_1, RECORD_2])

        proc = run_check(db, url, CHOSEN_INVALID)
        self.assert_timeout_argument_error(proc)

        # checks 记录数量与内容不变（无失败记录新增）
        self.assertEqual(checks_rows(db), rows_before)
        # 表结构（sqlite_master）与各表全部数据完全不变
        schema_after, data_after = db_snapshot(db)
        self.assertEqual(schema_after, schema_before)
        self.assertEqual(data_after, data_before)
        # 不产生 -wal/-journal 等旁路文件
        self.assertEqual(
            {p.name for p in self.tmp.iterdir()}, {"monitor.sqlite"}
        )
        # 未发出探测请求
        self.assertEqual(server.requests, [])

    def test_directory_db_path_reports_timeout_error_first(self):
        server = self._start()
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/probe?case=db-is-dir"

        # --db 指向一个已存在的目录；--timeout 错误必须先于数据库错误报告
        proc = run_check(self.tmp, url, CHOSEN_INVALID)
        self.assert_timeout_argument_error(proc)

        # 目录原样存在，内部未产生任何文件
        self.assertTrue(os.path.isdir(self.tmp))
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assertEqual(server.requests, [])


class ValidTimeoutControlTests(_TestCaseBase):
    """合法输入对照：省略、1、1e0 均正常探测、落库并可被 recent 读回。"""

    def test_valid_timeout_variants_success(self):
        variants = [
            ("omit", None),
            ("one", "1"),
            ("scientific", "1e0"),
        ]
        for label, timeout in variants:
            with self.subTest(label=label, timeout=timeout):
                server = self._start()
                port = server.server_address[1]
                db = self.tmp / f"ok-{label}.sqlite"
                url = f"http://127.0.0.1:{port}/ok?case=valid-timeout"

                proc = run_check(db, url, timeout)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")

                # stdout 恰好一行可解析 JSON，字段集合固定
                lines = proc.stdout.splitlines()
                self.assertEqual(len(lines), 1, proc.stdout)
                record = json.loads(lines[0])
                self.assertEqual(sorted(record.keys()),
                                 sorted(RECORD_FIELDS))

                # 约定字段
                self.assertEqual(record["status"], "success")
                self.assertEqual(record["reason"], "ok")
                self.assertEqual(record["http_status"], 200)
                # url 保留原始字符串
                self.assertEqual(record["url"], url)
                self.assertEqual(record["id"], 1)

                # 耗时为非负整数（排除 bool 伪装成 int）
                self.assertIsInstance(record["elapsed_ms"], int)
                self.assertNotIsInstance(record["elapsed_ms"], bool)
                self.assertGreaterEqual(record["elapsed_ms"], 0)

                # checked_at 带 UTC 时区（偏移为 0）
                checked_at = datetime.fromisoformat(record["checked_at"])
                self.assertIsNotNone(checked_at.tzinfo)
                self.assertEqual(checked_at.utcoffset(), timedelta(0))

                # 且仅发送一次 GET，目标路径与查询原样
                self.assertEqual(server.requests, ["/ok?case=valid-timeout"])

                # 新增恰一条记录，落库内容与 stdout 完全一致
                rows = checks_rows(db)
                self.assertEqual(len(rows), 1)
                self.assertEqual(dict(zip(RECORD_FIELDS, rows[0])), record)

                # recent 读回同一记录：不发新请求、不新增记录
                rows_before = checks_rows(db)
                recent_proc = run_cli(db, "recent")
                self.assertEqual(recent_proc.returncode, 0,
                                 recent_proc.stderr)
                self.assertEqual(recent_proc.stderr, "")
                self.assertEqual(json.loads(recent_proc.stdout), [record])

                self.assertEqual(checks_rows(db), rows_before)
                self.assertEqual(server.requests,
                                 ["/ok?case=valid-timeout"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
