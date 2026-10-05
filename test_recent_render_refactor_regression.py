#!/usr/bin/env python3
"""recent 结果呈现流程重构的回归验证。

重构把 recent 三种模式（记录数组 / --summary / --status-summary）的结果呈现
合并为单一入口 render_recent_result：缺库（含父目录不存在）不再单独维护
输出分支，而是与无历史表、空表、筛选无匹配一样以空记录集走同一呈现流程；
数据库访问与查询则抽为 query_recent_rows。本测试通过公开命令行入口
（子进程运行 healthcheck.py）核对重构后的行为与原语义完全等价：

- 验收主用例：目标 http://127.0.0.1:8765/ 的两条记录（checked_at 均为
  2026-10-05T00:00:00Z；id=1 耗时 0 毫秒/success/200/ok，id=2 耗时
  7 毫秒/failure/null/timeout）：
  · recent --limit 2 → 记录顺序 2、1，七字段、紧凑单行 JSON + 一个换行；
  · --summary → count 2、min 0、max 7、avg 3.5（零耗时计入、平均值不取整）；
  · --status-summary → count 2、success 1、failure 1（只按保存的 status）；
  三种模式均退出 0、stderr 为空。
- 空结果等价：缺库、父目录缺失、无历史表、空表、筛选无匹配五种情形，
  三种模式的输出逐字节相同（[] / 三耗时字段为 null 的摘要 / 三计数为 0
  的状态摘要），退出 0、stderr 为空。
- 错误边界保持现状：两摘要互斥先于其他参数校验与数据库访问（退出 2、
  stdout 为空、stderr 说明互斥）；非法 status/URL/reason/since/until、
  目录路径、无效 SQLite 文件、缺字段、时间筛选遇非法 checked_at 均
  退出 2 且 stdout 为空。
- 只读语义：查询不发网络请求，不创建文件/目录/表，不修改记录；兼容
  大小写异写的历史表名（CHECKS）与可读不可写的数据库。
- check 子命令不受影响：单次探测、持久化与退出码保持原样。

在项目目录执行：
    python3 -m unittest test_recent_render_refactor_regression
或：
    python3 test_recent_render_refactor_regression.py
"""

import http.server
import json
import os
import pathlib
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "healthcheck.py"

# 与 healthcheck.py 中 checks 表一致的结构（兼容性基准）
SCHEMA_SQL = """
CREATE TABLE {table} (
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

TARGET_URL = "http://127.0.0.1:8765/"
CHECKED_AT = "2026-10-05T00:00:00Z"

# 验收固定样本：id=1 耗时 0 毫秒/success/200/ok；id=2 耗时 7 毫秒/failure/null/timeout
REC1 = (TARGET_URL, CHECKED_AT, 0, "success", 200, "ok")
REC2 = (TARGET_URL, CHECKED_AT, 7, "failure", None, "timeout")

EXPECTED_RECORDS_JSON = (
    '[{"id":2,"url":"http://127.0.0.1:8765/",'
    '"checked_at":"2026-10-05T00:00:00Z","elapsed_ms":7,'
    '"status":"failure","http_status":null,"reason":"timeout"},'
    '{"id":1,"url":"http://127.0.0.1:8765/",'
    '"checked_at":"2026-10-05T00:00:00Z","elapsed_ms":0,'
    '"status":"success","http_status":200,"reason":"ok"}]'
)
EXPECTED_SUMMARY_JSON = (
    '{"count":2,"min_elapsed_ms":0,'
    '"max_elapsed_ms":7,"avg_elapsed_ms":3.5}'
)
EXPECTED_STATUS_SUMMARY_JSON = (
    '{"count":2,"success_count":1,"failure_count":1}'
)

EMPTY_RECORDS_JSON = "[]"
EMPTY_SUMMARY_JSON = (
    '{"count":0,"min_elapsed_ms":null,'
    '"max_elapsed_ms":null,"avg_elapsed_ms":null}'
)
EMPTY_STATUS_SUMMARY_JSON = '{"count":0,"success_count":0,"failure_count":0}'


def run_cli(*argv):
    """运行 healthcheck.py，返回 (退出码, stdout, stderr)。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def make_db(db_path, rows=(), table="checks", schema_sql=None):
    """创建测试数据库并插入给定记录（按给定顺序，id 自增）。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            schema_sql if schema_sql is not None
            else SCHEMA_SQL.format(table=table)
        )
        for row in rows:
            conn.execute(
                "INSERT INTO {} (url, checked_at, elapsed_ms, status,"
                " http_status, reason) VALUES (?, ?, ?, ?, ?, ?)".format(table),
                row,
            )
        conn.commit()
    finally:
        conn.close()


def snapshot_tree(root):
    """目录下全部相对路径及文件内容快照，用于核对查询不产生任何写副作用。"""
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames + filenames):
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            if os.path.isdir(path):
                snap[rel] = "<DIR>"
            else:
                with open(path, "rb") as fh:
                    snap[rel] = fh.read()
    return snap


class TempDirMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hc_render_regression_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "monitor.sqlite")


class AcceptanceCaseTest(TempDirMixin):
    """验收主用例：两条记录上三种模式的完整输出约定。"""

    def setUp(self):
        super().setUp()
        make_db(self.db, [REC1, REC2])

    def test_records_mode_order_and_fields(self):
        code, out, err = run_cli(
            "--db", self.db, "recent", "--url", TARGET_URL, "--limit", "2"
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        # 紧凑单行 JSON + 恰好一个换行；记录顺序为 id 倒序 2、1
        self.assertEqual(out, EXPECTED_RECORDS_JSON + "\n")
        records = json.loads(out)
        self.assertEqual([r["id"] for r in records], [2, 1])
        self.assertEqual(
            list(records[0].keys()),
            ["id", "url", "checked_at", "elapsed_ms",
             "status", "http_status", "reason"],
        )

    def test_summary_counts_failures_and_zero_elapsed(self):
        code, out, err = run_cli(
            "--db", self.db, "recent", "--url", TARGET_URL,
            "--limit", "2", "--summary",
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        # 失败与零耗时记录均计入；平均值不取整（3.5 而非 3 或 4）
        self.assertEqual(out, EXPECTED_SUMMARY_JSON + "\n")

    def test_status_summary_uses_stored_status_only(self):
        code, out, err = run_cli(
            "--db", self.db, "recent", "--url", TARGET_URL,
            "--limit", "2", "--status-summary",
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out, EXPECTED_STATUS_SUMMARY_JSON + "\n")

    def test_limit_one_takes_highest_id_first(self):
        code, out, err = run_cli("--db", self.db, "recent", "--limit", "1")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual([r["id"] for r in json.loads(out)], [2])

    def test_query_has_no_side_effects(self):
        before = snapshot_tree(self.tmp)
        for extra in ([], ["--summary"], ["--status-summary"]):
            code, _out, err = run_cli(
                "--db", self.db, "recent", "--limit", "2", *extra
            )
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
        self.assertEqual(snapshot_tree(self.tmp), before)


class EmptyResultEquivalenceTest(TempDirMixin):
    """五种空历史情形在三种模式下输出逐字节一致（统一呈现入口的核心）。"""

    MODE_ARGS = {
        "records": ([], EMPTY_RECORDS_JSON),
        "summary": (["--summary"], EMPTY_SUMMARY_JSON),
        "status_summary": (["--status-summary"], EMPTY_STATUS_SUMMARY_JSON),
    }

    def empty_scenarios(self):
        """返回 {情形名: db 路径}，覆盖全部空历史来源。"""
        scenarios = {}
        # 1. 数据库文件不存在
        scenarios["missing_db"] = os.path.join(self.tmp, "absent.sqlite")
        # 2. 父目录也不存在
        scenarios["missing_parent"] = os.path.join(
            self.tmp, "no-such-dir", "monitor.sqlite"
        )
        # 3. 库存在但没有 checks 表（仅有其他表）
        no_table = os.path.join(self.tmp, "no_checks.sqlite")
        conn = sqlite3.connect(no_table)
        conn.execute("CREATE TABLE other (id INTEGER)")
        conn.commit()
        conn.close()
        scenarios["no_checks_table"] = no_table
        # 4. checks 表存在但为空
        empty_table = os.path.join(self.tmp, "empty.sqlite")
        make_db(empty_table)
        scenarios["empty_table"] = empty_table
        # 5. 表内有记录但筛选无匹配
        no_match = os.path.join(self.tmp, "no_match.sqlite")
        make_db(no_match, [REC1, REC2])
        scenarios["filter_no_match"] = no_match
        return scenarios

    def test_empty_outputs_identical_across_scenarios(self):
        scenarios = self.empty_scenarios()
        for mode, (extra, expected) in self.MODE_ARGS.items():
            outputs = []
            for name, db_path in scenarios.items():
                argv = ["--db", db_path, "recent", "--limit", "2", *extra]
                if name == "filter_no_match":
                    argv += ["--status", "success", "--reason", "timeout"]
                code, out, err = run_cli(*argv)
                self.assertEqual(code, 0, f"{mode}/{name}: exit {code}")
                self.assertEqual(err, "", f"{mode}/{name}: stderr {err!r}")
                self.assertEqual(
                    out, expected + "\n", f"{mode}/{name}: stdout {out!r}"
                )
                outputs.append(out)
            # 缺库分支与其余空结果分支共用同一呈现：输出逐字节相同
            self.assertEqual(
                len(set(outputs)), 1, f"{mode}: 各空情形输出不一致 {outputs!r}"
            )

    def test_missing_db_creates_nothing(self):
        db_path = os.path.join(self.tmp, "absent.sqlite")
        for extra in ([], ["--summary"], ["--status-summary"]):
            code, _out, _err = run_cli("--db", db_path, "recent", *extra)
            self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(db_path))
        self.assertEqual(os.listdir(self.tmp), [])


class ErrorContractTest(TempDirMixin):
    """错误顺序与说明保持现状：退出 2、stdout 为空。"""

    def assert_error(self, argv, needle):
        code, out, err = run_cli(*argv)
        self.assertEqual(code, 2, f"{argv}: exit {code}, stderr {err!r}")
        self.assertEqual(out, "", f"{argv}: stdout 应为空，实际 {out!r}")
        self.assertIn(needle, err, f"{argv}: stderr {err!r} 不含 {needle!r}")

    def test_summaries_mutually_exclusive_before_anything_else(self):
        # 互斥检查先于其他参数校验与数据库访问：
        # 同时给非法 --status 与不存在的库，仍只报互斥
        missing_db = os.path.join(self.tmp, "absent.sqlite")
        self.assert_error(
            ["--db", missing_db, "recent",
             "--summary", "--status-summary", "--status", "bogus"],
            "互斥",
        )
        self.assertFalse(os.path.exists(missing_db))

    def test_invalid_status_rejected_before_db_access(self):
        missing_db = os.path.join(self.tmp, "absent.sqlite")
        self.assert_error(
            ["--db", missing_db, "recent", "--status", "SUCCESS"],
            "status",
        )
        self.assertFalse(os.path.exists(missing_db))

    def test_invalid_url_rejected_before_db_access(self):
        missing_db = os.path.join(self.tmp, "absent.sqlite")
        self.assert_error(
            ["--db", missing_db, "recent", "--url", "http://example.com/"],
            "127.0.0.1",
        )
        self.assertFalse(os.path.exists(missing_db))

    def test_invalid_reason_rejected_before_db_access(self):
        missing_db = os.path.join(self.tmp, "absent.sqlite")
        self.assert_error(
            ["--db", missing_db, "recent", "--reason", "OK"],
            "reason",
        )
        self.assertFalse(os.path.exists(missing_db))

    def test_invalid_since_and_until_rejected(self):
        missing_db = os.path.join(self.tmp, "absent.sqlite")
        self.assert_error(
            ["--db", missing_db, "recent", "--since", "2026-10-05 00:00:00"],
            "since",
        )
        self.assert_error(
            ["--db", missing_db, "recent", "--until", "2026-13-01T00:00:00Z"],
            "until",
        )
        self.assertFalse(os.path.exists(missing_db))

    def test_since_after_until_rejected(self):
        self.assert_error(
            ["--db", self.db, "recent",
             "--since", "2026-10-05T00:00:02Z",
             "--until", "2026-10-05T00:00:01Z"],
            "晚于",
        )

    def test_directory_path_rejected(self):
        self.assert_error(
            ["--db", self.tmp, "recent"], "目录",
        )

    def test_invalid_sqlite_file_rejected(self):
        bad = os.path.join(self.tmp, "bad.sqlite")
        with open(bad, "wb") as fh:
            fh.write(b"this is not a sqlite database" * 4)
        self.assert_error(["--db", bad, "recent"], "读取数据库")

    def test_missing_columns_rejected(self):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE checks (id INTEGER PRIMARY KEY, url TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()
        self.assert_error(["--db", self.db, "recent"], "缺少字段")

    def test_invalid_checked_at_rejected_under_time_filter(self):
        make_db(self.db, [(TARGET_URL, "not-a-timestamp", 3,
                           "success", 200, "ok")])
        self.assert_error(
            ["--db", self.db, "recent",
             "--since", "2026-10-04T00:00:00Z"],
            "checked_at",
        )

    def test_invalid_limit_rejected_by_argparse(self):
        code, out, err = run_cli("--db", self.db, "recent", "--limit", "0")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("limit", err)


class CompatibilityTest(TempDirMixin):
    """只读与兼容性语义：异写表名、只读库、记录不被修改。"""

    def test_case_variant_table_name(self):
        make_db(self.db, [REC1, REC2], table="CHECKS")
        code, out, err = run_cli("--db", self.db, "recent", "--limit", "2")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out, EXPECTED_RECORDS_JSON + "\n")

    def test_readonly_database_file(self):
        make_db(self.db, [REC1, REC2])
        os.chmod(self.db, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        try:
            code, out, err = run_cli(
                "--db", self.db, "recent", "--limit", "2", "--summary"
            )
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertEqual(out, EXPECTED_SUMMARY_JSON + "\n")
        finally:
            os.chmod(self.db, stat.S_IRUSR | stat.S_IWUSR)

    def test_records_not_modified_by_query(self):
        make_db(self.db, [REC1, REC2])
        code, _out, _err = run_cli("--db", self.db, "recent", "--limit", "2")
        self.assertEqual(code, 0)
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(
                "SELECT url, checked_at, elapsed_ms, status, http_status,"
                " reason FROM checks ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [REC1, REC2])

    def test_time_window_filter_still_applies(self):
        rows = [
            (TARGET_URL, "2026-10-04T23:59:59Z", 1, "success", 200, "ok"),
            REC1,
            REC2,
            (TARGET_URL, "2026-10-05T00:00:01Z", 3, "success", 200, "ok"),
        ]
        make_db(self.db, rows)
        code, out, err = run_cli(
            "--db", self.db, "recent",
            "--since", "2026-10-05T00:00:00Z",
            "--until", "2026-10-05T00:00:00.000000+00:00",
            "--limit", "5",
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        # 窗口端点包含边界；Z 与 +00:00 视为同一时刻；输出保留原始字符串
        self.assertEqual([r["id"] for r in json.loads(out)], [3, 2])


class CheckCommandUnchangedTest(TempDirMixin):
    """check 子命令：单次探测、持久化与退出码保持原样。"""

    @classmethod
    def setUpClass(cls):
        cls.requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                cls.requests.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        cls.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_check_probes_once_and_persists(self):
        url = f"http://127.0.0.1:{self.port}/"
        code, out, err = run_cli("--db", self.db, "check", "--url", url)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        record = json.loads(out)
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["http_status"], 200)
        self.assertEqual(record["reason"], "ok")
        self.assertEqual(record["url"], url)
        # 恰好一次探测
        self.assertEqual(len(self.requests), 1)
        # recent 能读回同一条记录（表结构与落库字段未变）
        code, out, err = run_cli("--db", self.db, "recent", "--limit", "1")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], record["id"])
        self.assertEqual(rows[0]["url"], url)


if __name__ == "__main__":
    unittest.main()
