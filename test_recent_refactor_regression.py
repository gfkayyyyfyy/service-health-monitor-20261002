#!/usr/bin/env python3
"""recent 历史筛选流程重构的回归验证。

重构把原来为 url/status/reason 八种筛选组合分别维护的八条 SQL 与八分支
if/elif，合并为单一模板 + build_recent_query 动态组装。本测试通过公开
命令行入口（子进程运行 healthcheck.py）核对重构后的行为与原语义完全等价：

- 八种筛选组合（含普通输出与 --summary）在固定五条样本上的结果；
- 先筛选、再按 id 倒序取 limit（默认五条，checked_at 不决定顺序）；
- 验收主用例：--url A --reason timeout --limit 2 → id [3, 1]，
  加 --summary → {"count":2,"min_elapsed_ms":0,"max_elapsed_ms":10,
  "avg_elapsed_ms":5.0}，两次退出 0、stderr 为空；
- 空结果边界：缺库/父目录缺失、无历史表、空表、无匹配 → 退出 0，
  普通输出 []、摘要 count 0 三个耗时字段为 null；
- 错误边界：目录、无效 SQLite、历史表缺字段（stderr 列出缺失名）→
  退出 2、stdout 为空；非法 limit 由 argparse 拒绝；
- 报错顺序：status → URL → reason → 数据库路径；
- 历史表名大小写异写（CHECKS/Checks）等价；可读不可写的库仍可查询；
- 查询不发网络请求，不创建文件/目录/表，不修改记录
  （前后目录文件集合与库中全部数据快照比对）。

在项目目录执行：
    python3 -m unittest test_recent_refactor_regression
或：
    python3 test_recent_refactor_regression.py
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

COLUMNS = ["id", "url", "checked_at", "elapsed_ms",
           "status", "http_status", "reason"]

URL_A = "http://127.0.0.1:8765/health"
URL_B = "http://127.0.0.1:8765/ready"
URL_NO_MATCH = "http://127.0.0.1:8765/no-such-path"

# 验收固定样本（同 test_recent_reason_filter.py）：
# id 1..5 依次为 A timeout、A ok、A timeout、B timeout、A ok；
# elapsed_ms 依次为 0、4、10、20、9。
REC1 = {
    "id": 1, "url": URL_A,
    "checked_at": "2026-10-04T00:00:01.000000+00:00",
    "elapsed_ms": 0, "status": "failure", "http_status": None,
    "reason": "timeout",
}
REC2 = {
    "id": 2, "url": URL_A,
    "checked_at": "2026-10-04T00:00:02.000000+00:00",
    "elapsed_ms": 4, "status": "success", "http_status": 200, "reason": "ok",
}
REC3 = {
    "id": 3, "url": URL_A,
    "checked_at": "2026-10-04T00:00:03.000000+00:00",
    "elapsed_ms": 10, "status": "failure", "http_status": None,
    "reason": "timeout",
}
REC4 = {
    "id": 4, "url": URL_B,
    "checked_at": "2026-10-04T00:00:04.000000+00:00",
    "elapsed_ms": 20, "status": "failure", "http_status": None,
    "reason": "timeout",
}
REC5 = {
    "id": 5, "url": URL_A,
    "checked_at": "2026-10-04T00:00:05.000000+00:00",
    "elapsed_ms": 9, "status": "success", "http_status": 200, "reason": "ok",
}

ALL_RECORDS = [REC1, REC2, REC3, REC4, REC5]
BY_ID = {r["id"]: r for r in ALL_RECORDS}

EMPTY_SUMMARY = {
    "count": 0,
    "min_elapsed_ms": None,
    "max_elapsed_ms": None,
    "avg_elapsed_ms": None,
}


def run_cli(db_path, *extra):
    """以子进程运行 healthcheck.py 命令行，返回 CompletedProcess。"""
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


def run_recent(db_path, *extra):
    return run_cli(db_path, "recent", *extra)


def build_sample_db(path, table_name="checks"):
    """创建包含验收固定样本 5 条记录的临时数据库（可指定历史表名）。"""
    conn = sqlite3.connect(str(path))
    conn.execute(SCHEMA_SQL.replace("checks", f'"{table_name}"', 1))
    conn.executemany(
        "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
        "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (r["id"], r["url"], r["checked_at"], r["elapsed_ms"],
             r["status"], r["http_status"], r["reason"])
            for r in ALL_RECORDS
        ],
    )
    conn.commit()
    conn.close()


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
            # 不是有效 SQLite 文件：记录原始字节供前后比对
            tables["<raw-bytes>"] = pathlib.Path(db_path).read_bytes()
    return files, tables


def expected_summary(ids):
    """按 id 列表从样本直接计算摘要，作为 --summary 的期望口径。"""
    elapsed = [BY_ID[i]["elapsed_ms"] for i in ids]
    if not elapsed:
        return EMPTY_SUMMARY
    return {
        "count": len(elapsed),
        "min_elapsed_ms": min(elapsed),
        "max_elapsed_ms": max(elapsed),
        "avg_elapsed_ms": sum(elapsed) / len(elapsed),
    }


# 八种筛选组合：(子命令额外参数, 期望 id 顺序)。
# 样本上 url 与 reason 的交集、status 与 reason 的交集等均由保存值决定。
COMBINATIONS = [
    ("无筛选", (), [5, 4, 3, 2, 1]),
    ("仅 url", ("--url", URL_A), [5, 3, 2, 1]),
    ("仅 status", ("--status", "failure"), [4, 3, 1]),
    ("仅 reason", ("--reason", "timeout"), [4, 3, 1]),
    ("url+status", ("--url", URL_A, "--status", "failure"), [3, 1]),
    ("url+reason", ("--url", URL_A, "--reason", "timeout"), [3, 1]),
    ("status+reason", ("--status", "failure", "--reason", "timeout"),
     [4, 3, 1]),
    ("url+status+reason",
     ("--url", URL_A, "--status", "failure", "--reason", "timeout"), [3, 1]),
]


class RecentRefactorRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-refactor-"))
        self.db = self.tmp / "monitor.sqlite"
        build_sample_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_state_unchanged(self, before, label, db_path=None):
        """查询不得改变目录文件集合、表结构或任何已有记录。"""
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

    def assert_ok(self, proc, label):
        self.assertEqual(
            proc.returncode, 0,
            f"{label}: 期望退出码 0，实际 {proc.returncode}，"
            f"stderr={proc.stderr!r}",
        )
        self.assertEqual(proc.stderr, "", f"{label}: stderr 应为空")

    # ---- 验收主用例（任务指定） ----

    def test_acceptance_url_reason_limit_2(self):
        label = "recent --url A --reason timeout --limit 2"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--url", URL_A,
                          "--reason", "timeout", "--limit", "2")
        self.assert_ok(proc, label)
        self.assertEqual(
            json.loads(proc.stdout), [REC3, REC1],
            f"{label}: 期望 id [3, 1] 的原记录",
        )
        self.assert_state_unchanged(before, label)

        proc_s = run_recent(self.db, "--url", URL_A,
                            "--reason", "timeout", "--limit", "2", "--summary")
        self.assert_ok(proc_s, label + " --summary")
        self.assertEqual(
            json.loads(proc_s.stdout),
            {"count": 2, "min_elapsed_ms": 0,
             "max_elapsed_ms": 10, "avg_elapsed_ms": 5.0},
        )
        self.assert_state_unchanged(before, label + " --summary")

    # ---- 八种筛选组合：普通输出与摘要 ----

    def test_all_eight_filter_combinations(self):
        for name, extra, ids in COMBINATIONS:
            with self.subTest(combination=name):
                before = snapshot_state(self.tmp, self.db)
                proc = run_recent(self.db, *extra)
                self.assert_ok(proc, name)
                records = json.loads(proc.stdout)
                self.assertEqual(
                    records, [BY_ID[i] for i in ids],
                    f"{name}: 期望 id {ids}，"
                    f"实际 {[r['id'] for r in records]}",
                )
                for rec in records:
                    self.assertEqual(set(rec.keys()), set(COLUMNS))
                self.assert_state_unchanged(before, name)

                proc_s = run_recent(self.db, *extra, "--summary")
                self.assert_ok(proc_s, name + " --summary")
                self.assertEqual(
                    json.loads(proc_s.stdout), expected_summary(ids),
                    f"{name} --summary: 摘要应统计同一批记录",
                )
                self.assert_state_unchanged(before, name + " --summary")

    def test_filtering_before_limit_and_id_desc_order(self):
        # checked_at 与 id 同向递增，但排序键只有 id：先筛选（A 且 timeout
        # 为 id 3、1）再倒序取 1 条 → id 3
        proc = run_recent(self.db, "--url", URL_A,
                          "--reason", "timeout", "--limit", "1")
        self.assert_ok(proc, "limit 1")
        self.assertEqual([r["id"] for r in json.loads(proc.stdout)], [3])
        # 默认 limit 为 5：无筛选时 5 条样本全部返回
        proc_all = run_recent(self.db)
        self.assert_ok(proc_all, "默认 limit")
        self.assertEqual(len(json.loads(proc_all.stdout)), 5)

    # ---- 空结果边界：退出 0，[] / count0 三 null ----

    def assert_empty_result(self, db_path, label, *extra):
        before = snapshot_state(self.tmp, db_path)
        proc = run_recent(db_path, *extra)
        self.assert_ok(proc, label)
        self.assertEqual(proc.stdout, "[]\n", f"{label}: 普通模式应输出 []")
        proc_s = run_recent(db_path, *extra, "--summary")
        self.assert_ok(proc_s, label + " --summary")
        self.assertEqual(
            json.loads(proc_s.stdout), EMPTY_SUMMARY,
            f"{label} --summary: 应为 count 0 三 null",
        )
        self.assert_state_unchanged(before, label, db_path)

    def test_empty_when_db_and_parent_missing(self):
        missing_root = self.tmp / "does-not-exist"
        missing_db = missing_root / "nested" / "monitor.sqlite"
        self.assert_empty_result(missing_db, "缺库含父目录",
                                 "--reason", "timeout")
        self.assertFalse(
            missing_root.exists(), "查询不应创建缺失的目录或数据库文件"
        )

    def test_empty_when_no_checks_table(self):
        other_db = self.tmp / "other-only.sqlite"
        conn = sqlite3.connect(str(other_db))
        conn.execute("CREATE TABLE other_t (name TEXT NOT NULL)")
        conn.execute("INSERT INTO other_t (name) VALUES ('kept')")
        conn.commit()
        conn.close()
        self.assert_empty_result(other_db, "无历史表", "--status", "failure")

    def test_empty_when_checks_table_empty(self):
        empty_db = self.tmp / "empty.sqlite"
        conn = sqlite3.connect(str(empty_db))
        conn.execute(SCHEMA_SQL)
        conn.commit()
        conn.close()
        self.assert_empty_result(empty_db, "空表", "--reason", "ok")

    def test_empty_when_no_match(self):
        self.assert_empty_result(
            self.db, "URL 无匹配", "--url", URL_NO_MATCH,
            "--reason", "timeout",
        )
        # 合法值但样本中无此 reason 的记录（不从 status/http_status 推断）
        self.assert_empty_result(self.db, "reason 无匹配",
                                 "--reason", "http_status")
        # 三条件交集为空
        self.assert_empty_result(
            self.db, "交集为空", "--url", URL_A,
            "--status", "success", "--reason", "timeout",
        )

    # ---- 错误边界：退出 2、stdout 空、原因写 stderr ----

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

    def test_directory_db_path_rejected(self):
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir()
        before = snapshot_state(self.tmp, db_dir)
        proc = run_recent(db_dir, "--reason", "timeout")
        self.assert_error(proc, "目录路径", "目录")
        self.assert_state_unchanged(before, "目录路径", db_dir)

    def test_invalid_sqlite_file_rejected(self):
        bad_db = self.tmp / "not-a-database.sqlite"
        bad_db.write_bytes(b"this is not sqlite content at all")
        before = snapshot_state(self.tmp, bad_db)
        proc = run_recent(bad_db, "--url", URL_A)
        self.assert_error(proc, "无效 SQLite", "读取数据库")
        self.assert_state_unchanged(before, "无效 SQLite", bad_db)

    def test_missing_columns_rejected_with_names(self):
        # 缺 http_status 与 reason 两列的历史表：退出 2 并列出缺失字段名
        for table in ("checks", "CHECKS"):
            with self.subTest(table=table):
                db = self.tmp / f"missing-cols-{table}.sqlite"
                conn = sqlite3.connect(str(db))
                conn.execute(
                    f'CREATE TABLE "{table}" ('
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "url TEXT NOT NULL, checked_at TEXT NOT NULL, "
                    "elapsed_ms INTEGER NOT NULL, status TEXT NOT NULL)"
                )
                conn.commit()
                conn.close()
                before = snapshot_state(self.tmp, db)
                proc = run_recent(db, "--reason", "timeout")
                self.assert_error(
                    proc, f"缺字段({table})", "缺少字段",
                    "http_status", "reason",
                )
                self.assert_state_unchanged(before, f"缺字段({table})", db)

    def test_invalid_limit_rejected_by_argparse(self):
        for value in ("0", "-1", "1.5", "abc"):
            with self.subTest(limit=value):
                proc = run_recent(self.db, "--limit", value)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, "")
                self.assertNotIn("Traceback", proc.stderr)

    def test_validation_order_status_url_reason_db(self):
        db_dir = self.tmp / "order-dir"
        db_dir.mkdir()

        # 非法 status + 非法 URL + 非法 reason + 目录库：只报 status
        proc = run_recent(db_dir, "--status", "nope", "--reason", "bad",
                          "--url", "https://127.0.0.1:8765/x")
        self.assert_error(proc, "status 优先", "status")
        self.assertNotIn("reason 参数错误", proc.stderr)
        self.assertNotIn("目录", proc.stderr)

        # status 合法后：非法 URL 优先于 reason 与目录错误
        proc = run_recent(db_dir, "--status", "success", "--reason", "bad",
                          "--url", "https://127.0.0.1:8765/x")
        self.assert_error(proc, "URL 优先", "协议")
        self.assertNotIn("reason 参数错误", proc.stderr)
        self.assertNotIn("目录", proc.stderr)

        # status、URL 合法后：非法 reason 优先于目录错误
        proc = run_recent(db_dir, "--status", "success",
                          "--url", URL_A, "--reason", "bad")
        self.assert_error(proc, "reason 优先", "reason", "'bad'")
        self.assertNotIn("目录", proc.stderr)

        # 裸 --status / 裸 --reason 缺值同样被各自的校验拒绝
        proc = run_recent(db_dir, "--status")
        self.assert_error(proc, "裸 --status", "status", "缺少值")
        proc = run_recent(db_dir, "--url", URL_A, "--reason")
        self.assert_error(proc, "裸 --reason", "reason", "缺少值")
        self.assertNotIn("目录", proc.stderr)

        # 全部参数合法时，目录路径错误照常报告
        proc = run_recent(db_dir, "--reason", "timeout")
        self.assert_error(proc, "目录最后", "目录")

    # ---- 表名大小写异写与只读库 ----

    def test_case_variant_table_names(self):
        for table in ("CHECKS", "Checks"):
            with self.subTest(table=table):
                db = self.tmp / f"{table}.sqlite"
                build_sample_db(db, table_name=table)
                before = snapshot_state(self.tmp, db)
                proc = run_recent(db, "--url", URL_A,
                                  "--reason", "timeout", "--limit", "2")
                self.assert_ok(proc, f"表 {table}")
                self.assertEqual(json.loads(proc.stdout), [REC3, REC1])
                self.assert_state_unchanged(before, f"表 {table}", db)

    def test_readonly_database_is_queryable(self):
        ro_db = self.tmp / "readonly.sqlite"
        shutil.copy(self.db, ro_db)
        os.chmod(ro_db, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        try:
            proc = run_recent(ro_db, "--url", URL_A,
                              "--reason", "timeout", "--limit", "2")
            self.assert_ok(proc, "只读库")
            self.assertEqual(
                [r["id"] for r in json.loads(proc.stdout)], [3, 1]
            )
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
            for _name, extra, _ids in COMBINATIONS:
                proc = run_recent(self.db, *extra)
                self.assertEqual(proc.returncode, 0, proc.stderr)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(
            hits, [], f"查询不应发出任何网络请求，实际收到 {hits}"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
