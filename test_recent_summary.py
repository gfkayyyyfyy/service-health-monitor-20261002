#!/usr/bin/env python3
"""recent --summary 耗时摘要功能的独立回归测试。

通过公开命令行入口（子进程运行 healthcheck.py）核对退出码与 JSON 输出，
样本数据放在独立临时 SQLite 数据库中，只用 Python 3 标准库，
不依赖真实服务或已有历史文件，临时资源在每个用例结束后释放。
同时核对 check、普通 recent（记录数组）与数据库结构的既有行为保持不变。

在项目目录执行：
    python3 -m unittest test_recent_summary
或：
    python3 test_recent_summary.py
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

SUMMARY_KEYS = {"count", "min_elapsed_ms", "max_elapsed_ms", "avg_elapsed_ms"}

# ---- 固定样本 -------------------------------------------------------------
# 目标 A 与目标 B 仅查询参数 detail 的值不同
URL_A = "http://127.0.0.1:8765/health?detail=1"
URL_B = "http://127.0.0.1:8765/health?detail=2"
# 合法但样本中没有任何记录的目标
URL_NO_MATCH = "http://127.0.0.1:8765/no-such-path?x=9"

# id 1..7 的目标依次为 A、B、A、B、A、A、B；
# elapsed_ms 依次为 90、80、0、70、2、7、60；
# A 的零耗时（id 3）与七毫秒（id 6）记录为 failure，其余为 success；
# checked_at 的先后顺序与 id 相反（id 1 最新，id 7 最早）。
REC1 = {
    "id": 1, "url": URL_A,
    "checked_at": "2026-10-02T04:40:06.000000+00:00",
    "elapsed_ms": 90, "status": "success", "http_status": 200, "reason": "ok",
}
REC2 = {
    "id": 2, "url": URL_B,
    "checked_at": "2026-10-02T04:40:05.000000+00:00",
    "elapsed_ms": 80, "status": "success", "http_status": 200, "reason": "ok",
}
REC3 = {
    "id": 3, "url": URL_A,
    "checked_at": "2026-10-02T04:40:04.000000+00:00",
    "elapsed_ms": 0, "status": "failure", "http_status": None,
    "reason": "connection_error",
}
REC4 = {
    "id": 4, "url": URL_B,
    "checked_at": "2026-10-02T04:40:03.000000+00:00",
    "elapsed_ms": 70, "status": "success", "http_status": 200, "reason": "ok",
}
REC5 = {
    "id": 5, "url": URL_A,
    "checked_at": "2026-10-02T04:40:02.000000+00:00",
    "elapsed_ms": 2, "status": "success", "http_status": 200, "reason": "ok",
}
REC6 = {
    "id": 6, "url": URL_A,
    "checked_at": "2026-10-02T04:40:01.000000+00:00",
    "elapsed_ms": 7, "status": "failure", "http_status": 500,
    "reason": "http_status",
}
REC7 = {
    "id": 7, "url": URL_B,
    "checked_at": "2026-10-02T04:40:00.000000+00:00",
    "elapsed_ms": 60, "status": "success", "http_status": 200, "reason": "ok",
}

ALL_RECORDS = [REC1, REC2, REC3, REC4, REC5, REC6, REC7]

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


def run_summary(db_path, *extra):
    return run_cli(db_path, "recent", "--summary", *extra)


def run_recent(db_path, *extra):
    return run_cli(db_path, "recent", *extra)


def build_sample_db(path):
    """创建包含固定样本 7 条记录的临时数据库。"""
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


def make_empty_checks_db(path):
    """有效数据库，checks 表结构完整但没有任何记录。"""
    conn = sqlite3.connect(str(path))
    conn.execute(SCHEMA_SQL)
    conn.commit()
    conn.close()


def make_other_table_db(path):
    """有效数据库，但只有与 checks 无关的表。"""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE other_t (name TEXT NOT NULL, n INTEGER NOT NULL)")
    conn.execute("INSERT INTO other_t (name, n) VALUES ('kept', 42)")
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


class RecentSummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-summary-"))
        self.db = self.tmp / "monitor.sqlite"
        build_sample_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_state_unchanged(self, before, label, db_path=None):
        """摘要查询不得改变目录文件集合、表结构或任何已有记录。"""
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

    def assert_summary_ok(self, proc, expected, label):
        """退出码 0、stderr 为空、单行 JSON 对象且恰含四个摘要字段。"""
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
            f"输入 {label}：摘要应为一行 JSON 对象，实际 {proc.stdout!r}",
        )
        summary = json.loads(proc.stdout)
        self.assertEqual(
            set(summary.keys()), SUMMARY_KEYS,
            f"输入 {label}：摘要应仅含字段 {SUMMARY_KEYS}，"
            f"实际 {set(summary.keys())}",
        )
        self.assertEqual(
            summary, expected,
            f"输入 {label}：期望摘要 {expected}，实际 {summary}",
        )
        return summary

    # ---- 目标 A 摘要：失败与零耗时记录同样参与统计 ----

    def test_summary_target_a_limit_2(self):
        label = f"recent --summary --url {URL_A!r} --limit 2"
        before = snapshot_state(self.tmp, self.db)
        proc = run_summary(self.db, "--url", URL_A, "--limit", "2")
        # 最新两条 A 记录为 id 6（7ms，failure）与 id 5（2ms，success）
        self.assert_summary_ok(
            proc,
            {"count": 2, "min_elapsed_ms": 2,
             "max_elapsed_ms": 7, "avg_elapsed_ms": 4.5},
            label,
        )
        self.assert_state_unchanged(before, label)

    def test_summary_target_a_limit_3_includes_failure_and_zero(self):
        label = f"recent --summary --url {URL_A!r} --limit 3"
        before = snapshot_state(self.tmp, self.db)
        proc = run_summary(self.db, "--url", URL_A, "--limit", "3")
        # id 6（7ms，failure）、id 5（2ms）、id 3（0ms，failure）：
        # 失败记录与零耗时记录都计入 count 与 min
        summary = self.assert_summary_ok(
            proc,
            {"count": 3, "min_elapsed_ms": 0,
             "max_elapsed_ms": 7, "avg_elapsed_ms": 3},
            label,
        )
        self.assertEqual(
            summary["count"], 3,
            f"输入 {label}：两条 failure 记录（含零耗时）必须参与统计",
        )
        self.assert_state_unchanged(before, label)

    def test_summary_all_targets_default_limit(self):
        label = "recent --summary（省略 URL 与 limit）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_summary(self.db)
        # 最新五条为 id 7..3：60、7、2、70、0
        self.assert_summary_ok(
            proc,
            {"count": 5, "min_elapsed_ms": 0,
             "max_elapsed_ms": 70, "avg_elapsed_ms": 27.8},
            label,
        )
        self.assert_state_unchanged(before, label)

    # ---- 相同条件去掉 --summary：仍返回对应记录数组 ----

    def test_same_conditions_without_summary_return_record_arrays(self):
        cases = [
            (("--url", URL_A, "--limit", "2"), [REC6, REC5]),
            (("--url", URL_A, "--limit", "3"), [REC6, REC5, REC3]),
            ((), [REC7, REC6, REC5, REC4, REC3]),
        ]
        for extra, expected in cases:
            with self.subTest(extra=extra):
                label = f"recent {' '.join(extra)}（不带 --summary）"
                before = snapshot_state(self.tmp, self.db)
                proc = run_recent(self.db, *extra)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")
                records = json.loads(proc.stdout)
                self.assertEqual(
                    records, expected,
                    f"输入 {label}：期望记录 id "
                    f"{[r['id'] for r in expected]}，实际 id "
                    f"{[r['id'] for r in records]}",
                )
                self.assert_state_unchanged(before, label)

    # ---- 四种空结果：count 为 0、三个耗时字段为 null、退出码 0 ----

    def assert_empty_summary(self, proc, label):
        self.assert_summary_ok(proc, EMPTY_SUMMARY, label)

    def test_valid_url_without_matching_records(self):
        label = f"recent --summary --url {URL_NO_MATCH!r}（无匹配记录）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_summary(self.db, "--url", URL_NO_MATCH)
        self.assert_empty_summary(proc, label)
        self.assert_state_unchanged(before, label)

    def test_empty_checks_table(self):
        db = self.tmp / "empty.sqlite"
        make_empty_checks_db(db)
        label = f"recent --summary --url {URL_A!r}（空 checks 表）"
        before = snapshot_state(self.tmp, db)
        proc = run_summary(db, "--url", URL_A)
        self.assert_empty_summary(proc, label)
        self.assert_state_unchanged(before, label, db)

    def test_database_with_only_other_tables(self):
        db = self.tmp / "other-only.sqlite"
        make_other_table_db(db)
        label = f"recent --summary --url {URL_A!r}（只有其他表）"
        before = snapshot_state(self.tmp, db)
        proc = run_summary(db, "--url", URL_A)
        self.assert_empty_summary(proc, label)
        after = snapshot_state(self.tmp, db)
        self.assertEqual(
            set(after[1]), {"other_t"},
            f"输入 {label}：不应创建 checks 表，实际表 {set(after[1])}",
        )
        self.assertEqual(after, before, f"输入 {label}：库内容被修改")

    def test_missing_database_and_parent(self):
        missing_root = self.tmp / "does-not-exist"
        db = missing_root / "nested" / "monitor.sqlite"
        label = f"recent --summary --url {URL_A!r}（数据库与父目录均不存在）"
        before = snapshot_state(self.tmp, db)
        proc = run_summary(db, "--url", URL_A)
        self.assert_empty_summary(proc, label)
        self.assertFalse(
            missing_root.exists(),
            f"输入 {label}：查询不应创建缺失的目录 {missing_root}",
        )
        self.assertFalse(
            db.exists(),
            f"输入 {label}：查询不应创建数据库文件 {db}",
        )
        self.assert_state_unchanged(before, label, db)

    # ---- 摘要查询不发网络请求 ----

    def test_summary_makes_no_network_requests(self):
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
            for extra in (("--url", URL_A), ("--url", URL_B), ()):
                proc = run_summary(self.db, *extra)
                self.assertEqual(proc.returncode, 0, proc.stderr)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(
            hits, [],
            f"摘要查询不应发出任何网络请求，实际收到 {hits}",
        )

    # ---- 参数与路径错误：退出码 2、stdout 为空、stderr 说明原因 ----

    def assert_rejected(self, proc, label, keyword):
        self.assertEqual(
            proc.returncode, 2,
            f"输入 {label}：期望退出码 2，实际 {proc.returncode}，"
            f"stdout={proc.stdout!r}",
        )
        self.assertEqual(
            proc.stdout, "",
            f"输入 {label}：期望 stdout 为空，实际 {proc.stdout!r}",
        )
        self.assertIn(
            keyword, proc.stderr,
            f"输入 {label}：stderr 应说明原因（含 {keyword!r}），"
            f"实际 {proc.stderr!r}",
        )
        self.assertNotIn(
            "Traceback", proc.stderr,
            f"输入 {label}：stderr 不应包含 Python 回溯，"
            f"实际 {proc.stderr!r}",
        )

    def test_limit_zero_rejected(self):
        label = f"recent --summary --url {URL_A!r} --limit 0"
        before = snapshot_state(self.tmp, self.db)
        proc = run_summary(self.db, "--url", URL_A, "--limit", "0")
        self.assert_rejected(proc, label, "limit")
        self.assert_state_unchanged(before, label)

    def test_https_url_rejected(self):
        https_url = "https://127.0.0.1:8765/health?detail=1"
        label = f"recent --summary --url {https_url!r}"
        before = snapshot_state(self.tmp, self.db)
        proc = run_summary(self.db, "--url", https_url)
        self.assert_rejected(proc, label, "协议")
        self.assertIn(
            https_url, proc.stderr,
            f"输入 {label}：stderr 应回显非法输入，实际 {proc.stderr!r}",
        )
        self.assert_state_unchanged(before, label)

    def test_directory_db_path_rejected(self):
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir()
        label = f"recent --summary --url {URL_A!r}（数据库路径为目录）"
        before = snapshot_state(self.tmp, db_dir)
        proc = run_summary(db_dir, "--url", URL_A)
        self.assert_rejected(proc, label, "目录")
        self.assert_state_unchanged(before, label, db_dir)

    def test_invalid_url_and_directory_reports_url_first(self):
        bad_url = "http://[127.0.0.1:8765/health?detail=1"
        db_dir = self.tmp / "another-directory"
        db_dir.mkdir()
        label = f"recent --summary --url {bad_url!r}（数据库路径为目录）"
        before = snapshot_state(self.tmp, db_dir)
        proc = run_summary(db_dir, "--url", bad_url)
        self.assert_rejected(proc, label, "URL")
        self.assertNotIn(
            "目录", proc.stderr,
            f"输入 {label}：非法 URL 与目录路径同时出现时应优先报告 "
            f"URL 错误，实际 {proc.stderr!r}",
        )
        self.assert_state_unchanged(before, label, db_dir)

    # ---- 既有行为保持：check 入口、普通 recent、数据库结构 ----

    def test_schema_remains_compatible(self):
        conn = sqlite3.connect(str(self.db))
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(checks)")]
        finally:
            conn.close()
        self.assertEqual(
            cols, COLUMNS,
            f"checks 表列结构应保持兼容：{COLUMNS}，实际 {cols}",
        )
        # 摘要查询之后结构不变
        proc = run_summary(self.db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        conn = sqlite3.connect(str(self.db))
        try:
            cols_after = [
                row[1] for row in conn.execute("PRAGMA table_info(checks)")
            ]
        finally:
            conn.close()
        self.assertEqual(cols_after, COLUMNS)

    def test_check_entry_and_plain_recent_unchanged(self):
        """check 入口产生的记录，普通 recent 与 --summary 都能正确读取。"""
        db = self.tmp / "from-check.sqlite"
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler
        )
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{port}/"
            proc = run_cli(db, "check", "--url", url)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            written = json.loads(proc.stdout)
            self.assertEqual(written["url"], url)
            self.assertEqual(written["status"], "success")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        # 普通 recent 返回完整记录数组
        proc = run_recent(db, "--url", url)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = json.loads(proc.stdout)
        self.assertEqual(len(records), 1)
        self.assertEqual(set(records[0].keys()), set(COLUMNS))
        self.assertEqual(records[0]["url"], url)
        self.assertEqual(records[0]["status"], "success")
        self.assertEqual(records[0]["http_status"], 200)
        self.assertEqual(records[0]["reason"], "ok")

        # 同一批记录的摘要与记录内容一致
        proc = run_summary(db, "--url", url)
        self.assert_summary_ok(
            proc,
            {"count": 1,
             "min_elapsed_ms": records[0]["elapsed_ms"],
             "max_elapsed_ms": records[0]["elapsed_ms"],
             "avg_elapsed_ms": records[0]["elapsed_ms"]},
            f"recent --summary --url {url!r}（check 产生的数据）",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
