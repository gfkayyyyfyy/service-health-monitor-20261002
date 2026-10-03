#!/usr/bin/env python3
"""recent 对仅大小写不同的历史表名（CHECKS / Checks）的回归测试。

背景：旧实现用 `name == 'checks'` 精确匹配 sqlite_master 中的表名，
名为 CHECKS 或 Checks 的历史表被误判为「没有历史表」而返回空历史。
修复后表名按大小写不敏感识别，三种写法给出完全相同的查询结果；
识别到的表缺任一所需字段时以退出码 2 说明缺列，不再误报空历史。

验收固定样本（三条记录，完整字段、合法 UTC 时间）：
- id 1：http://127.0.0.1:8765/health，failure，0 ms，connection_error，http_status null
- id 2：http://127.0.0.1:8765/ready，success，8 ms，ok，http_status 200
- id 3：http://127.0.0.1:8765/health，failure，6 ms，timeout，http_status null

查询 health 目标的 failure 并 --limit 1 只返回 id 3（字段原样保留）；
同条件加 --summary 得 {"count":1,"min_elapsed_ms":6,"max_elapsed_ms":6,
"avg_elapsed_ms":6.0}。CHECKS、Checks 与 checks 结果一致。

运行：python3 -m unittest test_recent_case_table
"""

import json
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "healthcheck.py"

COLUMNS = ["id", "url", "checked_at", "elapsed_ms",
           "status", "http_status", "reason"]

URL_HEALTH = "http://127.0.0.1:8765/health"
URL_READY = "http://127.0.0.1:8765/ready"

REC1 = {
    "id": 1, "url": URL_HEALTH,
    "checked_at": "2026-10-04T00:00:01.000000+00:00",
    "elapsed_ms": 0, "status": "failure", "http_status": None,
    "reason": "connection_error",
}
REC2 = {
    "id": 2, "url": URL_READY,
    "checked_at": "2026-10-04T00:00:02.000000+00:00",
    "elapsed_ms": 8, "status": "success", "http_status": 200, "reason": "ok",
}
REC3 = {
    "id": 3, "url": URL_HEALTH,
    "checked_at": "2026-10-04T00:00:03.000000+00:00",
    "elapsed_ms": 6, "status": "failure", "http_status": None,
    "reason": "timeout",
}
ALL_RECORDS = [REC1, REC2, REC3]

EMPTY_SUMMARY = {
    "count": 0,
    "min_elapsed_ms": None,
    "max_elapsed_ms": None,
    "avg_elapsed_ms": None,
}

# 覆盖大写、首字母大写与小写三种历史表名
TABLE_NAMES = ["CHECKS", "Checks", "checks"]


def run_recent(db_path, *extra):
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), "recent", *extra]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


def build_sample_db(path, table_name):
    """以指定表名创建历史表并写入验收固定样本三条记录。"""
    conn = sqlite3.connect(str(path))
    conn.execute(
        f'CREATE TABLE "{table_name}" ('
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "url TEXT NOT NULL, "
        "checked_at TEXT NOT NULL, "
        "elapsed_ms INTEGER NOT NULL CHECK (elapsed_ms >= 0), "
        "status TEXT NOT NULL CHECK (status IN ('success', 'failure')), "
        "http_status INTEGER, "
        "reason TEXT NOT NULL CHECK (reason IN "
        "('ok', 'http_status', 'connection_error', 'timeout')))"
    )
    conn.executemany(
        f'INSERT INTO "{table_name}" (id, url, checked_at, elapsed_ms, '
        "status, http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
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
    return files, tables


class RecentCaseInsensitiveTableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-casetable-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def db_for(self, table_name):
        db = self.tmp / f"{table_name}.sqlite"
        build_sample_db(db, table_name)
        return db

    # ---- 验收主用例：三种表名写法给出相同结果 ----

    def test_health_failure_limit_1_same_for_all_casings(self):
        outputs = {}
        for table in TABLE_NAMES:
            with self.subTest(table=table):
                db = self.db_for(table)
                before = snapshot_state(self.tmp, db)
                proc = run_recent(db, "--url", URL_HEALTH,
                                  "--status", "failure", "--limit", "1")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")
                records = json.loads(proc.stdout)
                # 先筛选（health 且 failure：id 1、3）再倒序取 1 条 → id 3，
                # 七个字段值原样保留
                self.assertEqual(
                    records, [REC3],
                    f"表 {table}：期望仅 id 3 的完整记录，实际 {records}",
                )
                outputs[table] = proc.stdout
                # 查询只读：目录文件集合与表内容均无变化
                self.assertEqual(
                    snapshot_state(self.tmp, db), before,
                    f"表 {table}：查询前后文件或数据发生变化",
                )
        self.assertEqual(
            len(set(outputs.values())), 1,
            f"三种表名写法的输出应完全一致：{outputs}",
        )

    def test_health_failure_limit_1_summary_same_for_all_casings(self):
        outputs = {}
        for table in TABLE_NAMES:
            with self.subTest(table=table):
                db = self.db_for(table)
                proc = run_recent(db, "--url", URL_HEALTH,
                                  "--status", "failure",
                                  "--limit", "1", "--summary")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")
                self.assertEqual(
                    json.loads(proc.stdout),
                    {"count": 1, "min_elapsed_ms": 6,
                     "max_elapsed_ms": 6, "avg_elapsed_ms": 6.0},
                    f"表 {table}：摘要应只统计 id 3 的 6 ms",
                )
                outputs[table] = proc.stdout
        self.assertEqual(len(set(outputs.values())), 1)

    def test_unfiltered_query_sees_all_rows_in_case_variant_table(self):
        # 不带筛选：CHECKS 表的全部三条记录按 id 倒序返回
        db = self.db_for("CHECKS")
        proc = run_recent(db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [REC3, REC2, REC1])
        # 仅按 url 精确匹配：/ready 的成功记录
        proc_r = run_recent(db, "--url", URL_READY)
        self.assertEqual(json.loads(proc_r.stdout), [REC2])

    # ---- 空结果语义不变：空表、无匹配仍为 [] / count 0 三 null ----

    def test_empty_case_variant_table_returns_empty(self):
        db = self.tmp / "empty.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE CHECKS ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "url TEXT NOT NULL, checked_at TEXT NOT NULL, "
            "elapsed_ms INTEGER NOT NULL, status TEXT NOT NULL, "
            "http_status INTEGER, reason TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()
        proc = run_recent(db, "--status", "failure")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "[]\n")
        proc_s = run_recent(db, "--status", "failure", "--summary")
        self.assertEqual(proc_s.returncode, 0, proc_s.stderr)
        self.assertEqual(json.loads(proc_s.stdout), EMPTY_SUMMARY)

    def test_no_matching_filter_returns_empty(self):
        db = self.db_for("Checks")
        proc = run_recent(db, "--url", URL_READY, "--status", "failure")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "[]\n")
        proc_s = run_recent(db, "--url", URL_READY,
                            "--status", "failure", "--summary")
        self.assertEqual(json.loads(proc_s.stdout), EMPTY_SUMMARY)

    # ---- 缺列的大小写异写表：退出码 2、stdout 空、stderr 说明缺列 ----

    def test_case_variant_table_missing_columns_rejected(self):
        for table in ("CHECKS", "Checks"):
            with self.subTest(table=table):
                db = self.tmp / f"bad-{table}.sqlite"
                conn = sqlite3.connect(str(db))
                conn.execute(
                    f'CREATE TABLE "{table}" ('
                    "id INTEGER PRIMARY KEY, url TEXT NOT NULL)"
                )
                conn.execute(
                    f'INSERT INTO "{table}" VALUES (7, ?)',
                    ("http://127.0.0.1:1/old",),
                )
                conn.commit()
                conn.close()
                before = snapshot_state(self.tmp, db)
                proc = run_recent(db, "--status", "failure")
                self.assertEqual(proc.returncode, 2, proc.stdout)
                self.assertEqual(proc.stdout, "")
                self.assertIn("healthcheck: error:", proc.stderr)
                self.assertIn("缺少字段", proc.stderr)
                for name in ("checked_at", "elapsed_ms",
                             "status", "http_status", "reason"):
                    self.assertIn(name, proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                # 拒绝时不改动表结构与已有行
                self.assertEqual(snapshot_state(self.tmp, db), before)

    # ---- 错误优先级不变：非法 status 优先于缺列等数据库错误 ----

    def test_invalid_status_still_rejected_before_db_access(self):
        db = self.tmp / "bad-status.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE CHECKS (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        proc = run_recent(db, "--status", "Failure")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("status", proc.stderr)
        self.assertNotIn("缺少字段", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
