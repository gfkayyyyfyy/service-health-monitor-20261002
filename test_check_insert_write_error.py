#!/usr/bin/env python3
"""healthcheck.py check 探测成功但写入被数据库拒绝的边界回归测试。

保护的边界：单次 HTTP 探测已正常结束，新增检查记录在插入阶段被
SQLite 拒绝时，check 必须以退出码 2 结束、stdout 完全为空、
stderr 说明写入失败且无 Python 回溯，数据库与服务端均不受污染。

失败样本的构造：临时库中有完整的 checks 表与一条 id=7 的合法记录，
另挂一个 BEFORE INSERT ... RAISE(ABORT) 触发器——数据库可以正常
打开、初始化（CREATE TABLE IF NOT EXISTS）与读取，唯独新增检查
记录时确定产生真实 SQLite 写入错误；不依赖文件权限或锁竞争，
checks 表结构本身保持不变。

场景（每个场景独立临时库 + 独立本机 HTTP 服务，目标路径
/health?case=write，服务只收到一次该路径的 GET）：
  * 服务返回 200 / 503，插入被拒绝：退出码 2，stdout 为空，
    stderr 含「写入检查记录失败」且无回溯；库中仍只有 id=7 的记录，
    表结构不变；随后 recent 以退出码 0 返回该旧记录，stderr 为空，
    不增加请求或记录。
  * 对照（允许插入）：200 → 退出码 0、success/ok；503 → 退出码 1、
    failure/http_status；stdout 恰为一行七字段 JSON，stderr 为空，
    新记录 id=8，输出与落库值逐字段相同，URL 保留路径与查询参数，
    http_status 与服务响应一致。checked_at 只核对有效 UTC 时间，
    elapsed_ms 只核对非负整数。

运行：python3 -m unittest test_check_insert_write_error
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
import unittest
from datetime import datetime, timedelta

SCRIPT = pathlib.Path(__file__).resolve().parent / "healthcheck.py"

RECORD_FIELDS = ("id", "url", "checked_at", "elapsed_ms",
                 "status", "http_status", "reason")

TARGET = "/health?case=write"

# 与 healthcheck.py 的 CREATE_TABLE_SQL 一致的完整 checks 表结构
CREATE_TABLE_SQL = """
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

# 让新增检查记录确定失败的触发器：只在 INSERT 时中止，
# 不影响打开、初始化（CREATE TABLE IF NOT EXISTS）与读取
REJECT_INSERT_TRIGGER_SQL = """
CREATE TRIGGER reject_checks_insert BEFORE INSERT ON checks
BEGIN
    SELECT RAISE(ABORT, 'insert rejected by test trigger');
END
"""

# 每个场景预置的 id=7 合法成功记录，全部字段使用固定值
SEED_RECORD = {
    "id": 7,
    "url": "http://127.0.0.1:1/health?case=write",
    "checked_at": "2026-10-01T00:00:00+00:00",
    "elapsed_ms": 3,
    "status": "success",
    "http_status": 200,
    "reason": "ok",
}


def run_cli(db_path, *args):
    """以子进程运行 healthcheck.py（等待上限 30 秒），返回 CompletedProcess。"""
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def make_db(path, reject_insert):
    """创建含完整 checks 表与 id=7 固定记录的临时库；可按需挂拒绝插入的触发器。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(CREATE_TABLE_SQL)
        conn.execute(
            "INSERT INTO checks (id, url, checked_at, elapsed_ms, status,"
            " http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            tuple(SEED_RECORD[name] for name in RECORD_FIELDS),
        )
        if reject_insert:
            conn.execute(REJECT_INSERT_TRIGGER_SQL)
        conn.commit()
    finally:
        conn.close()


def read_records(path):
    """按 id 升序读出全部记录，映射为七字段字典列表。"""
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute(
            "SELECT id, url, checked_at, elapsed_ms, status,"
            " http_status, reason FROM checks ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(zip(RECORD_FIELDS, row)) for row in rows]


def schema_snapshot(path):
    """库中全部模式对象（表、索引、触发器等）的快照，用于核对结构不变。"""
    conn = sqlite3.connect(str(path))
    try:
        objects = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master"
            " ORDER BY type, name"
        ).fetchall()
        cols = conn.execute("PRAGMA table_info(checks)").fetchall()
    finally:
        conn.close()
    return objects, cols


def make_handler(status):
    """构造对任意 GET 返回固定状态码完整响应、并记录请求路径的处理器。"""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.request_count += 1
            self.server.paths.append(self.path)
            body = b"ok\n" if status == 200 else b"service unavailable\n"
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # 安静：不污染测试输出

    return _Handler


class _HTTPServer(http.server.HTTPServer):
    allow_reuse_address = True


def start_server(status):
    server = _HTTPServer(("127.0.0.1", 0), make_handler(status))
    server.request_count = 0
    server.paths = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class InsertWriteErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-write-err-test-"))
        self.server = None
        self.thread = None

    def tearDown(self):
        try:
            if self.server is not None:
                self.server.shutdown()
                self.server.server_close()
                self.thread.join(timeout=5)
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _start_server(self, status):
        self.server, self.thread = start_server(status)
        port = self.server.server_address[1]
        return f"http://127.0.0.1:{port}{TARGET}"

    def _assert_valid_utc(self, value):
        """只核对 checked_at 是有效 UTC 时间，不断言具体时刻。"""
        parsed = datetime.fromisoformat(value)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset(), timedelta(0))

    def _assert_seed_db_intact(self, db, snapshot_before):
        """核对库中仍只有 id=7 的固定记录，且全部模式对象保持原样。"""
        self.assertEqual(read_records(db), [SEED_RECORD])
        self.assertEqual(schema_snapshot(db), snapshot_before)

    def _run_rejected_case(self, http_status):
        db = self.tmp / f"reject-{http_status}.sqlite"
        make_db(db, reject_insert=True)
        snapshot_before = schema_snapshot(db)
        # 测试前逐字段核对预置记录
        self.assertEqual(read_records(db), [SEED_RECORD])

        url = self._start_server(http_status)
        proc = run_cli(db, "check", "--url", url)

        # 探测已结束但写入被拒：退出码 2，stdout 完全为空，
        # stderr 说明写入失败且无 Python 回溯
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(proc.stdout, "")
        self.assertIn("healthcheck: error:", proc.stderr)
        self.assertIn("写入检查记录失败", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

        # 服务只收到一次对应路径的 GET
        self.assertEqual(self.server.request_count, 1)
        self.assertEqual(self.server.paths, [TARGET])

        # 数据库仍只有原来的记录，表结构（含全部模式对象）保持不变
        self._assert_seed_db_intact(db, snapshot_before)

        # 随后 recent 以退出码 0 返回该旧记录，stderr 为空，
        # 查询不增加请求或记录
        proc_recent = run_cli(db, "recent")
        self.assertEqual(proc_recent.returncode, 0, proc_recent.stderr)
        self.assertEqual(proc_recent.stderr, "")
        self.assertEqual(json.loads(proc_recent.stdout), [SEED_RECORD])
        self.assertEqual(self.server.request_count, 1)
        self._assert_seed_db_intact(db, snapshot_before)

    def _run_allowed_case(self, http_status, expected_code,
                          expected_status, expected_reason):
        db = self.tmp / f"allow-{http_status}.sqlite"
        make_db(db, reject_insert=False)
        # 测试前逐字段核对预置记录
        self.assertEqual(read_records(db), [SEED_RECORD])

        url = self._start_server(http_status)
        proc = run_cli(db, "check", "--url", url)

        self.assertEqual(proc.returncode, expected_code, proc.stderr)
        self.assertEqual(proc.stderr, "")

        # stdout 恰好是一行既有七字段 JSON
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(sorted(record.keys()), sorted(RECORD_FIELDS))

        # 状态、原因与服务响应的状态码
        self.assertEqual(record["status"], expected_status)
        self.assertEqual(record["reason"], expected_reason)
        self.assertEqual(record["http_status"], http_status)

        # 新记录 id 为 8；原始 URL 保留路径和查询参数
        self.assertEqual(record["id"], 8)
        self.assertEqual(record["url"], url)
        self.assertIn(TARGET, record["url"])

        # checked_at 只核对有效 UTC 时间，elapsed_ms 只核对非负整数
        self._assert_valid_utc(record["checked_at"])
        self.assertIs(type(record["elapsed_ms"]), int)
        self.assertGreaterEqual(record["elapsed_ms"], 0)

        # 服务只收到一次对应路径的 GET
        self.assertEqual(self.server.request_count, 1)
        self.assertEqual(self.server.paths, [TARGET])

        # 输出内容与数据库保存值逐字段相同；id=7 的记录保持原样
        records = read_records(db)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0], SEED_RECORD)
        self.assertEqual(records[1], record)

    # ---- 插入被拒绝：200 与 503 两种响应都以退出码 2 结束 ----

    def test_insert_rejected_http_200(self):
        self._run_rejected_case(200)

    def test_insert_rejected_http_503(self):
        self._run_rejected_case(503)

    # ---- 对照：允许插入时 200 / 503 各自正常落库 ----

    def test_insert_allowed_http_200(self):
        self._run_allowed_case(200, 0, "success", "ok")

    def test_insert_allowed_http_503(self):
        self._run_allowed_case(503, 1, "failure", "http_status")


if __name__ == "__main__":
    unittest.main(verbosity=2)
