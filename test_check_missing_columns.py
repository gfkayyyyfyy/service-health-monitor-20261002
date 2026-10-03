#!/usr/bin/env python3
"""healthcheck.py check 探测前数据库结构校验的对照回归测试。

背景：checks 表已存在但缺少落库字段时，旧实现会先发送 GET，
直到写入阶段才报错。修复后必须在探测前拒绝，且不改动数据库。

两个核心对照场景（均只依赖本机、临时数据，不访问公网、
不依赖既有历史文件）：
  * 缺字段旧库 bad.sqlite（checks 只有 id 一列，已有 id=7 的行）：
    check 以退出码 2 结束、stdout 为空、stderr 列出缺失字段且无回溯，
    服务端没有收到任何请求，表结构与原有行保持原样；
  * 父目录存在、文件尚不存在的 monitor.sqlite：
    服务返回 200 时自动建库建表，只发送一次 GET，新增一条成功记录，
    stdout 的单行 JSON 与落库内容逐字段一致，退出码 0。

另覆盖：列顺序不同/存在额外列/列名大小写不同不拒绝；
非法 URL 与非法 timeout 的错误优先级高于数据库缺字段。

运行：python3 -m unittest test_check_missing_columns
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

SCRIPT = pathlib.Path(__file__).resolve().parent / "healthcheck.py"

RECORD_FIELDS = ("id", "url", "checked_at", "elapsed_ms",
                 "status", "http_status", "reason")

TARGET = "/health"


def run_cli(db_path, *args):
    """以子进程运行 healthcheck.py，返回 CompletedProcess。"""
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *args]
    return subprocess.run(cmd, capture_output=True, text=True)


def make_bad_db(path):
    """创建验收用 bad.sqlite：checks 只有 id 一列，并预置 id=7 的行。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE checks (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO checks (id) VALUES (7)")
        conn.commit()
    finally:
        conn.close()


def table_info(path):
    """返回 (列名按原顺序列表, 建表 SQL)。"""
    conn = sqlite3.connect(str(path))
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(checks)")]
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='checks'"
        ).fetchone()[0]
    finally:
        conn.close()
    return cols, sql


def read_ids(path):
    conn = sqlite3.connect(str(path))
    try:
        return [r[0] for r in conn.execute("SELECT id FROM checks ORDER BY id")]
    finally:
        conn.close()


def read_checks_rows(path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(
            "SELECT id, url, checked_at, elapsed_ms, status, "
            "http_status, reason FROM checks ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


class _CountingHandler(http.server.BaseHTTPRequestHandler):
    """对任意 GET 返回 200，并由服务端累计完整请求次数。"""

    def do_GET(self):
        self.server.request_count += 1
        self.server.paths.append(self.path)
        body = b"ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # 安静：不污染测试输出


class _CountingHTTPServer(http.server.HTTPServer):
    allow_reuse_address = True


def start_server():
    server = _CountingHTTPServer(("127.0.0.1", 0), _CountingHandler)
    server.request_count = 0
    server.paths = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class MissingColumnsBeforeProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-badcols-test-"))
        self.server = None
        self.thread = None

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                os.chmod(os.path.join(root, name), stat.S_IRWXU)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start(self):
        self.server, self.thread = start_server()
        port = self.server.server_address[1]
        return port, f"http://127.0.0.1:{port}{TARGET}"

    # ---- 对照场景一：缺字段旧库必须在探测前被拒绝 ----

    def test_bad_db_rejected_before_probe(self):
        db = self.tmp / "bad.sqlite"
        make_bad_db(db)
        cols_before, sql_before = table_info(db)
        self.assertEqual(cols_before, ["id"])
        self.assertEqual(read_ids(db), [7])

        port, url = self._start()
        proc = run_cli(db, "check", "--url", url)

        # 退出码 2、stdout 为空、stderr 说明缺失字段且无回溯
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(proc.stdout, "")
        self.assertIn("healthcheck: error:", proc.stderr)
        self.assertIn("checks 表缺少字段", proc.stderr)
        for name in ("url", "checked_at", "elapsed_ms",
                     "status", "http_status", "reason"):
            self.assertIn(name, proc.stderr)
        self.assertNotIn("id", proc.stderr.split("缺少字段", 1)[1])
        self.assertNotIn("Traceback", proc.stderr)

        # 不发送任何网络请求
        self.assertEqual(
            self.server.request_count, 0,
            "结构错误必须在探测前拒绝，服务端不应收到请求",
        )

        # 不补列、不重建表：列定义与原有行保持原样
        cols_after, sql_after = table_info(db)
        self.assertEqual(cols_after, ["id"])
        self.assertEqual(sql_after, sql_before)
        self.assertEqual(read_ids(db), [7])

    def test_bad_db_invalid_url_still_reported_first(self):
        """非法 URL 的错误优先级不变：先报 URL 错误，不查库缺字段。"""
        db = self.tmp / "bad.sqlite"
        make_bad_db(db)
        proc = run_cli(db, "check", "--url", "http://example.com/")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(proc.stdout, "")
        self.assertNotIn("缺少字段", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_bad_db_invalid_timeout_still_reported_first(self):
        """非法 timeout 的错误优先级不变：argparse 阶段即拒绝。"""
        db = self.tmp / "bad.sqlite"
        make_bad_db(db)
        _port, url = self._start()
        proc = run_cli(db, "check", "--url", url, "--timeout", "0")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(proc.stdout, "")
        self.assertNotIn("缺少字段", proc.stderr)
        # 非法 timeout 时同样不应发请求
        self.assertEqual(self.server.request_count, 0)

    # ---- 合法结构差异不应触发拒绝 ----

    def test_reordered_extra_and_mixed_case_columns_accepted(self):
        """列顺序不同、有额外列、列名大小写不同均不拒绝，检查正常落库。"""
        db = self.tmp / "weird.sqlite"
        conn = sqlite3.connect(str(db))
        try:
            # 七个必备列以不同顺序、混合大小写给出，另加一个额外列
            conn.execute(
                "CREATE TABLE checks ("
                "reason TEXT NOT NULL, "
                "HTTP_STATUS INTEGER, "
                "Status TEXT NOT NULL, "
                "Elapsed_Ms INTEGER NOT NULL, "
                "Checked_At TEXT NOT NULL, "
                "URL TEXT NOT NULL, "
                "note TEXT, "
                "id INTEGER PRIMARY KEY AUTOINCREMENT)"
            )
            conn.execute(
                "INSERT INTO checks (id, URL, Checked_At, Elapsed_Ms, Status, "
                "HTTP_STATUS, reason, note) VALUES (1, ?, ?, 5, 'success', "
                "200, 'ok', 'old')",
                ("http://127.0.0.1:1/old", "2026-10-01T00:00:00+00:00"),
            )
            conn.commit()
        finally:
            conn.close()

        port, url = self._start()
        proc = run_cli(db, "check", "--url", url)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(self.server.request_count, 1)

        record = json.loads(proc.stdout)
        self.assertEqual(record["id"], 2)
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["reason"], "ok")
        self.assertEqual(record["http_status"], 200)

        # 旧行与额外列数据保持不变，新行已插入
        conn = sqlite3.connect(str(db))
        try:
            self.assertEqual(
                conn.execute("SELECT note FROM checks WHERE id=1").fetchall(),
                [("old",)],
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM checks").fetchone()[0], 2
            )
        finally:
            conn.close()

    # ---- 对照场景二：文件不存在时自动建库建表、一次 GET、成功落库 ----

    def test_nonexistent_db_created_with_single_get_and_persisted_record(self):
        db = self.tmp / "monitor.sqlite"
        # 父目录已存在，文件尚不存在
        self.assertTrue(self.tmp.is_dir())
        self.assertFalse(db.exists())

        port, url = self._start()
        proc = run_cli(db, "check", "--url", url)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")

        # stdout 恰为单行 JSON
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(sorted(record.keys()), sorted(RECORD_FIELDS))
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["http_status"], 200)
        self.assertEqual(record["reason"], "ok")
        self.assertEqual(record["url"], url)

        # 只发送一次 GET，路径原样到达
        self.assertEqual(self.server.request_count, 1)
        self.assertEqual(self.server.paths, [TARGET])

        # 自动建库建表，恰好新增一条与 stdout 一致的记录
        self.assertTrue(db.exists())
        cols, _sql = table_info(db)
        self.assertEqual({c.lower() for c in cols}, set(RECORD_FIELDS))
        rows = read_checks_rows(db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(dict(zip(RECORD_FIELDS, rows[0])), record)

        # recent 只读往返一致，且不新增请求
        proc_recent = run_cli(db, "recent")
        self.assertEqual(proc_recent.returncode, 0, proc_recent.stderr)
        self.assertEqual(json.loads(proc_recent.stdout), [record])
        self.assertEqual(self.server.request_count, 1)

    # ---- 有效旧库继续正常使用 ----

    def test_valid_legacy_db_still_works(self):
        db = self.tmp / "legacy.sqlite"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "CREATE TABLE checks ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "url TEXT NOT NULL, checked_at TEXT NOT NULL, "
                "elapsed_ms INTEGER NOT NULL, status TEXT NOT NULL, "
                "http_status INTEGER, reason TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO checks (id, url, checked_at, elapsed_ms, status,"
                " http_status, reason) VALUES "
                "(7, 'http://127.0.0.1:1/old', '2026-10-01T00:00:00+00:00',"
                " 3, 'success', 200, 'ok')"
            )
            conn.commit()
        finally:
            conn.close()

        port, url = self._start()
        proc = run_cli(db, "check", "--url", url)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        record = json.loads(proc.stdout)
        self.assertEqual(record["id"], 8)  # 接续旧库的 id 序列
        self.assertEqual(read_ids(db), [7, 8])
        self.assertEqual(self.server.request_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
