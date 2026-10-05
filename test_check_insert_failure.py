#!/usr/bin/env python3
"""healthcheck.py check 探测已结束、但新增记录被数据库拒绝的边界回归测试。

保护的边界：单次 HTTP 探测已拿到完整响应，写入阶段 INSERT 被数据库
拒绝时，check 必须以退出码 2 结束、stdout 完全为空、stderr 说明
「写入检查记录失败」且无 Python 回溯；数据库保持只有原有记录，
表结构不变；随后 recent 只读查询仍返回旧记录。

失败样本的构造：checks 表结构完整（与 healthcheck.py 的建表语句一致），
预置一条 id=7 的合法成功记录，另挂一个 BEFORE INSERT 触发器
（RAISE(ABORT)）。数据库的打开、初始化（CREATE TABLE IF NOT EXISTS）
与读取全部正常，唯独新增检查记录时确定地产生真实 SQLite 写入错误
（sqlite3.IntegrityError），不依赖文件权限或偶然的锁竞争。

四个场景（每个场景使用独立的临时数据库与本机服务）：
  * 服务返回完整 200，插入被拒 → 退出码 2，stdout 为空，
    stderr 含写入失败说明且无回溯；服务恰好收到一次
    GET /health?case=write；库中仍只有 id=7 的记录，表结构不变；
    recent 退出码 0 返回该旧记录，stderr 为空，不增加请求或记录；
  * 服务返回完整 503，插入被拒 → 同上；
  * 对照：服务返回 200，插入允许 → 退出码 0，status=success、
    reason=ok、http_status=200，stdout 恰为一行七字段 JSON，
    新记录 id=8，输出与落库值逐字段相同；
  * 对照：服务返回 503，插入允许 → 退出码 1，status=failure、
    reason=http_status、http_status=503，其余同 200 对照。

checked_at 只核对是带 UTC 时区的有效时间，elapsed_ms 只核对是非负
整数，不断言具体时刻或毫秒数。仅依赖 Python 标准库与绑定 127.0.0.1
随机端口的本机服务；每次执行自行准备样本，子进程等待设有上限，
异常时也释放服务、进程与临时数据，重复执行互不影响。

运行：python3 -m unittest test_check_insert_failure
"""

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
from datetime import datetime, timedelta

SCRIPT = pathlib.Path(__file__).resolve().parent / "healthcheck.py"

RECORD_FIELDS = ("id", "url", "checked_at", "elapsed_ms",
                 "status", "http_status", "reason")

# 探测目标：路径与查询参数原样出现在登记的 URL 与服务的请求记录中
TARGET = "/health?case=write"

# 子进程等待上限（秒）：本机探测默认超时 1 秒，15 秒足够且能挡住挂死
CLI_TIMEOUT = 15

# 与 healthcheck.py 中 CREATE_TABLE_SQL 相同的完整 checks 表结构
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS checks (
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

# 预置的 id=7 合法成功记录：除 id 外各字段均为固定值，
# 测试前后逐字段核对，确认失败场景不改动既有数据
SEED_ROW = (
    7,
    "http://127.0.0.1:9/seed?fixed=1",
    "2026-10-01T00:00:00+00:00",
    3,
    "success",
    200,
    "ok",
)
SEED_RECORD = dict(zip(RECORD_FIELDS, SEED_ROW))

# 让新增检查记录确定失败的触发器：打开、初始化、读取均不受影响，
# 只有 INSERT INTO checks 被 RAISE(ABORT) 以真实 SQLite 错误拒绝
REJECT_INSERT_SQL = """
CREATE TRIGGER reject_new_check
BEFORE INSERT ON checks
BEGIN
    SELECT RAISE(ABORT, 'seeded insert rejection');
END
"""


def run_cli(db_path, *args):
    """以子进程运行 healthcheck.py（等待设有上限），返回 CompletedProcess。"""
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *args]
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=CLI_TIMEOUT
    )


def make_seeded_db(path, reject_inserts):
    """创建场景数据库：完整 checks 表 + id=7 的固定成功记录。

    reject_inserts 为真时额外挂上 BEFORE INSERT 触发器，使新增检查
    记录确定地被真实 SQLite 写入错误拒绝。
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(CREATE_TABLE_SQL)
        conn.execute(
            "INSERT INTO checks (id, url, checked_at, elapsed_ms, status,"
            " http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            SEED_ROW,
        )
        if reject_inserts:
            conn.execute(REJECT_INSERT_SQL)
        conn.commit()
    finally:
        conn.close()


def read_checks_rows(db_path):
    """按 id 升序读取 checks 表全部行，用于逐字段核对落库内容。"""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT id, url, checked_at, elapsed_ms, status, "
            "http_status, reason FROM checks ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def read_schema(db_path):
    """用户定义对象（checks 表及触发器）的 sqlite_master 快照。"""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()


class _WriteCaseHandler(http.server.BaseHTTPRequestHandler):
    """对 GET 返回本服务预设的状态码（200 或 503），并记录请求目标。"""

    def do_GET(self):
        self.server.requests.append(self.path)
        code = self.server.reply_code
        body = b"ok\n" if code == 200 else b"unavailable\n"
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # 安静：不污染测试输出


def start_server(reply_code):
    """启动绑定 127.0.0.1 随机端口的服务，固定以 reply_code 响应。"""
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _WriteCaseHandler
    )
    server.daemon_threads = True
    server.reply_code = reply_code
    server.requests = []  # 已收到的请求目标列表，用于核对次数与路径
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class InsertFailureBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-insert-test-"))
        self._servers = []

    def tearDown(self):
        # 即使用例断言失败也必须释放本机服务与临时数据
        for server, thread in self._servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start(self, reply_code):
        server, thread = start_server(reply_code)
        self._servers.append((server, thread))
        port = server.server_address[1]
        return server, f"http://127.0.0.1:{port}{TARGET}"

    # ---- 公共核对 ----

    def assert_seed_row(self, row):
        """id=7 的预置记录逐字段等于固定值。"""
        self.assertEqual(
            len(row), len(RECORD_FIELDS), "[种子] 记录字段数必须一致"
        )
        for field, actual, expected in zip(RECORD_FIELDS, row, SEED_ROW):
            self.assertEqual(
                actual, expected,
                f"[种子] 字段 {field} 必须为固定值 {expected!r}，"
                f"实际 {actual!r}",
            )

    def assert_seed_only_and_schema_unchanged(self, db, schema_before):
        """库中仍只有 id=7 的固定记录（逐字段核对），表结构保持不变。"""
        rows = read_checks_rows(db)
        self.assertEqual(len(rows), 1, "[持久化] 插入被拒后不得新增记录")
        self.assert_seed_row(rows[0])
        self.assertEqual(
            read_schema(db), schema_before,
            "[持久化] 表结构与触发器定义必须保持不变",
        )

    # ---- 失败场景：探测完成，但 INSERT 被触发器拒绝 ----

    def _run_insert_rejected(self, code):
        db = self.tmp / f"reject-{code}.sqlite"
        make_seeded_db(db, reject_inserts=True)
        # 前置核对：数据库可打开、可读取，恰有一条逐字段固定的 id=7 记录
        schema_before = read_schema(db)
        rows_before = read_checks_rows(db)
        self.assertEqual(len(rows_before), 1)
        self.assert_seed_row(rows_before[0])

        server, url = self._start(code)
        proc = run_cli(db, "check", "--url", url)

        # 退出码 2，stdout 完全为空
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(proc.stdout, "", "[输出] 写入被拒时 stdout 必须为空")
        # stderr 说明写入检查记录失败，且没有 Python 回溯
        self.assertIn("healthcheck: error:", proc.stderr)
        self.assertIn(
            "写入检查记录失败", proc.stderr,
            "[输出] stderr 必须说明写入检查记录失败",
        )
        self.assertNotIn(
            "Traceback", proc.stderr, "[输出] stderr 不得包含 Python 回溯"
        )

        # 服务恰好收到一次对应路径的 GET，路径与查询参数原样到达
        self.assertEqual(
            server.requests, [TARGET],
            "[探测] 服务必须恰好收到一次 GET /health?case=write",
        )

        # 数据库仍只有原来的记录，表结构保持不变
        self.assert_seed_only_and_schema_unchanged(db, schema_before)

        # recent 只读查询：退出码 0 返回该旧记录，stderr 为空，
        # 不增加请求或记录
        proc_recent = run_cli(db, "recent")
        self.assertEqual(proc_recent.returncode, 0, proc_recent.stderr)
        self.assertEqual(proc_recent.stderr, "")
        self.assertEqual(
            json.loads(proc_recent.stdout), [SEED_RECORD],
            "[持久化] recent 必须返回原来的 id=7 记录",
        )
        self.assertEqual(
            server.requests, [TARGET],
            "[持久化] recent 不得发起任何网络请求",
        )
        self.assert_seed_only_and_schema_unchanged(db, schema_before)

    def test_insert_rejected_after_200_response(self):
        self._run_insert_rejected(200)

    def test_insert_rejected_after_503_response(self):
        self._run_insert_rejected(503)

    # ---- 对照场景：同样的响应，插入被允许时行为不变 ----

    def _run_insert_allowed(self, code, expected_rc,
                            expected_status, expected_reason):
        db = self.tmp / f"allow-{code}.sqlite"
        make_seeded_db(db, reject_inserts=False)
        schema_before = read_schema(db)
        rows_before = read_checks_rows(db)
        self.assertEqual(len(rows_before), 1)
        self.assert_seed_row(rows_before[0])

        server, url = self._start(code)
        proc = run_cli(db, "check", "--url", url)

        # 退出码与 stderr
        self.assertEqual(proc.returncode, expected_rc, proc.stderr)
        self.assertEqual(proc.stderr, "", "[输出] stderr 必须为空")

        # stdout 恰好是一行既有七字段 JSON
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1, "[输出] stdout 必须恰有一行 JSON")
        record = json.loads(lines[0])
        self.assertEqual(
            sorted(record.keys()), sorted(RECORD_FIELDS),
            "[输出] JSON 字段集合必须与既有七字段一致",
        )

        # 接续 id=7 的序列，新记录 id 为 8；原始 URL 保留路径和查询参数
        self.assertEqual(record["id"], 8, "[持久化] 新记录 id 必须为 8")
        self.assertEqual(
            record["url"], url,
            "[持久化] 必须保留含路径与查询参数的原始 URL",
        )
        self.assertIn(TARGET, record["url"])

        # 状态归类与 http_status 和服务响应一致
        self.assertEqual(record["status"], expected_status)
        self.assertEqual(record["reason"], expected_reason)
        self.assertEqual(
            record["http_status"], code,
            "[输出] http_status 必须与服务响应一致",
        )

        # checked_at 只核对是有效 UTC 时间，elapsed_ms 只核对非负整数
        checked_at = datetime.fromisoformat(record["checked_at"])
        self.assertIsNotNone(
            checked_at.tzinfo, "[持久化] checked_at 必须带时区"
        )
        self.assertEqual(
            checked_at.utcoffset(), timedelta(0),
            "[持久化] checked_at 必须是 UTC 时间",
        )
        self.assertIsInstance(record["elapsed_ms"], int)
        self.assertNotIsInstance(record["elapsed_ms"], bool)
        self.assertGreaterEqual(record["elapsed_ms"], 0)

        # 服务恰好收到一次对应路径的 GET
        self.assertEqual(server.requests, [TARGET])

        # 恰好新增一条记录：旧记录逐字段不变，新记录与输出逐字段相同
        rows = read_checks_rows(db)
        self.assertEqual(len(rows), 2, "[持久化] 必须恰好新增一条记录")
        self.assert_seed_row(rows[0])
        self.assertEqual(
            dict(zip(RECORD_FIELDS, rows[1])), record,
            "[持久化] 落库值必须与 stdout 逐字段相同",
        )
        self.assertEqual(
            read_schema(db), schema_before, "[持久化] 表结构必须保持不变"
        )

        # recent 只读往返：退出码 0、stderr 为空，不增加请求或记录
        proc_recent = run_cli(db, "recent")
        self.assertEqual(proc_recent.returncode, 0, proc_recent.stderr)
        self.assertEqual(proc_recent.stderr, "")
        self.assertEqual(
            json.loads(proc_recent.stdout), [record, SEED_RECORD],
            "[持久化] recent 必须按 id 倒序返回新记录与旧记录",
        )
        self.assertEqual(server.requests, [TARGET])
        self.assertEqual(read_checks_rows(db), rows)

    def test_insert_allowed_after_200_response(self):
        self._run_insert_allowed(200, 0, "success", "ok")

    def test_insert_allowed_after_503_response(self):
        self._run_insert_allowed(503, 1, "failure", "http_status")


if __name__ == "__main__":
    unittest.main(verbosity=2)
