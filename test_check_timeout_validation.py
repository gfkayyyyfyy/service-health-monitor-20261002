#!/usr/bin/env python3
"""healthcheck.py check --timeout 输入校验的回归测试（命令行 → SQLite）。

公开约定：--timeout 只接受有限正数秒；非法值在参数解析阶段即被拒绝，
退出码 2、stdout 为空、stderr 指出 --timeout 参数错误且无 Python 回溯，
不探测、不打开/创建数据库、不写任何记录。

覆盖：
  * 非法输入 0、-0、-1、nan、inf、-inf、1e309、abc、空字符串：
    面对正在监听的本机服务，请求计数为零、不产生数据库文件；
  * 以非法值 "0" 覆盖三种数据库状态：
      - 数据库与父目录均不存在 → 不产生任何文件或目录；
      - 已有有效数据库（含检查记录与无关表）→ 表结构与全部数据原样不动；
      - 数据库路径指向目录 → 仍优先报告 --timeout 参数错误；
  * 合法对照：省略 --timeout、传入 1、传入 1e0，对立即返回 200 的本机
    服务均退出码 0、stderr 为空、恰好一次 GET、恰好新增一条记录，
    stdout 单行 JSON 与落库记录完全一致，recent 只读读回同一记录。

全部使用独立临时目录与本机 127.0.0.1 可用端口，不访问公网、
不依赖固定端口或既有历史，也不要求测得固定耗时。
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

# 全部应被拒绝的 --timeout 取值：非正数、非有限值、非数字、空串
INVALID_TIMEOUTS = ["0", "-0", "-1", "nan", "inf", "-inf", "1e309", "abc", ""]

# 三种数据库状态共用的代表非法值
REPRESENTATIVE_INVALID = "0"

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


def run_check(db_path, url, *extra):
    """以子进程运行 check，返回 CompletedProcess。"""
    cmd = [
        sys.executable, str(SCRIPT), "--db", str(db_path),
        "check", "--url", url, *extra,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def run_cli(db_path, *args):
    """以子进程运行 healthcheck.py 任意子命令，返回 CompletedProcess。"""
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


def dump_schema_and_data(db_path):
    """快照全部表结构（sqlite_master 原文）与每张表的全部数据。"""
    conn = sqlite3.connect(str(db_path))
    try:
        schema = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        data = {}
        for _type, name, _sql in schema:
            if _type == "table":
                data[name] = conn.execute(
                    f"SELECT * FROM \"{name}\" ORDER BY rowid"
                ).fetchall()
        return schema, data
    finally:
        conn.close()


def build_sample_db(path):
    """已有有效数据库：README 示例两条历史 + 一个无关表。"""
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
    """记录请求路径并立即返回 200，抑制日志噪音。"""

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
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _FastHandler
    )
    server.daemon_threads = True
    server.requests = []  # 已收到的请求路径列表，用于核对请求次数与目标
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class InvalidTimeoutTests(unittest.TestCase):
    """非法 --timeout：参数解析阶段拒绝，不探测、不触碰数据库。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-badtimeout-test-"))
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

    # ---- 公共断言 ----

    def assert_timeout_error(self, proc):
        """非法 --timeout 约定：退出码 2、stdout 空、stderr 指出 --timeout
        参数错误（argparse 报告），且不出现 Python 回溯。"""
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("error", proc.stderr)
        self.assertIn("--timeout", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    # ---- 每种非法取值：面对存活的本机服务也不发请求、不建库 ----

    def test_all_invalid_values_rejected_before_probe_and_db(self):
        server = self._start()
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/ok"

        for index, value in enumerate(INVALID_TIMEOUTS):
            db = self.tmp / f"case-{index}.sqlite"
            with self.subTest(timeout=value):
                proc = run_check(db, url, "--timeout", value)
                self.assert_timeout_error(proc)
                # 不创建数据库文件（含 -wal/-journal 旁路文件）
                self.assertFalse(db.exists())
                self.assertFalse(db.with_suffix(".sqlite-wal").exists())
                self.assertFalse(db.with_suffix(".sqlite-journal").exists())

        # 正在监听的本机服务未收到任何请求
        self.assertEqual(server.requests, [])
        # 临时目录中不产生任何文件
        self.assertEqual(list(self.tmp.iterdir()), [])

    # ---- 数据库状态一：数据库与父目录均不存在 ----

    def test_missing_db_and_parent_nothing_created(self):
        server = self._start()
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/ok"
        missing_root = self.tmp / "does-not-exist"
        db = missing_root / "nested" / "monitor.sqlite"
        self.assertFalse(os.path.exists(missing_root))

        proc = run_check(db, url, "--timeout", REPRESENTATIVE_INVALID)
        self.assert_timeout_error(proc)

        # 不产生数据库文件，也不产生任何（父）目录
        self.assertFalse(os.path.exists(missing_root))
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assertEqual(server.requests, [])

    # ---- 数据库状态二：已有有效数据库含检查记录与无关表 ----

    def test_existing_db_schema_and_data_untouched(self):
        server = self._start()
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/ok"
        db = self.tmp / "monitor.sqlite"
        build_sample_db(db)
        schema_before, data_before = dump_schema_and_data(db)
        self.assertEqual(data_before["checks"], [RECORD_1, RECORD_2])

        proc = run_check(db, url, "--timeout", REPRESENTATIVE_INVALID)
        self.assert_timeout_error(proc)

        # 表结构与全部数据（checks 旧记录、无关表内容）保持不变
        self.assertEqual(dump_schema_and_data(db), (schema_before, data_before))
        # 不产生 -wal/-journal 旁路文件
        self.assertEqual(
            {p.name for p in self.tmp.iterdir()}, {"monitor.sqlite"}
        )
        self.assertEqual(server.requests, [])

    # ---- 数据库状态三：数据库路径指向目录 ----

    def test_directory_db_path_reports_timeout_error_first(self):
        server = self._start()
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/ok"
        db_dir = self.tmp / "db-as-dir"
        db_dir.mkdir()

        proc = run_check(db_dir, url, "--timeout", REPRESENTATIVE_INVALID)
        # --timeout 参数错误优先于数据库路径错误
        self.assert_timeout_error(proc)

        # 目录保持为空，服务未收到请求
        self.assertEqual(list(db_dir.iterdir()), [])
        self.assertEqual(server.requests, [])


class ValidTimeoutTests(unittest.TestCase):
    """合法 --timeout（省略、1、1e0）：照常探测并落库，recent 只读读回。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-oktimeout-test-"))
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

    def _run_valid_case(self, tag, timeout_args):
        server = self._start()
        port = server.server_address[1]
        db = self.tmp / f"{tag}.sqlite"
        url = f"http://127.0.0.1:{port}/ok?case={tag}"

        proc = run_check(db, url, *timeout_args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")

        # stdout 恰好一行 JSON，字段齐全
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1, proc.stdout)
        record = json.loads(lines[0])
        self.assertEqual(sorted(record.keys()), sorted(RECORD_FIELDS))

        self.assertEqual(record["status"], "success")
        self.assertEqual(record["reason"], "ok")
        self.assertEqual(record["http_status"], 200)
        # url 保留原始字符串
        self.assertEqual(record["url"], url)
        # 耗时为非负整数（不要求具体取值）
        self.assertIsInstance(record["elapsed_ms"], int)
        self.assertNotIsInstance(record["elapsed_ms"], bool)
        self.assertGreaterEqual(record["elapsed_ms"], 0)
        # 检查时间带 UTC 时区
        checked_at = datetime.fromisoformat(record["checked_at"])
        self.assertIsNotNone(checked_at.tzinfo)
        self.assertEqual(checked_at.utcoffset(), timedelta(0))

        # 恰好一次 GET，目标为登记的路径与查询
        self.assertEqual(server.requests, [f"/ok?case={tag}"])

        # 恰好新增一条记录，且与 stdout 完全一致
        rows = read_checks_rows(db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows_as_records(rows), [record])

        # recent 只读读回同一记录：不发新请求、不新增记录
        rows_before = read_checks_rows(db)
        requests_before = list(server.requests)
        recent = run_cli(db, "recent")
        self.assertEqual(recent.returncode, 0, recent.stderr)
        self.assertEqual(recent.stderr, "")
        self.assertEqual(json.loads(recent.stdout), [record])
        self.assertEqual(read_checks_rows(db), rows_before)
        self.assertEqual(server.requests, requests_before)

    def test_default_timeout_omitted(self):
        self._run_valid_case("default", [])

    def test_timeout_one_second(self):
        self._run_valid_case("one", ["--timeout", "1"])

    def test_timeout_scientific_notation(self):
        self._run_valid_case("sci", ["--timeout", "1e0"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
