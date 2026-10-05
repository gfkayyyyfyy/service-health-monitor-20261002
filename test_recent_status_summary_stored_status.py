#!/usr/bin/env python3
"""recent --status-summary 只按保存的 status 计数的回归测试。

样本记录满足 checks 表全部约束，但 status、reason、http_status 的字段
含义相互不一致（如 status 为 success 而 reason 为 timeout），以此确认
状态摘要只依据保存的 status 字段分类，绝不从 reason 或 http_status
重新推断成功或失败。

通过公开命令行入口（子进程运行 healthcheck.py）核对退出码与 JSON 输出，
每个用例使用独立临时 SQLite 数据库，只用 Python 3 标准库，不依赖真实
服务或已有历史文件，临时资源在每个用例结束后释放。同时核对普通 recent
（记录数组）与 --summary 耗时摘要对同一样本的既有行为保持不变。

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
URL = "http://127.0.0.1:8765/health"
CHECKED_AT = "2026-10-05T00:00:00Z"

# 三条记录共用同一 url、checked_at 与零耗时；status、reason、http_status
# 的字段含义相互不一致（均满足表约束），用于确认状态摘要不会从 reason
# 或 http_status 重新推断成功或失败：
#   id 1：status=success，但 reason=timeout、http_status 为 null
#   id 2：status=success，但 reason=http_status、http_status=500
#   id 3：status=failure，但 reason=ok、http_status=200
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
                    f"SELECT * FROM {name}"
                ).fetchall()
        finally:
            conn.close()
    return files, tables


class RecentStatusSummaryStoredStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-stored-status-"))
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
            f"输入 {label}：查询前后数据库表或记录内容发生变化",
        )

    def assert_status_summary_ok(self, proc, expected, label):
        """退出码 0、stderr 为空、单行 JSON 对象且恰含三个计数字段。"""
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

    # ---- 只按保存的 status 计数，不从 reason/http_status 推断 ----

    def test_counts_by_stored_status_only(self):
        label = "recent --status-summary（字段含义不一致的样本）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_status_summary(self.db)
        # 保存的 status 为 success、success、failure：
        # 尽管 id 1 的 reason 是 timeout、id 2 的 http_status 是 500、
        # id 3 的 reason 是 ok 且 http_status 是 200，也不重新推断
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
        # 按 id 倒序截取最新两条：id 3（failure）与 id 2（success）
        self.assert_status_summary_ok(
            proc,
            {"count": 2, "success_count": 1, "failure_count": 1},
            label,
        )
        self.assert_state_unchanged(before, label)

    def test_reason_filter_timeout(self):
        label = "recent --status-summary --reason timeout"
        before = snapshot_state(self.tmp, self.db)
        proc = run_status_summary(self.db, "--reason", "timeout")
        # 仅 id 1 的 reason 为 timeout；其保存的 status 是 success
        self.assert_status_summary_ok(
            proc,
            {"count": 1, "success_count": 1, "failure_count": 0},
            label,
        )
        self.assert_state_unchanged(before, label)

    def test_status_failure_and_reason_ok(self):
        label = "recent --status-summary --status failure --reason ok"
        before = snapshot_state(self.tmp, self.db)
        proc = run_status_summary(
            self.db, "--status", "failure", "--reason", "ok"
        )
        # 仅 id 3 同时满足；其保存的 status 是 failure，
        # 不因 reason=ok、http_status=200 改记为成功
        self.assert_status_summary_ok(
            proc,
            {"count": 1, "success_count": 0, "failure_count": 1},
            label,
        )
        self.assert_state_unchanged(before, label)

    def test_status_success_and_reason_ok_no_match(self):
        label = "recent --status-summary --status success --reason ok"
        before = snapshot_state(self.tmp, self.db)
        proc = run_status_summary(
            self.db, "--status", "success", "--reason", "ok"
        )
        # 没有记录同时满足 status=success 且 reason=ok
        self.assert_status_summary_ok(
            proc,
            {"count": 0, "success_count": 0, "failure_count": 0},
            label,
        )
        self.assert_state_unchanged(before, label)

    # ---- 与 --summary 互斥：退出码 2、stdout 为空、stderr 指出互斥 ----

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
        self.assertNotIn(
            "Traceback", proc.stderr,
            f"输入 {label}：stderr 不应包含 Python 回溯，实际 {proc.stderr!r}",
        )
        self.assert_state_unchanged(before, label)

    # ---- 既有行为保持：普通 recent 与 --summary 输出不变 ----

    def test_plain_recent_and_summary_unchanged(self):
        label = "普通 recent 与 --summary（回归）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_cli(self.db, "recent")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        records = json.loads(proc.stdout)
        # 记录按 id 倒序原样返回，不一致的字段组合原样保留
        self.assertEqual(
            records,
            [
                {"id": r["id"], "url": r["url"],
                 "checked_at": r["checked_at"],
                 "elapsed_ms": r["elapsed_ms"], "status": r["status"],
                 "http_status": r["http_status"], "reason": r["reason"]}
                for r in (REC3, REC2, REC1)
            ],
            f"输入 {label}：普通 recent 应原样返回保存的记录，实际 {records}",
        )

        proc = run_cli(self.db, "recent", "--summary")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        summary = json.loads(proc.stdout)
        self.assertEqual(
            summary,
            {"count": 3, "min_elapsed_ms": 0,
             "max_elapsed_ms": 0, "avg_elapsed_ms": 0.0},
            f"输入 {label}：--summary 耗时摘要应保持原样，实际 {summary}",
        )
        self.assert_state_unchanged(before, label)


if __name__ == "__main__":
    unittest.main(verbosity=2)
