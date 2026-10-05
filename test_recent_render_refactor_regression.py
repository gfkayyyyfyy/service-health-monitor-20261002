#!/usr/bin/env python3
"""recent 结果呈现流程重构的回归验证。

重构把「缺库快路径」与「正常查询路径」各自维护的输出分支（含手工拼写的
空摘要常量 NULL_SUMMARY_JSON / NULL_STATUS_SUMMARY_JSON）合并为唯一呈现
入口 render_recent_result：三种形态（记录数组 / --summary 耗时摘要 /
--status-summary 状态摘要）都作用于筛选、时间窗口与 limit 处理后的同一批
记录，空结果统一以空记录集走同一入口。本测试通过公开命令行入口（子进程
运行 healthcheck.py）核对重构前后行为完全等价：

- 验收主用例：目标 http://127.0.0.1:8765/ 的两条记录（checked_at 均为
  2026-10-05T00:00:00Z；id 1 耗时 0/success/200/ok，id 2 耗时
  7/failure/null/timeout），recent --limit 2 记录顺序为 2、1，耗时摘要
  count 2、min 0、max 7、avg 3.5，状态摘要 count 2、成功 1、失败 1；
  三种输出均为紧凑单行 JSON 加一个换行，退出码 0、stderr 为空；
- 输出逐字节核对：字段名与顺序、原始 URL/时间字符串/中文原样、
  平均值不取整（3.5）、耗时摘要计入失败与零耗时、状态摘要只按保存的
  status 分类（不从 reason 或 http_status 推断）；
- 空结果等价：缺库（含父目录缺失）、无历史表、空表、筛选无匹配、
  时间窗口无保留，五种情形三种形态的输出逐字节一致
  （[] / count 0 三 null / 三个计数 0），退出码 0，且不创建任何东西；
- --summary 与 --status-summary 同用：在其他参数校验和数据库访问之前
  退出 2，stdout 为空，stderr 说明互斥；
- 错误顺序与说明保持现状：非法参数、目录路径、无效 SQLite、缺字段、
  时间筛选遇到非法 checked_at 均退出 2 且 stdout 为空；
- 查询不发网络请求，不创建文件/目录/表，不改记录；兼容大小写异写表名
  与可读不可写的数据库；check 的探测、持久化与退出码保持原样。

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

TARGET_URL = "http://127.0.0.1:8765/"
CHECKED_AT = "2026-10-05T00:00:00Z"

# 验收主用例的两条记录
REC1 = {
    "id": 1, "url": TARGET_URL, "checked_at": CHECKED_AT,
    "elapsed_ms": 0, "status": "success", "http_status": 200, "reason": "ok",
}
REC2 = {
    "id": 2, "url": TARGET_URL, "checked_at": CHECKED_AT,
    "elapsed_ms": 7, "status": "failure", "http_status": None,
    "reason": "timeout",
}

# 三种形态在验收样本上的预期 stdout（逐字节，含末尾唯一换行）
EXPECTED_RECORDS_JSON = (
    '[{"id":2,"url":"http://127.0.0.1:8765/",'
    '"checked_at":"2026-10-05T00:00:00Z","elapsed_ms":7,"status":"failure",'
    '"http_status":null,"reason":"timeout"},'
    '{"id":1,"url":"http://127.0.0.1:8765/",'
    '"checked_at":"2026-10-05T00:00:00Z","elapsed_ms":0,"status":"success",'
    '"http_status":200,"reason":"ok"}]\n'
)
EXPECTED_SUMMARY_JSON = (
    '{"count":2,"min_elapsed_ms":0,"max_elapsed_ms":7,'
    '"avg_elapsed_ms":3.5}\n'
)
EXPECTED_STATUS_SUMMARY_JSON = (
    '{"count":2,"success_count":1,"failure_count":1}\n'
)

# 空结果的三种形态输出（所有空结果情形共用）
EMPTY_RECORDS_JSON = "[]\n"
EMPTY_SUMMARY_JSON = (
    '{"count":0,"min_elapsed_ms":null,'
    '"max_elapsed_ms":null,"avg_elapsed_ms":null}\n'
)
EMPTY_STATUS_SUMMARY_JSON = (
    '{"count":0,"success_count":0,"failure_count":0}\n'
)


def run_cli(db_path, *extra):
    """以子进程运行 healthcheck.py 命令行，返回 CompletedProcess。"""
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


def run_recent(db_path, *extra):
    return run_cli(db_path, "recent", *extra)


def insert_records(db_path, records, table_name="checks"):
    """创建临时数据库并写入给定记录（可指定历史表名）。"""
    conn = sqlite3.connect(str(db_path))
    conn.execute(SCHEMA_SQL.replace("checks", f'"{table_name}"', 1))
    conn.executemany(
        f'INSERT INTO "{table_name}" (id, url, checked_at, elapsed_ms, '
        "status, http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (r["id"], r["url"], r["checked_at"], r["elapsed_ms"],
             r["status"], r["http_status"], r["reason"])
            for r in records
        ],
    )
    conn.commit()
    conn.close()


def build_acceptance_db(db_path, table_name="checks"):
    insert_records(db_path, [REC1, REC2], table_name=table_name)


def snapshot_state(tmpdir, db_path):
    """捕获目录文件集合与数据库内所有表的全部行，供前后比对。"""
    files = {
        str(p.relative_to(tmpdir))
        for p in tmpdir.rglob("*") if p.is_file()
    }
    tables = {}
    if pathlib.Path(db_path).is_file():
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                names = [
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                ]
                for name in names:
                    tables[name] = conn.execute(
                        f'SELECT * FROM "{name}"'
                    ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            tables["<raw-bytes>"] = pathlib.Path(db_path).read_bytes()
    return files, tables


class RecentRenderRefactorRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-render-"))
        self.db = self.tmp / "monitor.sqlite"
        build_acceptance_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 通用断言 ----

    def assert_ok(self, proc, label):
        self.assertEqual(
            proc.returncode, 0,
            f"{label}: 期望退出码 0，实际 {proc.returncode}，"
            f"stderr={proc.stderr!r}",
        )
        self.assertEqual(proc.stderr, "", f"{label}: stderr 应为空")

    def assert_error(self, proc, label, *fragments):
        self.assertEqual(
            proc.returncode, 2,
            f"{label}: 期望退出码 2，实际 {proc.returncode}，"
            f"stdout={proc.stdout!r}",
        )
        self.assertEqual(proc.stdout, "", f"{label}: stdout 应为空")
        self.assertTrue(
            proc.stderr.startswith("healthcheck: error:"),
            f"{label}: stderr 应以固定错误前缀开头，实际 {proc.stderr!r}",
        )
        for fragment in fragments:
            self.assertIn(
                fragment, proc.stderr,
                f"{label}: stderr 应包含 {fragment!r}，实际 {proc.stderr!r}",
            )
        self.assertNotIn("Traceback", proc.stderr)

    def assert_state_unchanged(self, before, label, db_path=None):
        db_path = self.db if db_path is None else pathlib.Path(db_path)
        after = snapshot_state(self.tmp, db_path)
        self.assertEqual(
            after[0], before[0],
            f"{label}: 查询前后目录文件集合发生变化："
            f"新增 {sorted(after[0] - before[0])}，"
            f"消失 {sorted(before[0] - after[0])}",
        )
        self.assertEqual(
            after[1], before[1],
            f"{label}: 查询前后数据库表或记录内容发生变化",
        )

    # ---- 验收主用例：三种形态作用于同一批记录 ----

    def test_acceptance_two_records_all_three_modes(self):
        before = snapshot_state(self.tmp, self.db)

        proc = run_recent(self.db, "--limit", "2")
        self.assert_ok(proc, "普通模式")
        self.assertEqual(proc.stdout, EXPECTED_RECORDS_JSON)
        self.assertEqual([r["id"] for r in json.loads(proc.stdout)], [2, 1])

        proc = run_recent(self.db, "--limit", "2", "--summary")
        self.assert_ok(proc, "耗时摘要")
        self.assertEqual(proc.stdout, EXPECTED_SUMMARY_JSON)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["min_elapsed_ms"], 0)
        self.assertEqual(summary["max_elapsed_ms"], 7)
        self.assertEqual(summary["avg_elapsed_ms"], 3.5)

        proc = run_recent(self.db, "--limit", "2", "--status-summary")
        self.assert_ok(proc, "状态摘要")
        self.assertEqual(proc.stdout, EXPECTED_STATUS_SUMMARY_JSON)
        status_summary = json.loads(proc.stdout)
        self.assertEqual(status_summary["count"], 2)
        self.assertEqual(status_summary["success_count"], 1)
        self.assertEqual(status_summary["failure_count"], 1)

        self.assert_state_unchanged(before, "验收主用例")

    def test_output_is_single_compact_line(self):
        # 紧凑单行：除末尾唯一换行外无其他空白，字段名与顺序固定
        for extra in ((), ("--summary",), ("--status-summary",)):
            with self.subTest(extra=extra):
                proc = run_recent(self.db, "--limit", "2", *extra)
                self.assert_ok(proc, f"单行 {extra}")
                self.assertTrue(proc.stdout.endswith("\n"))
                self.assertNotIn("\n", proc.stdout[:-1])
                self.assertNotIn(": ", proc.stdout)
                self.assertNotIn(", ", proc.stdout)

    def test_summary_counts_failure_and_zero_elapsed(self):
        # 验收样本本身即覆盖：id 2 为 failure（7 ms）、id 1 零耗时（0 ms），
        # 两者都计入 count/min；均值 (7+0)/2=3.5 不取整
        proc = run_recent(self.db, "--summary")
        self.assert_ok(proc, "摘要口径")
        self.assertEqual(proc.stdout, EXPECTED_SUMMARY_JSON)

    def test_status_summary_uses_stored_status_only(self):
        # 人为构造 status 与 reason/http_status 「不一致」的记录：
        # success 但 http_status 500、reason timeout——分类只依据保存的
        # status 字段，不做任何推断
        odd = {
            "id": 3, "url": TARGET_URL, "checked_at": CHECKED_AT,
            "elapsed_ms": 5, "status": "success", "http_status": 500,
            "reason": "timeout",
        }
        db = self.tmp / "stored-status.sqlite"
        insert_records(db, [REC1, REC2, odd])
        proc = run_recent(db, "--status-summary")
        self.assert_ok(proc, "状态只按保存值")
        self.assertEqual(
            proc.stdout,
            '{"count":3,"success_count":2,"failure_count":1}\n',
        )

    def test_time_window_feeds_same_batch_to_all_modes(self):
        # 时间窗口与 limit 处理后的同一批记录进入三种形态：
        # 窗口恰含两条记录时结果与无窗口一致；窗口排除全部时为空结果
        window = ("--since", "2026-10-05T00:00:00Z",
                  "--until", "2026-10-05T00:00:00Z")
        proc = run_recent(self.db, *window, "--limit", "2")
        self.assert_ok(proc, "窗口内普通")
        self.assertEqual(proc.stdout, EXPECTED_RECORDS_JSON)
        proc = run_recent(self.db, *window, "--limit", "2", "--summary")
        self.assert_ok(proc, "窗口内摘要")
        self.assertEqual(proc.stdout, EXPECTED_SUMMARY_JSON)
        proc = run_recent(self.db, *window, "--limit", "2",
                          "--status-summary")
        self.assert_ok(proc, "窗口内状态摘要")
        self.assertEqual(proc.stdout, EXPECTED_STATUS_SUMMARY_JSON)

        later = ("--since", "2026-10-05T00:00:01Z")
        self.assertEqual(
            run_recent(self.db, *later).stdout, EMPTY_RECORDS_JSON
        )
        self.assertEqual(
            run_recent(self.db, *later, "--summary").stdout,
            EMPTY_SUMMARY_JSON,
        )
        self.assertEqual(
            run_recent(self.db, *later, "--status-summary").stdout,
            EMPTY_STATUS_SUMMARY_JSON,
        )

    def test_unicode_url_and_raw_strings_preserved(self):
        # 原始 URL（含中文）与 checked_at 字符串原样输出，不归一化
        unicode_url = "http://127.0.0.1:8765/健康?项目=巡检"
        rec = {
            "id": 1, "url": unicode_url,
            "checked_at": "2026-10-05T00:00:00.000000+00:00",
            "elapsed_ms": 3, "status": "success", "http_status": 200,
            "reason": "ok",
        }
        db = self.tmp / "unicode.sqlite"
        insert_records(db, [rec])
        proc = run_recent(db, "--url", unicode_url)
        self.assert_ok(proc, "中文 URL")
        self.assertEqual(
            proc.stdout,
            '[{"id":1,"url":"http://127.0.0.1:8765/健康?项目=巡检",'
            '"checked_at":"2026-10-05T00:00:00.000000+00:00",'
            '"elapsed_ms":3,"status":"success","http_status":200,'
            '"reason":"ok"}]\n',
        )

    # ---- 空结果：五种情形三种形态逐字节一致 ----

    def assert_empty_all_modes(self, db_path, label, *extra):
        before = snapshot_state(self.tmp, db_path)
        proc = run_recent(db_path, *extra)
        self.assert_ok(proc, f"{label} 普通")
        self.assertEqual(proc.stdout, EMPTY_RECORDS_JSON, label)
        proc = run_recent(db_path, *extra, "--summary")
        self.assert_ok(proc, f"{label} 摘要")
        self.assertEqual(proc.stdout, EMPTY_SUMMARY_JSON, label)
        proc = run_recent(db_path, *extra, "--status-summary")
        self.assert_ok(proc, f"{label} 状态摘要")
        self.assertEqual(proc.stdout, EMPTY_STATUS_SUMMARY_JSON, label)
        self.assert_state_unchanged(before, label, db_path)

    def test_empty_results_identical_across_scenarios(self):
        missing_db = self.tmp / "no-such-dir" / "nested" / "monitor.sqlite"
        self.assert_empty_all_modes(missing_db, "缺库含父目录")
        self.assertFalse(
            (self.tmp / "no-such-dir").exists(),
            "查询不应创建缺失的目录或数据库文件",
        )

        no_table_db = self.tmp / "other-only.sqlite"
        conn = sqlite3.connect(str(no_table_db))
        conn.execute("CREATE TABLE other_t (name TEXT NOT NULL)")
        conn.execute("INSERT INTO other_t (name) VALUES ('kept')")
        conn.commit()
        conn.close()
        self.assert_empty_all_modes(no_table_db, "无历史表")

        empty_db = self.tmp / "empty.sqlite"
        conn = sqlite3.connect(str(empty_db))
        conn.execute(SCHEMA_SQL)
        conn.commit()
        conn.close()
        self.assert_empty_all_modes(empty_db, "空表")

        self.assert_empty_all_modes(
            self.db, "筛选无匹配", "--url",
            "http://127.0.0.1:8765/no-such-path",
        )

    # ---- 两种摘要互斥：先于其他校验与数据库访问 ----

    def test_summary_and_status_summary_mutually_exclusive(self):
        proc = run_recent(self.db, "--summary", "--status-summary")
        self.assert_error(proc, "摘要互斥", "互斥")

    def test_mutex_checked_before_other_validation_and_db(self):
        # 同时给出非法 --status、非法 --url 与缺失的库路径：
        # 仍只报互斥，且不触碰数据库路径
        missing_db = self.tmp / "no-such-dir" / "monitor.sqlite"
        proc = run_recent(
            missing_db, "--summary", "--status-summary",
            "--status", "nope", "--url", "https://127.0.0.1:8765/x",
        )
        self.assert_error(proc, "互斥优先", "互斥")
        self.assertNotIn("status 参数错误", proc.stderr)
        self.assertFalse(
            (self.tmp / "no-such-dir").exists(),
            "互斥拒绝不应创建任何目录或文件",
        )

    # ---- 错误边界：退出码 2、stdout 空、说明保持现状 ----

    def test_invalid_arguments_rejected(self):
        for value in ("0", "-1", "1.5", "abc"):
            with self.subTest(limit=value):
                proc = run_recent(self.db, "--limit", value)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, "")
                self.assertNotIn("Traceback", proc.stderr)
        proc = run_recent(self.db, "--status", "Success")
        self.assert_error(proc, "非法 status", "status 参数错误")
        proc = run_recent(self.db, "--reason", "Timeout")
        self.assert_error(proc, "非法 reason", "reason 参数错误")
        proc = run_recent(self.db, "--since", "2026-10-05")
        self.assert_error(proc, "非法 since", "since 参数错误")

    def test_directory_db_path_rejected(self):
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir()
        before = snapshot_state(self.tmp, db_dir)
        proc = run_recent(db_dir)
        self.assert_error(proc, "目录路径", "目录")
        self.assert_state_unchanged(before, "目录路径", db_dir)

    def test_invalid_sqlite_file_rejected(self):
        bad_db = self.tmp / "not-a-database.sqlite"
        bad_db.write_bytes(b"this is not sqlite content at all")
        before = snapshot_state(self.tmp, bad_db)
        proc = run_recent(bad_db)
        self.assert_error(proc, "无效 SQLite", "读取数据库")
        self.assert_state_unchanged(before, "无效 SQLite", bad_db)

    def test_missing_columns_rejected_with_names(self):
        db = self.tmp / "missing-cols.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE checks ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "url TEXT NOT NULL, checked_at TEXT NOT NULL, "
            "elapsed_ms INTEGER NOT NULL, status TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()
        before = snapshot_state(self.tmp, db)
        proc = run_recent(db)
        self.assert_error(proc, "缺字段", "缺少字段", "http_status", "reason")
        self.assert_state_unchanged(before, "缺字段", db)

    def test_bad_checked_at_rejected_under_time_filter(self):
        bad = {
            "id": 3, "url": TARGET_URL, "checked_at": "not-a-timestamp",
            "elapsed_ms": 1, "status": "success", "http_status": 200,
            "reason": "ok",
        }
        db = self.tmp / "bad-checked-at.sqlite"
        insert_records(db, [REC1, REC2, bad])
        before = snapshot_state(self.tmp, db)
        proc = run_recent(db, "--since", "2026-10-05T00:00:00Z")
        self.assert_error(proc, "非法 checked_at", "id=3", "checked_at")
        self.assert_state_unchanged(before, "非法 checked_at", db)

    # ---- 兼容与副作用保证 ----

    def test_case_variant_table_name(self):
        db = self.tmp / "CHECKS.sqlite"
        build_acceptance_db(db, table_name="CHECKS")
        proc = run_recent(db, "--limit", "2")
        self.assert_ok(proc, "CHECKS 表")
        self.assertEqual(proc.stdout, EXPECTED_RECORDS_JSON)
        proc = run_recent(db, "--status-summary")
        self.assert_ok(proc, "CHECKS 表状态摘要")
        self.assertEqual(proc.stdout, EXPECTED_STATUS_SUMMARY_JSON)

    def test_readonly_database_is_queryable(self):
        ro_db = self.tmp / "readonly.sqlite"
        shutil.copy(self.db, ro_db)
        os.chmod(ro_db, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        try:
            proc = run_recent(ro_db, "--limit", "2")
            self.assert_ok(proc, "只读库")
            self.assertEqual(proc.stdout, EXPECTED_RECORDS_JSON)
            sidecars = [
                p.name for p in self.tmp.glob("readonly.sqlite*")
                if p.name != "readonly.sqlite"
            ]
            self.assertEqual(
                sidecars, [],
                f"只读查询不应产生 -wal/-journal 旁路文件：{sidecars}",
            )
        finally:
            os.chmod(ro_db, stat.S_IREAD | stat.S_IWRITE)

    def test_query_makes_no_network_requests(self):
        hits = []

        class CountingHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        try:
            server = http.server.ThreadingHTTPServer(
                ("127.0.0.1", 8765), CountingHandler
            )
        except OSError as exc:
            self.skipTest(f"无法绑定 127.0.0.1:8765 以核对网络静默：{exc}")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for extra in ((), ("--summary",), ("--status-summary",)):
                proc = run_recent(self.db, "--limit", "2", *extra)
                self.assertEqual(proc.returncode, 0, proc.stderr)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(
            hits, [], f"查询不应发出任何网络请求，实际收到 {hits}"
        )

    # ---- check 行为保持原样 ----

    def test_check_probe_persist_and_exit_code_unchanged(self):
        class OkHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), OkHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        db = self.tmp / "check.sqlite"
        url = f"http://127.0.0.1:{port}/"
        try:
            proc = run_cli(db, "check", "--url", url)
            self.assertEqual(
                proc.returncode, 0,
                f"check 成功应退出 0，实际 {proc.returncode}，"
                f"stderr={proc.stderr!r}",
            )
            self.assertEqual(proc.stderr, "")
            record = json.loads(proc.stdout)
            self.assertEqual(record["url"], url)
            self.assertEqual(record["status"], "success")
            self.assertEqual(record["http_status"], 200)
            self.assertEqual(record["reason"], "ok")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        # check 持久化的记录可由 recent 原样读回（七字段）
        proc = run_recent(db)
        self.assert_ok(proc, "check 后 recent")
        records = json.loads(proc.stdout)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["url"], url)
        self.assertEqual(records[0]["status"], "success")


if __name__ == "__main__":
    unittest.main(verbosity=2)
