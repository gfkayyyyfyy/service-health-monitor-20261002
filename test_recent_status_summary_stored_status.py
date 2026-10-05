#!/usr/bin/env python3
"""recent --status-summary 只按保存的 status 计数的回归测试。

与 test_recent_status_summary.py 中状态、原因、HTTP 状态码相互一致的
样本不同，本文件的固定样本符合 checks 表的全部约束，但字段含义刻意
不一致（success 配 timeout、success 配 500、failure 配 ok/200），
以确认状态摘要只依据保存的 status 字段分类，绝不从 reason 或
http_status 重新推断成功或失败。

通过公开命令行入口（子进程运行 healthcheck.py 的 recent 命令，
经 --db 指定临时 SQLite 数据库）核对退出码、单行 JSON 输出与空
stderr；每个用例使用独立临时数据库，结束后释放临时资源，不依赖
已有历史文件或在线服务；查询前后的表结构、记录内容与目录文件集合
逐一比对，确认查询严格只读。

在项目目录执行：
    python3 -m unittest test_recent_status_summary_stored_status
或：
    python3 test_recent_status_summary_stored_status.py
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

STATUS_SUMMARY_KEYS = {"count", "success_count", "failure_count"}

# ---- 固定样本 -------------------------------------------------------------
# 三条记录共用同一目标、同一 checked_at、零耗时；status 与
# reason/http_status 的组合在含义上相互矛盾，但每一行都满足表约束。
URL = "http://127.0.0.1:8765/health"
CHECKED_AT = "2026-10-05T00:00:00Z"

REC1 = {
    "id": 1, "url": URL, "checked_at": CHECKED_AT,
    "elapsed_ms": 0, "status": "success", "http_status": None,
    "reason": "timeout",
}
REC2 = {
    "id": 2, "url": URL, "checked_at": CHECKED_AT,
    "elapsed_ms": 0, "status": "success", "http_status": 500,
    "reason": "http_status",
}
REC3 = {
    "id": 3, "url": URL, "checked_at": CHECKED_AT,
    "elapsed_ms": 0, "status": "failure", "http_status": 200,
    "reason": "ok",
}

ALL_RECORDS = [REC1, REC2, REC3]


def run_cli(db_path, *extra):
    """以子进程运行 healthcheck.py 命令行，返回 CompletedProcess。"""
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


def run_status_summary(db_path, *extra):
    return run_cli(db_path, "recent", "--status-summary", *extra)


def build_sample_db(path):
    """创建包含固定样本 3 条记录的临时数据库。"""
    conn = sqlite3.connect(str(path))
    conn.execute(SCHEMA_SQL)
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
    """捕获目录文件集合、表结构与数据库内所有表的全部行，供前后比对。"""
    files = {
        str(p.relative_to(tmpdir))
        for p in tmpdir.rglob("*") if p.is_file()
    }
    schema = []
    tables = {}
    if pathlib.Path(db_path).is_file():
        conn = sqlite3.connect(str(db_path))
        try:
            schema = conn.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY name"
            ).fetchall()
            names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
            for name in names:
                tables[name] = conn.execute(
                    f"SELECT * FROM {name}"
                ).fetchall()
        finally:
            conn.close()
    return files, schema, tables


class RecentStatusSummaryStoredStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(
            tempfile.mkdtemp(prefix="hc-status-summary-stored-")
        )
        self.db = self.tmp / "monitor.sqlite"
        build_sample_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_state_unchanged(self, before, label):
        """状态摘要查询不得改变目录文件集合、表结构或任何已有记录。"""
        after = snapshot_state(self.tmp, self.db)
        self.assertEqual(
            after[0], before[0],
            f"输入 {label}：查询前后目录文件集合发生变化："
            f"新增 {sorted(after[0] - before[0])}，"
            f"消失 {sorted(before[0] - after[0])}",
        )
        self.assertEqual(
            after[1], before[1],
            f"输入 {label}：查询前后表结构发生变化",
        )
        self.assertEqual(
            after[2], before[2],
            f"输入 {label}：查询前后数据库记录内容发生变化",
        )

    def assert_status_summary_ok(self, proc, expected, label):
        """退出码 0、stderr 为空、单行 JSON 对象且恰含三个整数字段。"""
        self.assertEqual(
            proc.returncode, 0,
            f"输入 {label}：期望退出码 0，实际 {proc.returncode}，"
            f"stderr={proc.stderr!r}",
        )
        self.assertEqual(
            proc.stderr, "",
            f"输入 {label}：期望 stderr 为空，实际 {proc.stderr!r}",
        )
        self.assertEqual(
            proc.stdout.count("\n"), 1,
            f"输入 {label}：状态摘要应为一行 JSON 对象，实际 {proc.stdout!r}",
        )
        summary = json.loads(proc.stdout)
        self.assertEqual(
            set(summary.keys()), STATUS_SUMMARY_KEYS,
            f"输入 {label}：状态摘要应仅含字段 {STATUS_SUMMARY_KEYS}，"
            f"实际 {set(summary.keys())}",
        )
        for key in STATUS_SUMMARY_KEYS:
            self.assertIsInstance(
                summary[key], int,
                f"输入 {label}：{key} 应为整数，实际 {summary[key]!r}",
            )
        self.assertEqual(
            summary, expected,
            f"输入 {label}：期望状态摘要 {expected}，实际 {summary}",
        )
        return summary

    # ---- 只按保存的 status 计数，不从 reason/http_status 重新推断 ----

    def test_counts_use_stored_status_only(self):
        label = "recent --status-summary（省略全部筛选）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_status_summary(self.db)
        # id 1（success 但 reason=timeout）、id 2（success 但 500）、
        # id 3（failure 但 ok/200）：若按 reason/http_status 推断会得到
        # 1 成功 2 失败，按保存的 status 计数才是 2 成功 1 失败
        self.assert_status_summary_ok(
            proc,
            {"count": 3, "success_count": 2, "failure_count": 1},
            label,
        )
        self.assert_state_unchanged(before, label)

    def test_limit_2_counts_latest_two_by_id_desc(self):
        label = "recent --status-summary --limit 2"
        before = snapshot_state(self.tmp, self.db)
        proc = run_status_summary(self.db, "--limit", "2")
        # 按 id 倒序截取最新两条：id 3（failure）、id 2（success）
        self.assert_status_summary_ok(
            proc,
            {"count": 2, "success_count": 1, "failure_count": 1},
            label,
        )
        self.assert_state_unchanged(before, label)

    def test_reason_timeout_filter_matches_success_record(self):
        label = "recent --status-summary --reason timeout"
        before = snapshot_state(self.tmp, self.db)
        proc = run_status_summary(self.db, "--reason", "timeout")
        # 仅 id 1 的 reason 为 timeout；其保存的 status 是 success，
        # 即使 timeout 通常意味着失败也不得改判
        self.assert_status_summary_ok(
            proc,
            {"count": 1, "success_count": 1, "failure_count": 0},
            label,
        )
        self.assert_state_unchanged(before, label)

    def test_status_failure_and_reason_ok_intersection(self):
        label = "recent --status-summary --status failure --reason ok"
        before = snapshot_state(self.tmp, self.db)
        proc = run_status_summary(
            self.db, "--status", "failure", "--reason", "ok"
        )
        # 仅 id 3 同时满足；其 reason=ok、http_status=200 通常意味着
        # 成功，但保存的 status 是 failure，计数不得改判
        self.assert_status_summary_ok(
            proc,
            {"count": 1, "success_count": 0, "failure_count": 1},
            label,
        )
        self.assert_state_unchanged(before, label)

    def test_status_success_and_reason_ok_intersection_empty(self):
        label = "recent --status-summary --status success --reason ok"
        before = snapshot_state(self.tmp, self.db)
        proc = run_status_summary(
            self.db, "--status", "success", "--reason", "ok"
        )
        # 没有任何记录同时满足：两条 success 的 reason 都不是 ok，
        # 唯一 reason=ok 的记录 status 是 failure
        self.assert_status_summary_ok(
            proc,
            {"count": 0, "success_count": 0, "failure_count": 0},
            label,
        )
        self.assert_state_unchanged(before, label)

    # ---- 与 --summary 互斥：退出码 2、stdout 为空、stderr 说明互斥 ----

    def test_summary_and_status_summary_are_mutually_exclusive(self):
        label = "recent --summary --status-summary（互斥）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_cli(self.db, "recent", "--summary", "--status-summary")
        self.assertEqual(
            proc.returncode, 2,
            f"输入 {label}：期望退出码 2，实际 {proc.returncode}",
        )
        self.assertEqual(
            proc.stdout, "",
            f"输入 {label}：期望 stdout 为空，实际 {proc.stdout!r}",
        )
        self.assertIn(
            "互斥", proc.stderr,
            f"输入 {label}：stderr 应指出两者互斥，实际 {proc.stderr!r}",
        )
        self.assert_state_unchanged(before, label)


if __name__ == "__main__":
    unittest.main(verbosity=2)
