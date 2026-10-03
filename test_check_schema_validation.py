#!/usr/bin/env python3
"""healthcheck.py check 对既有 checks 表缺字段的拒绝与新建库的回归测试。

两个对照场景（命令行 → SQLite，全部通过子进程运行 healthcheck.py 观察）：

  * 缺字段拒绝：既有 checks 表只有 id 一列（已有 id=7 的行）时，
    check 在探测前以退出码 2 拒绝——标准输出为空、标准错误列出
    缺少的字段、无 Python 回溯；服务没有收到任何请求，不新增记录，
    不补列或重建表，原有表定义与全部数据保持原样。
    附带核对：非法 URL / 非法 timeout 仍优先于缺字段错误报告；
    字段名大小写不同、列顺序不同、存在额外列的表不触发拒绝。

  * 正常建库：父目录已存在、数据库文件尚不存在时，check 自动建库
    建表，服务返回 200 恰好收到一次 GET，恰好新增一条与 stdout
    逐字段一致的成功记录，以退出码 0 结束。

服务只绑定 127.0.0.1，不访问公网，不依赖固定空闲端口或既有历史文件；
每次运行自行准备并释放服务、子进程与临时资源，重复运行互不影响。
运行：python3 -m unittest test_check_schema_validation
"""

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT = pathlib.Path(__file__).resolve().parent / "healthcheck.py"

TARGET = "/health"

REQUIRED_COLUMNS = ("id", "url", "checked_at", "elapsed_ms",
                    "status", "http_status", "reason")

# bad.sqlite 中 checks 表只有 id 一列，其余字段全部缺失
MISSING_COLUMNS = tuple(col for col in REQUIRED_COLUMNS if col != "id")

BAD_TABLE_SQL = "CREATE TABLE checks (id INTEGER PRIMARY KEY)"


def run_cli(db_path, *args):
    """以子进程运行 healthcheck.py，返回 CompletedProcess。"""
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *args]
    return subprocess.run(cmd, capture_output=True, text=True)


class _OkHandler(BaseHTTPRequestHandler):
    """对任何 GET 返回 200，并把请求路径记入 server.get_paths。"""

    def do_GET(self):
        self.server.get_paths.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass  # 静默，避免污染测试输出


def start_ok_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OkHandler)
    server.daemon_threads = True
    server.get_paths = []  # 已收到的 GET 路径列表
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class SchemaValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-schema-test-"))
        self.server, self.thread = start_ok_server()
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}{TARGET}"

    def tearDown(self):
        # 断言失败也必须释放本机服务与临时数据
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                os.chmod(os.path.join(root, name), stat.S_IRWXU)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 场景一：既有 checks 表缺字段，探测前拒绝 ----

    def _make_bad_db(self):
        """checks 表只有 id INTEGER PRIMARY KEY 一列，已有 id=7 的行。"""
        db = self.tmp / "bad.sqlite"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(BAD_TABLE_SQL)
            conn.execute("INSERT INTO checks (id) VALUES (7)")
            conn.commit()
        finally:
            conn.close()
        return db

    def _read_schema_and_rows(self, db):
        conn = sqlite3.connect(str(db))
        try:
            table_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='checks'"
            ).fetchone()[0]
            rows = conn.execute("SELECT * FROM checks ORDER BY id").fetchall()
            return table_sql, rows
        finally:
            conn.close()

    def test_missing_columns_rejected_before_probe(self):
        db = self._make_bad_db()
        schema_before, rows_before = self._read_schema_and_rows(db)

        proc = run_cli(db, "check", "--url", self.url)

        self.assertEqual(
            proc.returncode, 2,
            f"[拒绝] 缺字段时必须以退出码 2 结束，stderr: {proc.stderr}",
        )
        self.assertEqual(proc.stdout, "", "[拒绝] 标准输出必须为空")
        self.assertNotIn(
            "Traceback", proc.stderr, "[拒绝] 标准错误不得出现 Python 回溯"
        )
        for col in MISSING_COLUMNS:
            self.assertIn(
                col, proc.stderr,
                f"[拒绝] 标准错误必须说明缺少字段 {col}",
            )

        self.assertEqual(
            list(self.server.get_paths), [],
            "[拒绝] 缺字段时不得发送任何网络请求",
        )

        schema_after, rows_after = self._read_schema_and_rows(db)
        self.assertEqual(
            schema_after, schema_before,
            "[拒绝] 不得补列或重建表，表定义必须保持原样",
        )
        self.assertEqual(
            rows_after, rows_before,
            "[拒绝] 不得新增或改动记录，原有数据必须保持原样",
        )
        self.assertEqual(rows_after, [(7,)], "[拒绝] 原有 id=7 的行必须保留")

    def test_invalid_url_takes_priority_over_missing_columns(self):
        db = self._make_bad_db()

        proc = run_cli(db, "check", "--url", "http://example.com/")

        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("127.0.0.1", proc.stderr)
        self.assertNotIn(
            "缺少必需字段", proc.stderr,
            "[优先级] 非法 URL 必须先于缺字段错误报告",
        )
        self.assertEqual(list(self.server.get_paths), [])

    def test_invalid_timeout_takes_priority_over_missing_columns(self):
        db = self._make_bad_db()

        proc = run_cli(db, "check", "--url", self.url, "--timeout", "0")

        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertNotIn("Traceback", proc.stderr)
        self.assertNotIn(
            "缺少必需字段", proc.stderr,
            "[优先级] 非法 timeout 必须先于缺字段错误报告",
        )
        self.assertEqual(list(self.server.get_paths), [])

    def test_case_order_and_extra_columns_not_rejected(self):
        """列名大小写不同、顺序不同、存在额外列的表应正常使用。"""
        db = self.tmp / "variant.sqlite"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "CREATE TABLE checks ("
                "reason TEXT NOT NULL, "
                "HTTP_STATUS INTEGER, "
                "Status TEXT NOT NULL, "
                "elapsed_ms INTEGER NOT NULL, "
                "checked_at TEXT NOT NULL, "
                "URL TEXT NOT NULL, "
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "note TEXT)"
            )
            conn.commit()
        finally:
            conn.close()

        proc = run_cli(db, "check", "--url", self.url)

        self.assertEqual(
            proc.returncode, 0,
            f"[兼容] 大小写/顺序/额外列不应触发拒绝，stderr: {proc.stderr}",
        )
        self.assertEqual(proc.stderr, "")
        record = json.loads(proc.stdout)
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["http_status"], 200)
        self.assertEqual(list(self.server.get_paths), [TARGET])

    # ---- 场景二：数据库文件尚不存在，自动建库建表 ----

    def test_fresh_database_created_with_single_success_record(self):
        db = self.tmp / "monitor.sqlite"
        self.assertFalse(db.exists(), "[建库] 前置：数据库文件必须尚不存在")

        proc = run_cli(db, "check", "--url", self.url)

        self.assertEqual(
            proc.returncode, 0,
            f"[建库] 服务返回 200 时必须以退出码 0 结束，stderr: {proc.stderr}",
        )
        self.assertEqual(proc.stderr, "")
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1, "[建库] stdout 必须恰有一行 JSON")
        record = json.loads(lines[0])
        self.assertEqual(
            sorted(record.keys()), sorted(REQUIRED_COLUMNS),
            "[建库] JSON 字段集合必须与约定一致",
        )
        self.assertEqual(record["id"], 1)
        self.assertEqual(record["url"], self.url)
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["http_status"], 200)
        self.assertEqual(record["reason"], "ok")
        self.assertIsInstance(record["elapsed_ms"], int)
        self.assertGreaterEqual(record["elapsed_ms"], 0)

        self.assertEqual(
            list(self.server.get_paths), [TARGET],
            "[建库] 服务必须恰好收到一次 GET，路径原样到达",
        )

        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT id, url, checked_at, elapsed_ms, status, "
                "http_status, reason FROM checks ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1, "[建库] 必须恰好新增一条记录")
        self.assertEqual(
            dict(zip(REQUIRED_COLUMNS, rows[0])), record,
            "[建库] 落库记录必须与 stdout 逐字段一致",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
