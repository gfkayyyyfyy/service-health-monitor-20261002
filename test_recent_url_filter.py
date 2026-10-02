#!/usr/bin/env python3
"""recent --url 目标筛选功能的独立回归测试。

通过公开命令行入口（子进程运行 healthcheck.py）观察退出码与 JSON 输出，
全部数据放在独立临时 SQLite 数据库中，不依赖固定端口上的服务或已有历史
文件，临时资源在每个用例结束后释放。

在项目目录执行：
    python3 -m unittest test_recent_url_filter
或：
    python3 test_recent_url_filter.py
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

# ---- 固定样本 -------------------------------------------------------------
# 目标 A：带路径与查询参数
URL_A = "http://127.0.0.1:8765/health?detail=1"
# 与 A 仅有一处差异的近似地址
URL_DETAIL2 = "http://127.0.0.1:8765/health?detail=2"
URL_READY = "http://127.0.0.1:8765/ready"
URL_TRAILING_SLASH = "http://127.0.0.1:8765/health/"
# 省略根路径 vs 显式 / ：两个不同的合法原始字符串
URL_BARE_ROOT = "http://127.0.0.1:8765"
URL_EXPLICIT_ROOT = "http://127.0.0.1:8765/"
# 合法但样本中没有任何记录的目标
URL_NO_MATCH = "http://127.0.0.1:8765/no-such-path?x=9"

# id 1/3/5 属于目标 A，含成功与失败；id 2/4/6 分别只改查询值/路径
REC1 = {
    "id": 1, "url": URL_A,
    "checked_at": "2026-10-02T04:40:00.000000+00:00",
    "elapsed_ms": 12, "status": "success", "http_status": 200, "reason": "ok",
}
REC2 = {
    "id": 2, "url": URL_DETAIL2,
    "checked_at": "2026-10-02T04:40:01.000000+00:00",
    "elapsed_ms": 9, "status": "failure", "http_status": 500,
    "reason": "http_status",
}
REC3 = {
    "id": 3, "url": URL_A,
    "checked_at": "2026-10-02T04:40:02.000000+00:00",
    "elapsed_ms": 4, "status": "failure", "http_status": None,
    "reason": "connection_error",
}
REC4 = {
    "id": 4, "url": URL_READY,
    "checked_at": "2026-10-02T04:40:03.000000+00:00",
    "elapsed_ms": 5, "status": "success", "http_status": 200, "reason": "ok",
}
REC5 = {
    "id": 5, "url": URL_A,
    "checked_at": "2026-10-02T04:40:04.000000+00:00",
    "elapsed_ms": 7, "status": "success", "http_status": 204, "reason": "ok",
}
REC6 = {
    "id": 6, "url": URL_TRAILING_SLASH,
    "checked_at": "2026-10-02T04:40:05.000000+00:00",
    "elapsed_ms": 1001, "status": "failure", "http_status": None,
    "reason": "timeout",
}
# 根路径两种写法各一条
REC7 = {
    "id": 7, "url": URL_BARE_ROOT,
    "checked_at": "2026-10-02T04:40:06.000000+00:00",
    "elapsed_ms": 6, "status": "success", "http_status": 200, "reason": "ok",
}
REC8 = {
    "id": 8, "url": URL_EXPLICIT_ROOT,
    "checked_at": "2026-10-02T04:40:07.000000+00:00",
    "elapsed_ms": 8, "status": "success", "http_status": 200, "reason": "ok",
}

ALL_RECORDS = [REC1, REC2, REC3, REC4, REC5, REC6, REC7, REC8]


def run_cli(db_path, *extra):
    """以子进程运行 healthcheck.py 命令行，返回 CompletedProcess。"""
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


def run_recent(db_path, *extra):
    return run_cli(db_path, "recent", *extra)


def build_sample_db(path):
    """创建包含固定样本 8 条记录的临时数据库。"""
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


class RecentUrlFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-urlfilter-"))
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

    # ---- 目标 A 筛选：默认返回 id 5、3、1 ----

    def test_filter_target_a_returns_exact_three_records(self):
        label = f"recent --url {URL_A!r}"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--url", URL_A)
        self.assertEqual(
            proc.returncode, 0,
            f"输入 {label}：期望退出码 0，实际 {proc.returncode}，"
            f"stderr={proc.stderr!r}",
        )
        self.assertEqual(
            proc.stderr, "",
            f"输入 {label}：期望 stderr 为空，实际 {proc.stderr!r}",
        )
        records = json.loads(proc.stdout)
        # 完整记录逐值相等，且按 id 倒序
        self.assertEqual(
            records, [REC5, REC3, REC1],
            f"输入 {label}：期望返回 id [5, 3, 1] 的完整记录，"
            f"实际 {json.dumps(records, ensure_ascii=False)}",
        )
        # 结果中同时包含成功与失败记录，退出码仍为 0
        self.assertEqual(
            {r["status"] for r in records}, {"success", "failure"},
            f"输入 {label}：样本应同时含成功与失败记录",
        )
        self.assert_state_unchanged(before, label)

    def test_limit_applies_after_filtering(self):
        label = f"recent --url {URL_A!r} --limit 2"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--url", URL_A, "--limit", "2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        records = json.loads(proc.stdout)
        # 先筛选再取前 2 条：5、3；id 1 被条数限制排除
        self.assertEqual(
            records, [REC5, REC3],
            f"输入 {label}：期望筛选后再限制为 id [5, 3]，"
            f"实际 id {[r['id'] for r in records]}，"
            f"完整输出={json.dumps(records, ensure_ascii=False)}",
        )
        self.assert_state_unchanged(before, label)

    # ---- 近似地址不得混入 A 的结果 ----

    def test_similar_urls_not_matched(self):
        # 三个近似地址各自只能查到自己的记录，A 的记录不得混入
        cases = [
            (URL_DETAIL2, REC2),
            (URL_READY, REC4),
            (URL_TRAILING_SLASH, REC6),
        ]
        for url, expected in cases:
            with self.subTest(url=url):
                label = f"recent --url {url!r}"
                before = snapshot_state(self.tmp, self.db)
                proc = run_recent(self.db, "--url", url)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")
                records = json.loads(proc.stdout)
                self.assertEqual(
                    records, [expected],
                    f"输入 {label}：期望仅返回 {expected['id']} 号记录，"
                    f"实际返回 id {[r['id'] for r in records]}，"
                    f"应按原始字符串精确匹配，不得规范化合并",
                )
                self.assert_state_unchanged(before, label)

    # ---- 省略根路径 vs 显式 / ：不规范化、不合并 ----

    def test_bare_root_and_explicit_slash_are_distinct(self):
        for url, expected, other_id in (
            (URL_BARE_ROOT, REC7, 8),
            (URL_EXPLICIT_ROOT, REC8, 7),
        ):
            with self.subTest(url=url):
                label = f"recent --url {url!r}"
                before = snapshot_state(self.tmp, self.db)
                proc = run_recent(self.db, "--url", url)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")
                records = json.loads(proc.stdout)
                self.assertEqual(
                    records, [expected],
                    f"输入 {label}：期望只返回原始字符串完全相等的"
                    f" id {expected['id']} 记录；id {other_id} 不应被"
                    f"规范化后合并进来，实际 id {[r['id'] for r in records]}",
                )
                self.assert_state_unchanged(before, label)

    # ---- 不带 --url：原有行为，默认条数、全部目标 ----

    def test_recent_without_url_keeps_default_behavior(self):
        label = "recent（不带 --url）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        records = json.loads(proc.stdout)
        # 默认 limit 5，跨全部目标按 id 倒序
        self.assertEqual(
            records, [REC8, REC7, REC6, REC5, REC4],
            f"输入 {label}：期望默认返回全部目标中最新 5 条 "
            f"id [8,7,6,5,4]，实际 id {[r['id'] for r in records]}",
        )
        # 记录字段完整
        self.assertTrue(
            all(set(r.keys()) == set(COLUMNS) for r in records),
            f"输入 {label}：返回记录字段集合应为 {set(COLUMNS)}",
        )
        self.assert_state_unchanged(before, label)

        # 显式 limit 在不带筛选时同样生效
        proc2 = run_recent(self.db, "--limit", "2")
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertEqual(
            [r["id"] for r in json.loads(proc2.stdout)], [8, 7],
            f"输入 {label} --limit 2：期望 id [8, 7]",
        )

    # ---- 合法目标 / 空库 / 缺库：输出 []，退出码 0，不创建任何东西 ----

    def test_valid_url_without_matching_records_returns_empty(self):
        label = f"recent --url {URL_NO_MATCH!r}（无匹配记录）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--url", URL_NO_MATCH)
        self.assertEqual(
            proc.returncode, 0,
            f"输入 {label}：期望退出码 0，实际 {proc.returncode}",
        )
        self.assertEqual(
            proc.stdout, "[]\n",
            f"输入 {label}：期望输出 []，实际 {proc.stdout!r}",
        )
        self.assertEqual(proc.stderr, "")
        self.assert_state_unchanged(before, label)

    def test_missing_database_and_parent_returns_empty_creates_nothing(self):
        missing_root = self.tmp / "does-not-exist"
        db = missing_root / "nested" / "monitor.sqlite"
        label = f"recent --url {URL_A!r}（数据库与父目录均不存在）"
        before = snapshot_state(self.tmp, db)
        proc = run_recent(db, "--url", URL_A)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "[]\n")
        self.assertEqual(proc.stderr, "")
        # 不得创建目录或数据库文件
        self.assertFalse(
            missing_root.exists(),
            f"输入 {label}：查询不应创建缺失的目录 {missing_root}",
        )
        self.assert_state_unchanged(before, label, db)

    def test_database_with_only_other_table_returns_empty(self):
        db = self.tmp / "other-only.sqlite"
        make_other_table_db(db)
        label = f"recent --url {URL_A!r}（仅有其他表）"
        before = snapshot_state(self.tmp, db)
        proc = run_recent(db, "--url", URL_A)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "[]\n")
        self.assertEqual(proc.stderr, "")
        # 不新建 checks 表，其他表结构与数据原样保留
        after = snapshot_state(self.tmp, db)
        self.assertEqual(
            set(after[1]), {"other_t"},
            f"输入 {label}：不应创建 checks 表，实际表 {set(after[1])}",
        )
        self.assertEqual(after, before, f"输入 {label}：库内容被修改")
        self.assertEqual(
            after[0], before[0], f"输入 {label}：目录文件集合发生变化"
        )

    # ---- 非法筛选值：退出码 2、stdout 空、stderr 说明 URL 原因 ----

    def assert_url_rejected(self, proc, url, reason_keyword):
        label = f"recent --db <db> recent --url {url!r}"
        self.assertEqual(
            proc.returncode, 2,
            f"输入 {label}：期望退出码 2，实际 {proc.returncode}，"
            f"stdout={proc.stdout!r}",
        )
        self.assertEqual(
            proc.stdout, "",
            f"输入 {label}：期望 stdout 为空，实际 {proc.stdout!r}",
        )
        self.assertTrue(
            proc.stderr.startswith("healthcheck: error:"),
            f"输入 {label}：stderr 应以固定错误前缀开头，"
            f"实际 {proc.stderr!r}",
        )
        self.assertIn(
            reason_keyword, proc.stderr,
            f"输入 {label}：stderr 应说明 URL 原因（含 {reason_keyword!r}），"
            f"实际 {proc.stderr!r}",
        )
        # 错误信息必须回显引发问题的输入值，且无 Python 回溯
        self.assertIn(
            url, proc.stderr,
            f"输入 {label}：stderr 应回显非法输入 {url!r}，"
            f"实际 {proc.stderr!r}",
        )
        self.assertNotIn(
            "Traceback", proc.stderr,
            f"输入 {label}：stderr 不应包含 Python 回溯，"
            f"实际 {proc.stderr!r}",
        )

    def test_invalid_filter_urls_exit_2(self):
        cases = [
            # https 地址：协议不允许
            ("https://127.0.0.1:8765/health?detail=1", "协议",
             self.tmp / "https.sqlite"),
            # 缺少端口
            ("http://127.0.0.1/health", "端口",
             self.tmp / "noport.sqlite"),
        ]
        for url, keyword, db in cases:
            with self.subTest(url=url):
                # 数据库文件刻意不存在（父目录存在）：URL 错误必须优先
                before = snapshot_state(self.tmp, db)
                proc = run_recent(db, "--url", url)
                self.assert_url_rejected(proc, url, keyword)
                self.assert_state_unchanged(before, f"非法 URL {url!r}", db)
                self.assertFalse(
                    db.exists(),
                    f"非法 URL 查询不应创建数据库文件 {db}",
                )

        # 未闭合方括号 + 目录形式数据库路径：必须优先报告 URL 错误
        bad_bracket = "http://[127.0.0.1:8765/health?detail=1"
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir()
        with self.subTest(url=bad_bracket):
            before = snapshot_state(self.tmp, db_dir)
            proc = run_recent(db_dir, "--url", bad_bracket)
            self.assert_url_rejected(proc, bad_bracket, "URL")
            self.assertNotIn(
                "目录", proc.stderr,
                f"输入未闭合方括号且数据库路径是目录时应优先报 URL 错误，"
                f"却报告了目录错误：{proc.stderr!r}",
            )
            self.assert_state_unchanged(
                before, f"非法 URL {bad_bracket!r}（目录路径）", db_dir
            )

        # 换成合法 URL 后，同一目录路径才报数据库路径错误
        with self.subTest(url=URL_A, db="directory"):
            before = snapshot_state(self.tmp, db_dir)
            proc = run_recent(db_dir, "--url", URL_A)
            self.assertEqual(
                proc.returncode, 2,
                f"输入合法 URL、目录路径：期望退出码 2，"
                f"实际 {proc.returncode}",
            )
            self.assertEqual(proc.stdout, "")
            self.assertIn(
                "目录", proc.stderr,
                f"合法 URL 配合目录路径时应报告数据库路径（目录）错误，"
                f"实际 stderr={proc.stderr!r}",
            )
            self.assertNotIn("Traceback", proc.stderr)
            self.assert_state_unchanged(
                before, f"合法 URL {URL_A!r}（目录路径）", db_dir
            )

    # ---- 数据库结构与记录字段兼容 ----

    def test_schema_and_fields_remain_compatible(self):
        conn = sqlite3.connect(str(self.db))
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(checks)")]
        finally:
            conn.close()
        self.assertEqual(
            cols, COLUMNS,
            f"checks 表列结构应保持兼容：{COLUMNS}，实际 {cols}",
        )
        proc = run_recent(self.db, "--url", URL_A)
        records = json.loads(proc.stdout)
        for rec in records:
            self.assertEqual(
                set(rec.keys()), set(COLUMNS),
                f"返回记录字段应恰为 {COLUMNS}，实际 {list(rec.keys())}",
            )

    # ---- 经公开 check 入口产生的数据同样可筛选（临时端口，无固定依赖）----

    def test_filter_works_with_records_produced_by_check(self):
        db = self.tmp / "from-check.sqlite"
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler
        )
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            slash_url = f"http://127.0.0.1:{port}/"
            proc = run_cli(db, "check", "--url", slash_url)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            written = json.loads(proc.stdout)
            self.assertEqual(written["url"], slash_url)
            self.assertEqual(written["status"], "success")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        # 显式 / 的地址能查到记录
        proc = run_recent(db, "--url", slash_url)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = json.loads(proc.stdout)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["url"], slash_url)
        self.assertEqual(records[0]["status"], "success")
        self.assertEqual(records[0]["http_status"], 200)
        self.assertEqual(records[0]["reason"], "ok")

        # 省略根路径的写法是不同的原始字符串，查不到
        proc_bare = run_recent(db, "--url", f"http://127.0.0.1:{port}")
        self.assertEqual(proc_bare.returncode, 0, proc_bare.stderr)
        self.assertEqual(proc_bare.stdout, "[]\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
