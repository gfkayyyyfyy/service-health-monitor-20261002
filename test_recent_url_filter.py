#!/usr/bin/env python3
"""healthcheck.py recent --url 目标筛选功能的独立回归测试。

仅通过公开命令行入口观察退出码与 JSON 输出；所有数据放在独立临时目录的
SQLite 文件中，直接构造样本，不监听固定端口、不依赖已有历史文件、不发网络请求。

运行：
    python3 -m unittest test_recent_url_filter
或：
    python3 test_recent_url_filter.py
"""

import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "healthcheck.py"

# ---- 固定样本 -------------------------------------------------------------
#
# 目标 A（三条，id 1/3/5，含成功与失败）：
A_URL = "http://127.0.0.1:8765/health?detail=1"
# 干扰项：id 2 把 detail 改为 2；id 4 改路径 /ready；id 6 改路径 /health/
URL_DETAIL2 = "http://127.0.0.1:8765/health?detail=2"
URL_READY = "http://127.0.0.1:8765/ready"
URL_HEALTH_SLASH = "http://127.0.0.1:8765/health/"
# 另两个合法地址：省略根路径 vs 显式带 /，二者不得被规范化后合并
URL_BARE = "http://127.0.0.1:8765"
URL_ROOT = "http://127.0.0.1:8765/"

# 插入顺序刻意与 id 顺序交错，证明查询依赖 ORDER BY id DESC 而非插入顺序。
SAMPLE = [
    {
        "id": 1, "url": A_URL,
        "checked_at": "2026-10-02T04:40:00.000000+00:00",
        "elapsed_ms": 3, "status": "success", "http_status": 200,
        "reason": "ok",
    },
    {
        "id": 2, "url": URL_DETAIL2,
        "checked_at": "2026-10-02T04:40:01.000000+00:00",
        "elapsed_ms": 4, "status": "success", "http_status": 200,
        "reason": "ok",
    },
    {
        "id": 3, "url": A_URL,
        "checked_at": "2026-10-02T04:40:02.000000+00:00",
        "elapsed_ms": 2, "status": "failure", "http_status": 503,
        "reason": "http_status",
    },
    {
        "id": 4, "url": URL_READY,
        "checked_at": "2026-10-02T04:40:03.000000+00:00",
        "elapsed_ms": 5, "status": "success", "http_status": 200,
        "reason": "ok",
    },
    {
        "id": 5, "url": A_URL,
        "checked_at": "2026-10-02T04:40:04.000000+00:00",
        "elapsed_ms": 1, "status": "failure", "http_status": None,
        "reason": "connection_error",
    },
    {
        "id": 6, "url": URL_HEALTH_SLASH,
        "checked_at": "2026-10-02T04:40:05.000000+00:00",
        "elapsed_ms": 6, "status": "success", "http_status": 200,
        "reason": "ok",
    },
    {
        "id": 7, "url": URL_BARE,
        "checked_at": "2026-10-02T04:40:06.000000+00:00",
        "elapsed_ms": 7, "status": "success", "http_status": 204,
        "reason": "ok",
    },
    {
        "id": 8, "url": URL_ROOT,
        "checked_at": "2026-10-02T04:40:07.000000+00:00",
        "elapsed_ms": 8, "status": "failure", "http_status": None,
        "reason": "timeout",
    },
]
SAMPLE_BY_ID = {r["id"]: r for r in SAMPLE}
A_RECORDS_DESC = [SAMPLE_BY_ID[5], SAMPLE_BY_ID[3], SAMPLE_BY_ID[1]]

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

INSERT_SQL = (
    "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
    "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)"
)


def run_cli(db_path, *extra):
    """通过公开命令行入口运行 healthcheck.py，返回到的 CompletedProcess。"""
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), "recent", *extra]
    return subprocess.run(cmd, capture_output=True, text=True)


def build_sample_db(path):
    """按 SAMPLE 构造数据库，显式指定 id，并附带一张无关表用于只读校验。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(CREATE_TABLE_SQL)
        conn.execute("CREATE TABLE other_t (name TEXT PRIMARY KEY, n INTEGER)")
        conn.execute("INSERT INTO other_t VALUES ('kept', 42)")
        for rec in SAMPLE:
            conn.execute(
                INSERT_SQL,
                (
                    rec["id"], rec["url"], rec["checked_at"], rec["elapsed_ms"],
                    rec["status"], rec["http_status"], rec["reason"],
                ),
            )
        conn.commit()
    finally:
        conn.close()


def dump_state(path):
    """快照数据库内的全部表与记录（按表名、行排序），用于前后对比。"""
    conn = sqlite3.connect(str(path))
    try:
        tables = sorted(
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        )
        state = {}
        for table in tables:
            cols = [
                c[1] for c in conn.execute(f"PRAGMA table_info({table})")
            ]
            rows = sorted(conn.execute(f"SELECT * FROM {table}").fetchall())
            state[table] = (cols, rows)
        return state
    finally:
        conn.close()


class RecentUrlFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-url-filter-"))
        self.db = self.tmp / "monitor.sqlite"
        build_sample_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- 辅助断言：失败时指出输入与预期的差异 ------------------------------

    def assert_cli_ok_json(self, proc, expected, label):
        self.assertEqual(
            proc.returncode, 0,
            f"{label}: 期望退出码 0，实际 {proc.returncode}；"
            f"stderr={proc.stderr!r}",
        )
        self.assertEqual(
            proc.stderr, "",
            f"{label}: 期望标准错误为空，实际 {proc.stderr!r}",
        )
        try:
            actual = json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.fail(f"{label}: stdout 不是合法 JSON: {proc.stdout!r}")
        self.assertEqual(
            actual, expected,
            f"{label}: JSON 输出与预期不符。\n实际={actual!r}\n预期={expected!r}",
        )

    def assert_state_unchanged(self, path, before, label):
        self.assertTrue(
            path.exists(), f"{label}: 查询后数据库文件消失: {path}"
        )
        after = dump_state(path)
        self.assertEqual(
            after, before,
            f"{label}: 查询改变了数据库内容。\n查询前={before!r}\n查询后={after!r}",
        )

    def assert_dir_files_unchanged(self, directory, before_files, label):
        after_files = {
            str(p.relative_to(directory))
            for p in directory.rglob("*")
            if p.is_file()
        }
        self.assertEqual(
            after_files, before_files,
            f"{label}: 目录内文件集合发生变化。\n"
            f"查询前={sorted(before_files)}\n查询后={sorted(after_files)}",
        )

    def assert_url_error(self, proc, url, label):
        """非法筛选值：退出码 2、stdout 为空、stderr 说明 URL 原因且无回溯。"""
        self.assertEqual(
            proc.returncode, 2,
            f"{label}: url={url!r} 期望退出码 2，实际 {proc.returncode}；"
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        self.assertEqual(
            proc.stdout, "",
            f"{label}: url={url!r} 期望标准输出为空，实际 {proc.stdout!r}",
        )
        self.assertNotEqual(
            proc.stderr, "",
            f"{label}: url={url!r} 期望标准错误说明 URL 原因，实际为空",
        )
        # 说明必须针对该 URL：回显被拒绝的地址，而非数据库/路径错误
        self.assertIn(
            url, proc.stderr,
            f"{label}: url={url!r} 的标准错误未回显并说明该 URL 原因: "
            f"{proc.stderr!r}",
        )
        self.assertNotIn(
            "数据库", proc.stderr,
            f"{label}: url={url!r} 应报告 URL 原因而非数据库错误: "
            f"{proc.stderr!r}",
        )
        self.assertNotIn(
            "Traceback", proc.stderr,
            f"{label}: url={url!r} 的标准错误出现了 Python 回溯:\n{proc.stderr}",
        )

    # ---- 精确筛选：默认返回 A 的全部三条，倒序且逐值相等 ----

    def test_filter_returns_only_target_a_descending(self):
        label = "recent --url A（默认 limit）"
        before = dump_state(self.db)
        files_before = {
            str(p.relative_to(self.tmp))
            for p in self.tmp.rglob("*") if p.is_file()
        }

        proc = run_cli(self.db, "--url", A_URL)
        # id 5、3、1，包含失败记录（5/3）仍退出码 0
        self.assert_cli_ok_json(proc, A_RECORDS_DESC, label)
        # 顺序必须是 5 -> 3 -> 1
        self.assertEqual(
            [r["id"] for r in json.loads(proc.stdout)], [5, 3, 1],
            f"{label}: 记录顺序应为 id 倒序 5,3,1",
        )
        # 每条记录的每个字段逐值等于样本
        for actual, expected in zip(json.loads(proc.stdout), A_RECORDS_DESC):
            self.assertEqual(
                actual, expected,
                f"{label}: 记录字段与样本逐值不符。\n"
                f"实际={actual!r}\n预期={expected!r}",
            )
        # 任何近似地址都不得混入
        self.assertTrue(
            all(r["url"] == A_URL for r in json.loads(proc.stdout)),
            f"{label}: 结果中混入了不同原始 URL 的记录",
        )
        self.assert_state_unchanged(self.db, before, label)
        self.assert_dir_files_unchanged(self.tmp, files_before, label)

    # ---- --limit 作用于筛选之后 ----

    def test_limit_applies_after_filter(self):
        label = "recent --url A --limit 2"
        before = dump_state(self.db)
        proc = run_cli(self.db, "--url", A_URL, "--limit", "2")
        self.assert_cli_ok_json(
            proc, [SAMPLE_BY_ID[5], SAMPLE_BY_ID[3]], label
        )
        self.assertNotIn(
            SAMPLE_BY_ID[1], json.loads(proc.stdout),
            f"{label}: limit 2 不应包含 id=1",
        )
        self.assert_state_unchanged(self.db, before, label)

    # ---- 三个近似地址各自只匹配自身，证明不做规范化合并 ----

    def test_similar_urls_are_distinct_strings(self):
        cases = [
            ("detail=2", URL_DETAIL2, 2),
            ("路径 /ready", URL_READY, 4),
            ("路径 /health/", URL_HEALTH_SLASH, 6),
        ]
        for desc, url, expect_id in cases:
            with self.subTest(case=desc, url=url):
                label = f"近似地址精确匹配 {desc}"
                before = dump_state(self.db)
                proc = run_cli(self.db, "--url", url)
                expected = [SAMPLE_BY_ID[expect_id]]
                self.assert_cli_ok_json(proc, expected, label)
                self.assertEqual(
                    json.loads(proc.stdout)[0]["url"], url,
                    f"{label}: 返回记录的原始 url 与查询值不一致",
                )
                self.assert_state_unchanged(self.db, before, label)

    # ---- 省略根路径 vs 显式 /：两种写法各查各的，不被合并 ----

    def test_bare_host_and_slash_are_not_merged(self):
        for url, expect_id in ((URL_BARE, 7), (URL_ROOT, 8)):
            with self.subTest(url=url):
                label = f"根路径写法区分 {url!r}"
                before = dump_state(self.db)
                proc = run_cli(self.db, "--url", url)
                self.assert_cli_ok_json(
                    proc, [SAMPLE_BY_ID[expect_id]], label
                )
                self.assert_state_unchanged(self.db, before, label)

    # ---- 合法目标但无匹配记录：[] 且不写入 ----

    def test_valid_url_with_no_match_returns_empty(self):
        label = "合法目标无匹配记录"
        url = "http://127.0.0.1:8765/never-probed"
        before = dump_state(self.db)
        files_before = {
            str(p.relative_to(self.tmp))
            for p in self.tmp.rglob("*") if p.is_file()
        }
        proc = run_cli(self.db, "--url", url)
        self.assert_cli_ok_json(proc, [], label)
        self.assert_state_unchanged(self.db, before, label)
        self.assert_dir_files_unchanged(self.tmp, files_before, label)

    # ---- 去掉 --url：保持原有默认行为，跨全部目标按 id 倒序取 5 条 ----

    def test_recent_without_url_keeps_original_default(self):
        label = "recent 不带 --url"
        before = dump_state(self.db)
        proc = run_cli(self.db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = json.loads(proc.stdout)
        # 默认 limit=5，跨全部目标取最新（id 最大）的 5 条
        self.assertEqual(
            [r["id"] for r in records], [8, 7, 6, 5, 4],
            f"{label}: 期望默认返回全部目标中最新 5 条 8,7,6,5,4，"
            f"实际 {[r['id'] for r in records]}",
        )
        # 不经过筛选：包含多个不同目标
        self.assertEqual(
            {r["url"] for r in records},
            {URL_ROOT, URL_BARE, URL_HEALTH_SLASH, A_URL, URL_READY},
            f"{label}: 无筛选结果应覆盖多个不同目标",
        )
        self.assert_state_unchanged(self.db, before, label)

    # ---- 数据库文件及父目录都不存在：[] 且不创建任何目录/文件 ----

    def test_missing_db_and_parent_returns_empty_creates_nothing(self):
        label = "数据库及父目录不存在"
        missing_root = self.tmp / "does-not-exist"
        db = missing_root / "nested" / "monitor.sqlite"
        self.assertFalse(missing_root.exists())

        proc = run_cli(db, "--url", A_URL)
        self.assert_cli_ok_json(proc, [], label)
        self.assertFalse(
            missing_root.exists(),
            f"{label}: 查询创建了本不存在的目录 {missing_root}",
        )

        # 即使筛选值非法，也不创建任何东西（URL 错误优先，退出码 2）
        proc_bad = run_cli(db, "--url", "http://127.0.0.1/no-port")
        self.assertEqual(proc_bad.returncode, 2, proc_bad.stderr)
        self.assertFalse(
            missing_root.exists(),
            f"{label}: 非法 URL 查询也不应创建目录 {missing_root}",
        )

    # ---- 有效数据库但只有其他表：[] 且不新增 checks 表 ----

    def test_valid_db_with_only_other_table_returns_empty(self):
        label = "有效数据库只有其他表"
        db = self.tmp / "other-only.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("CREATE TABLE other_t (name TEXT PRIMARY KEY, n INTEGER)")
            conn.execute("INSERT INTO other_t VALUES ('kept', 42)")
            conn.commit()
        finally:
            conn.close()

        before = dump_state(db)
        files_before = {
            str(p.relative_to(self.tmp))
            for p in self.tmp.rglob("*") if p.is_file()
        }

        proc_ok = run_cli(db, "--url", A_URL)
        self.assert_cli_ok_json(proc_ok, [], label)
        # 不带 --url 同样为空
        proc_all = run_cli(db)
        self.assert_cli_ok_json(proc_all, [], label + "（不带 --url）")

        self.assert_state_unchanged(db, before, label)
        self.assert_dir_files_unchanged(self.tmp, files_before, label)

    # ---- 非法筛选值：退出码 2、stdout 空、stderr 说明 URL 原因、无回溯 ----

    def test_invalid_filter_urls_exit_2(self):
        bad_urls = [
            ("https 协议", "https://127.0.0.1:8765/health?detail=1"),
            ("缺少端口", "http://127.0.0.1/health?detail=1"),
            ("未闭合方括号", "http://[127.0.0.1:8765/health?detail=1"),
        ]
        before = dump_state(self.db)
        files_before = {
            str(p.relative_to(self.tmp))
            for p in self.tmp.rglob("*") if p.is_file()
        }
        for desc, url in bad_urls:
            with self.subTest(case=desc, url=url):
                proc = run_cli(self.db, "--url", url)
                self.assert_url_error(proc, url, f"非法筛选值（{desc}）")
                # 非法查询同样不得改动数据库或目录
                self.assert_state_unchanged(
                    self.db, before, f"非法筛选值（{desc}）"
                )
                self.assert_dir_files_unchanged(
                    self.tmp, files_before, f"非法筛选值（{desc}）"
                )

    # ---- 未闭合方括号 + 目录形式数据库路径：优先报 URL 错误 ----

    def test_bracket_url_error_takes_priority_over_dir_db(self):
        label = "未闭合方括号 + 目录形式数据库路径"
        files_before = {
            str(p.relative_to(self.tmp))
            for p in self.tmp.rglob("*") if p.is_file()
        }
        proc = run_cli(
            self.tmp, "--url", "http://[127.0.0.1:8765/health?detail=1"
        )
        self.assert_url_error(
            proc, "http://[127.0.0.1:8765/health?detail=1", label
        )
        self.assertNotIn(
            "目录", proc.stderr,
            f"{label}: 应优先报告 URL 错误而非数据库目录错误: {proc.stderr!r}",
        )
        self.assert_dir_files_unchanged(self.tmp, files_before, label)

    # ---- 换成合法 URL 后：报告数据库路径（目录）错误，退出码 2、stdout 空 ----

    def test_legal_url_reports_directory_db_error(self):
        label = "合法 URL + 目录形式数据库路径"
        before = dump_state(self.db)
        files_before = {
            str(p.relative_to(self.tmp))
            for p in self.tmp.rglob("*") if p.is_file()
        }
        proc = run_cli(self.tmp, "--url", A_URL)
        self.assertEqual(
            proc.returncode, 2,
            f"{label}: 期望退出码 2，实际 {proc.returncode}；"
            f"stdout={proc.stdout!r}",
        )
        self.assertEqual(
            proc.stdout, "",
            f"{label}: 期望标准输出为空，实际 {proc.stdout!r}",
        )
        self.assertNotEqual(
            proc.stderr, "",
            f"{label}: 期望标准错误说明数据库路径原因，实际为空",
        )
        self.assertNotIn(
            "Traceback", proc.stderr,
            f"{label}: 标准错误出现 Python 回溯:\n{proc.stderr}",
        )
        # 此时错误应来自数据库路径而非 URL
        self.assertIn(
            "数据库", proc.stderr,
            f"{label}: 标准错误应说明数据库路径原因: {proc.stderr!r}",
        )
        # 样本库内容、目录文件集合均不变
        self.assert_state_unchanged(self.db, before, label)
        self.assert_dir_files_unchanged(self.tmp, files_before, label)

    # ---- 记录字段与数据库结构保持兼容：直接核对存储层 ----

    def test_database_schema_and_fields_remain_compatible(self):
        conn = sqlite3.connect(str(self.db))
        try:
            # checks 表结构：列名与样本写入一致（原有结构未被改变）
            cols = [
                r[1]
                for r in conn.execute("PRAGMA table_info(checks)")
            ]
            self.assertEqual(
                cols,
                ["id", "url", "checked_at", "elapsed_ms", "status",
                 "http_status", "reason"],
                f"checks 表字段与既有结构不兼容: {cols}",
            )
            # 八条样本全部按原样存在
            rows = conn.execute(
                "SELECT id, url, checked_at, elapsed_ms, status, "
                "http_status, reason FROM checks ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            len(rows), len(SAMPLE),
            "数据库记录条数与样本不符（查询不应增删记录）",
        )
        for row, rec in zip(rows, SAMPLE):
            self.assertEqual(
                row,
                (
                    rec["id"], rec["url"], rec["checked_at"], rec["elapsed_ms"],
                    rec["status"], rec["http_status"], rec["reason"],
                ),
                f"存储记录与样本逐值不符。\n实际={row!r}\n预期={rec!r}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
