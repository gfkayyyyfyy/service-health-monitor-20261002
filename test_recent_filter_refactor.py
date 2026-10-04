#!/usr/bin/env python3
"""recent 历史筛选流程重构的回归测试。

针对 healthcheck.py 中由单一查询构造器（build_recent_query）统一处理的
三个可选筛选（--url / --status / --reason），系统性覆盖全部八种组合
（普通与 --summary 两种模式），并核对空结果、错误路径、校验顺序、
表名大小写异写、只读库等边界行为与重构前一致。

通过公开命令行入口（子进程运行 healthcheck.py）观察退出码与 JSON 输出；
每个用例前后核对临时目录文件集合与库中全部记录不变（查询严格只读，
不创建文件、目录或表，不修改记录）。

验收固定样本（五条记录，与 test_recent_reason_filter.py 相同）：
- 目标 A：http://127.0.0.1:8765/health
- 目标 B：http://127.0.0.1:8765/ready（仅路径不同）
- id 1..5 依次保存：A 的 timeout、A 的 ok、A 的 timeout、B 的 timeout、A 的 ok；
- elapsed_ms 依次为 0、4、10、20、9 毫秒；
- ok 对应 success（http_status 200），timeout 对应 failure（http_status null）。

在项目目录执行：
    python3 -m unittest test_recent_filter_refactor
或：
    python3 test_recent_filter_refactor.py
"""

import json
import os
import pathlib
import shutil
import sqlite3
import stat
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

COLUMNS = ["id", "url", "checked_at", "elapsed_ms",
           "status", "http_status", "reason"]

URL_A = "http://127.0.0.1:8765/health"
URL_B = "http://127.0.0.1:8765/ready"
URL_NO_MATCH = "http://127.0.0.1:8765/no-such-path"

# id 1..5：A timeout、A ok、A timeout、B timeout、A ok；
# elapsed_ms：0、4、10、20、9。
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

EMPTY_SUMMARY = {
    "count": 0,
    "min_elapsed_ms": None,
    "max_elapsed_ms": None,
    "avg_elapsed_ms": None,
}

# 八种筛选组合：(子命令额外参数, 命中的记录)
# 期望结果由固定样本按「先取条件交集，再按 id 倒序，最后取 limit」推出，
# 与重构前八个查询分支的语义一一对应。
def expected_records(url=None, status=None, reason=None, limit=5):
    rows = [
        r for r in ALL_RECORDS
        if (url is None or r["url"] == url)
        and (status is None or r["status"] == status)
        and (reason is None or r["reason"] == reason)
    ]
    rows.sort(key=lambda r: r["id"], reverse=True)
    return rows[:limit]


def expected_summary(records):
    if not records:
        return EMPTY_SUMMARY
    elapsed = [r["elapsed_ms"] for r in records]
    return {
        "count": len(elapsed),
        "min_elapsed_ms": min(elapsed),
        "max_elapsed_ms": max(elapsed),
        "avg_elapsed_ms": sum(elapsed) / len(elapsed),
    }


# (用例名, url, status, reason)——None 表示省略该参数，共八种组合
COMBINATIONS = [
    ("none", None, None, None),
    ("url_only", URL_A, None, None),
    ("status_only", None, "failure", None),
    ("reason_only", None, None, "timeout"),
    ("url_status", URL_A, "failure", None),
    ("url_reason", URL_A, None, "timeout"),
    ("status_reason", None, "failure", "timeout"),
    ("url_status_reason", URL_A, "failure", "timeout"),
]


def run_cli(db_path, *extra):
    """以子进程运行 healthcheck.py 命令行，返回 CompletedProcess。"""
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


def run_recent(db_path, *extra):
    return run_cli(db_path, "recent", *extra)


def insert_records(conn, records):
    conn.executemany(
        "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
        "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (r["id"], r["url"], r["checked_at"], r["elapsed_ms"],
             r["status"], r["http_status"], r["reason"])
            for r in records
        ],
    )


def build_sample_db(path, table_name="checks"):
    """创建包含验收固定样本 5 条记录的临时数据库（可指定历史表名）。"""
    conn = sqlite3.connect(str(path))
    conn.execute(SCHEMA_SQL.replace("checks", f'"{table_name}"', 1))
    insert_records(conn, ALL_RECORDS)
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
        except sqlite3.Error as exc:
            # 不是有效 SQLite 文件：记录错误本身供前后比对
            tables["<unreadable>"] = str(exc)
        finally:
            conn.close()
    return files, tables


class RecentFilterCombinationTests(unittest.TestCase):
    """八种筛选组合在普通与摘要模式下的行为等价性。"""

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

    @staticmethod
    def combo_args(url, status, reason):
        args = []
        if url is not None:
            args += ["--url", url]
        if status is not None:
            args += ["--status", status]
        if reason is not None:
            args += ["--reason", reason]
        return args

    def test_all_eight_combinations_records(self):
        """八种组合：普通模式输出完整记录数组，先筛选再 id 倒序取 limit。"""
        for name, url, status, reason in COMBINATIONS:
            with self.subTest(combination=name):
                args = self.combo_args(url, status, reason)
                label = f"recent {' '.join(args)}"
                before = snapshot_state(self.tmp, self.db)
                proc = run_recent(self.db, *args)
                self.assertEqual(
                    proc.returncode, 0,
                    f"输入 {label}：期望退出码 0，实际 {proc.returncode}，"
                    f"stderr={proc.stderr!r}",
                )
                self.assertEqual(proc.stderr, "")
                records = json.loads(proc.stdout)
                expected = expected_records(url, status, reason)
                self.assertEqual(
                    records, expected,
                    f"输入 {label}：期望 id "
                    f"{[r['id'] for r in expected]}，实际 id "
                    f"{[r['id'] for r in records]}",
                )
                for rec in records:
                    self.assertEqual(set(rec.keys()), set(COLUMNS))
                self.assert_state_unchanged(before, label)

    def test_all_eight_combinations_summary(self):
        """八种组合：摘要只统计同一批记录，失败与零耗时参与，均值不取整。"""
        for name, url, status, reason in COMBINATIONS:
            with self.subTest(combination=name):
                args = self.combo_args(url, status, reason)
                label = f"recent {' '.join(args)} --summary"
                before = snapshot_state(self.tmp, self.db)
                proc = run_recent(self.db, *args, "--summary")
                self.assertEqual(
                    proc.returncode, 0,
                    f"输入 {label}：期望退出码 0，实际 {proc.returncode}，"
                    f"stderr={proc.stderr!r}",
                )
                self.assertEqual(proc.stderr, "")
                expected = expected_summary(
                    expected_records(url, status, reason)
                )
                self.assertEqual(
                    json.loads(proc.stdout), expected,
                    f"输入 {label}：期望摘要 {expected}，实际 {proc.stdout!r}",
                )
                self.assert_state_unchanged(before, label)

    def test_acceptance_url_reason_limit_2(self):
        """验收主用例：--url A --reason timeout --limit 2 → id [3, 1]。"""
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--url", URL_A,
                          "--reason", "timeout", "--limit", "2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(json.loads(proc.stdout), [REC3, REC1])
        self.assert_state_unchanged(before, "验收主用例")

        proc_s = run_recent(self.db, "--url", URL_A,
                            "--reason", "timeout", "--limit", "2", "--summary")
        self.assertEqual(proc_s.returncode, 0, proc_s.stderr)
        self.assertEqual(proc_s.stderr, "")
        self.assertEqual(
            proc_s.stdout,
            '{"count":2,"min_elapsed_ms":0,'
            '"max_elapsed_ms":10,"avg_elapsed_ms":5.0}\n',
        )
        self.assert_state_unchanged(before, "验收主用例（摘要）")

    def test_limit_applies_after_filtering_and_default_is_five(self):
        # 先筛选（A 且 timeout：id 3、1）再倒序取 1 条 → id 3
        proc = run_recent(self.db, "--url", URL_A,
                          "--reason", "timeout", "--limit", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([r["id"] for r in json.loads(proc.stdout)], [3])
        # 省略 --limit 默认 5 条：样本恰好 5 条全部返回
        proc_all = run_recent(self.db)
        self.assertEqual(
            [r["id"] for r in json.loads(proc_all.stdout)], [5, 4, 3, 2, 1]
        )

    def test_url_exact_match_no_merging(self):
        """URL 按保存的原始字符串精确匹配：路径不同的目标不合并。"""
        proc_b = run_recent(self.db, "--url", URL_B)
        self.assertEqual(proc_b.returncode, 0, proc_b.stderr)
        self.assertEqual(json.loads(proc_b.stdout), [REC4])
        # 路径不同即无匹配
        proc_none = run_recent(self.db, "--url", URL_NO_MATCH)
        self.assertEqual(proc_none.returncode, 0, proc_none.stderr)
        self.assertEqual(proc_none.stdout, "[]\n")

    def test_empty_intersection_combinations(self):
        """合法组合但交集为空：普通 []、摘要 count0 三 null，退出码 0。"""
        cases = [
            ("--url", URL_B, "--reason", "ok"),
            ("--status", "success", "--reason", "timeout"),
            ("--url", URL_A, "--status", "success", "--reason", "timeout"),
            ("--reason", "http_status"),
        ]
        for args in cases:
            with self.subTest(args=args):
                before = snapshot_state(self.tmp, self.db)
                proc = run_recent(self.db, *args)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")
                self.assertEqual(proc.stdout, "[]\n")
                proc_s = run_recent(self.db, *args, "--summary")
                self.assertEqual(proc_s.returncode, 0, proc_s.stderr)
                self.assertEqual(proc_s.stderr, "")
                self.assertEqual(json.loads(proc_s.stdout), EMPTY_SUMMARY)
                self.assert_state_unchanged(before, f"空交集 {args}")


class RecentFilterBoundaryTests(unittest.TestCase):
    """缺库、空表、错误路径等边界行为与重构前一致。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-refactor-edge-"))
        self.db = self.tmp / "monitor.sqlite"
        build_sample_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_empty_ok(self, proc, summary):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        if summary:
            self.assertEqual(json.loads(proc.stdout), EMPTY_SUMMARY)
        else:
            self.assertEqual(proc.stdout, "[]\n")

    def test_missing_db_and_missing_parent_dir(self):
        """缺库或父目录缺失：退出 0，普通 []、摘要 count0 三 null，不建文件。"""
        missing_root = self.tmp / "no-such-dir"
        for db_path in (self.tmp / "not-yet.sqlite",
                        missing_root / "nested" / "monitor.sqlite"):
            for summary in (False, True):
                with self.subTest(db=db_path.name, summary=summary):
                    before = snapshot_state(self.tmp, db_path)
                    extra = ("--url", URL_A, "--reason", "timeout")
                    if summary:
                        extra += ("--summary",)
                    proc = run_recent(db_path, *extra)
                    self.assert_empty_ok(proc, summary)
                    after = snapshot_state(self.tmp, db_path)
                    self.assertEqual(after, before)
        self.assertFalse(
            missing_root.exists(), "查询不应创建缺失的目录或数据库文件"
        )

    def test_no_checks_table_and_empty_table(self):
        """无历史表与空表：退出 0，普通 []、摘要 count0 三 null。"""
        other_db = self.tmp / "other-only.sqlite"
        conn = sqlite3.connect(str(other_db))
        conn.execute("CREATE TABLE other_t (name TEXT NOT NULL)")
        conn.execute("INSERT INTO other_t (name) VALUES ('kept')")
        conn.commit()
        conn.close()

        empty_db = self.tmp / "empty.sqlite"
        conn = sqlite3.connect(str(empty_db))
        conn.execute(SCHEMA_SQL)
        conn.commit()
        conn.close()

        for db_path in (other_db, empty_db):
            for summary in (False, True):
                with self.subTest(db=db_path.name, summary=summary):
                    before = snapshot_state(self.tmp, db_path)
                    extra = ("--status", "failure")
                    if summary:
                        extra += ("--summary",)
                    proc = run_recent(db_path, *extra)
                    self.assert_empty_ok(proc, summary)
                    self.assertEqual(
                        snapshot_state(self.tmp, db_path), before,
                        f"{db_path.name}：查询前后文件集合与数据不应变化",
                    )

    def test_db_path_is_directory(self):
        """数据库路径是目录：退出 2，stdout 空，原因写入 stderr。"""
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir()
        before = snapshot_state(self.tmp, db_dir)
        proc = run_recent(db_dir, "--reason", "timeout")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("目录", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(snapshot_state(self.tmp, db_dir), before)

    def test_invalid_sqlite_file(self):
        """不是有效 SQLite 数据库：退出 2，stdout 空，原因写入 stderr。"""
        bad_db = self.tmp / "not-a-db.sqlite"
        bad_db.write_bytes(b"this is not a sqlite database at all" * 4)
        before = snapshot_state(self.tmp, bad_db)
        proc = run_recent(bad_db, "--url", URL_A)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertTrue(proc.stderr.startswith("healthcheck: error:"))
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(snapshot_state(self.tmp, bad_db), before)

    def test_checks_table_missing_columns(self):
        """历史表缺字段：退出 2，stdout 空，stderr 列出缺失字段名。"""
        conn = sqlite3.connect(str(self.tmp / "missing-cols.sqlite"))
        conn.execute(
            "CREATE TABLE checks (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "url TEXT NOT NULL, checked_at TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()
        db = self.tmp / "missing-cols.sqlite"
        before = snapshot_state(self.tmp, db)
        proc = run_recent(db, "--reason", "timeout")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("checks 表缺少字段", proc.stderr)
        for col in ("elapsed_ms", "status", "http_status", "reason"):
            self.assertIn(col, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(snapshot_state(self.tmp, db), before)

    def test_case_variant_table_names(self):
        """checks 表名大小写异写（CHECKS / Checks）查询结果一致。"""
        for table in ("CHECKS", "Checks"):
            with self.subTest(table=table):
                db = self.tmp / f"{table}.sqlite"
                build_sample_db(db, table_name=table)
                proc = run_recent(db, "--url", URL_A,
                                  "--reason", "timeout", "--limit", "2")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(json.loads(proc.stdout), [REC3, REC1])
                proc_s = run_recent(db, "--status", "failure", "--summary")
                self.assertEqual(
                    json.loads(proc_s.stdout),
                    {"count": 3, "min_elapsed_ms": 0,
                     "max_elapsed_ms": 20, "avg_elapsed_ms": 10.0},
                )

    def test_readonly_database_is_queryable(self):
        """可读但不可写的库仍可查询，且不产生 -wal/-journal 旁路文件。"""
        ro_db = self.tmp / "readonly.sqlite"
        shutil.copy(self.db, ro_db)
        os.chmod(ro_db, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        try:
            proc = run_recent(ro_db, "--url", URL_A,
                              "--reason", "timeout", "--limit", "2")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout), [REC3, REC1])
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

    def test_invalid_limit_rejected_by_argparse(self):
        """非法 limit 先由参数解析拒绝：退出 2，stdout 空，不读取数据库。"""
        missing_db = self.tmp / "limit-check.sqlite"
        for value in ("0", "-1", "1.5", "abc", "", "+1"):
            with self.subTest(limit=value):
                proc = run_recent(missing_db, "--limit", value)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, "")
                self.assertNotIn("Traceback", proc.stderr)
                self.assertFalse(
                    missing_db.exists(),
                    f"非法 limit {value!r} 被拒绝时不应创建数据库文件",
                )

    def test_validation_order_status_url_reason_db(self):
        """错误报告顺序：status → URL → reason → 数据库路径。"""
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir()

        # 非法 status + 非法 URL + 非法 reason + 目录库：只报 status
        proc = run_recent(db_dir, "--status", "nope", "--url", "not-a-url",
                          "--reason", "bad")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("status", proc.stderr)
        self.assertNotIn("reason 参数错误", proc.stderr)
        self.assertNotIn("目录", proc.stderr)

        # status 合法后：非法 URL 优先于 reason 与路径错误
        proc_url = run_recent(db_dir, "--status", "success",
                              "--url", "https://127.0.0.1:8765/x",
                              "--reason", "bad")
        self.assertEqual(proc_url.returncode, 2)
        self.assertEqual(proc_url.stdout, "")
        self.assertIn("协议", proc_url.stderr)
        self.assertNotIn("reason 参数错误", proc_url.stderr)
        self.assertNotIn("目录", proc_url.stderr)

        # status、URL 合法后：非法 reason 优先于路径错误
        proc_reason = run_recent(db_dir, "--status", "success",
                                 "--url", URL_A, "--reason", "bad")
        self.assertEqual(proc_reason.returncode, 2)
        self.assertEqual(proc_reason.stdout, "")
        self.assertIn("reason", proc_reason.stderr)
        self.assertIn("'bad'", proc_reason.stderr)
        self.assertNotIn("目录", proc_reason.stderr)

        # 全部参数合法时，目录路径错误照常报告
        proc_dir = run_recent(db_dir, "--status", "success",
                              "--url", URL_A, "--reason", "ok")
        self.assertEqual(proc_dir.returncode, 2)
        self.assertEqual(proc_dir.stdout, "")
        self.assertIn("目录", proc_dir.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
