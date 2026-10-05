#!/usr/bin/env python3
"""streak 子命令的验收与回归测试。

覆盖：
- 连续失败段按 id 倒序自最大 id 起统计 failure 直到首条 success；
- latest_id 为该目标最大 id；其他目标、reason、http_status、checked_at
  不参与；不受 recent 默认五条限制；
- 可选 --threshold：省略时输出三字段；给出时追加整数 threshold 与布尔
  threshold_reached（次数大于或等于阈值为 true），仅接受 ASCII 十进制
  正整数（允许前导零），缺值/空值/零/负数/小数/空白/其他字符在访问
  数据库前以退出码 2 拒绝；空历史一律 false；非法 status 即使已达阈值
  仍以退出码 2 指出记录 id；
- 无匹配记录、缺库/缺父目录、空库、无历史表、空表 → url 原文 + null + 0；
- 大小写异写历史表、可读不可写数据库沿用 recent 的兼容范围；
- 缺参数、非法 URL、不支持的 recent 选项、路径为目录、无效 SQLite、
  缺字段、连续段中出现其他 status → 退出码 2、stdout 空、stderr 说明；
- 全程只读：不创建文件/目录/表、不改动已有数据、不发网络请求。

运行：python3 -m unittest test_streak
"""

import json
import os
import pathlib
import shutil
import stat
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "healthcheck.py"

URL_A = "http://127.0.0.1:8765/health"
URL_B = "http://127.0.0.1:8765/ready"

SCHEMA = """
CREATE TABLE {table} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    elapsed_ms INTEGER NOT NULL CHECK (elapsed_ms >= 0),
    status TEXT,
    http_status INTEGER,
    reason TEXT NOT NULL
)
"""


def run_streak(db_path, url=URL_A, *extra):
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path),
           "streak", "--url", url, *extra]
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8")


def build_db(path, rows, table="checks"):
    """rows: (id, url, status, http_status, reason, checked_at) 元组序列。"""
    conn = sqlite3.connect(str(path))
    conn.execute(SCHEMA.format(table=f'"{table}"'))
    conn.executemany(
        f'INSERT INTO "{table}" (id, url, checked_at, elapsed_ms, status, '
        "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(i, u, ts, 0, st, hs, rs)
         for (i, u, st, hs, rs, ts) in rows],
    )
    conn.commit()
    conn.close()


def R(record_id, url, status, http_status=None, reason="ok",
      checked_at=None):
    if checked_at is None:
        checked_at = f"2026-10-04T00:00:{record_id:02d}.000000+00:00"
    return (record_id, url, status, http_status, reason, checked_at)


def snapshot(tmpdir, db_path):
    files = {str(p.relative_to(tmpdir))
             for p in tmpdir.rglob("*") if p.is_file()}
    tables = {}
    if pathlib.Path(db_path).is_file():
        conn = sqlite3.connect(str(db_path))
        try:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            for name in names:
                tables[name] = conn.execute(
                    f'SELECT * FROM "{name}"').fetchall()
        finally:
            conn.close()
    return files, tables


class StreakTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-streak-"))

    def tearDown(self):
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                os.chmod(os.path.join(root, name), stat.S_IRWXU)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assertStreak(self, proc, url, latest_id, failures):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        # 恰好一行 JSON + 换行
        self.assertTrue(proc.stdout.endswith("\n"))
        self.assertEqual(proc.stdout.count("\n"), 1)
        self.assertEqual(
            json.loads(proc.stdout),
            {"url": url, "latest_id": latest_id,
             "consecutive_failures": failures},
        )

    def assertStreakThreshold(self, proc, url, latest_id, failures,
                              threshold, reached):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertTrue(proc.stdout.endswith("\n"))
        self.assertEqual(proc.stdout.count("\n"), 1)
        self.assertEqual(
            json.loads(proc.stdout),
            {"url": url, "latest_id": latest_id,
             "consecutive_failures": failures,
             "threshold": threshold, "threshold_reached": reached},
        )

    def assertRejected(self, proc):
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(proc.stdout, "")
        self.assertNotEqual(proc.stderr, "")
        self.assertNotIn("Traceback", proc.stderr)

    # ---- 题目给定验收样例 ----

    def test_spec_example_threshold_true_and_false(self):
        # id 1 目标 A 成功；id 2、4 目标 A 失败；id 3 是另一目标成功
        db = self.tmp / "m.sqlite"
        build_db(db, [
            R(1, URL_A, "success", 200, "ok"),
            R(2, URL_A, "failure", None, "connection_error"),
            R(3, URL_B, "success", 200, "ok"),
            R(4, URL_A, "failure", 500, "http_status"),
        ])
        self.assertStreakThreshold(
            run_streak(db, URL_A, "--threshold", "2"),
            URL_A, 4, 2, 2, True)
        # 同一查询改为 3：次数 2 < 3 → false
        self.assertStreakThreshold(
            run_streak(db, URL_A, "--threshold", "3"),
            URL_A, 4, 2, 3, False)
        # 边界：次数等于阈值即 true
        self.assertStreakThreshold(
            run_streak(db, URL_A, "--threshold", "1"),
            URL_A, 4, 2, 1, True)
        # 其他目标最新成功：0 次，阈值 1 仍 false
        self.assertStreakThreshold(
            run_streak(db, URL_B, "--threshold", "1"),
            URL_B, 3, 0, 1, False)

    def test_threshold_leading_zeros_output_as_integer(self):
        db = self.tmp / "m.sqlite"
        build_db(db, [
            R(1, URL_A, "failure", None, "timeout"),
            R(2, URL_A, "failure", None, "timeout"),
        ])
        self.assertStreakThreshold(
            run_streak(db, URL_A, "--threshold", "002"),
            URL_A, 2, 2, 2, True)
        self.assertStreakThreshold(
            run_streak(db, URL_A, "--threshold", "0003"),
            URL_A, 2, 2, 3, False)

    def test_threshold_reached_not_capped_by_recent_limit(self):
        # 12 连败：阈值 10 仍按完整次数判断
        db = self.tmp / "m.sqlite"
        build_db(db, [R(i, URL_A, "failure", None, "timeout")
                      for i in range(1, 13)])
        self.assertStreakThreshold(
            run_streak(db, URL_A, "--threshold", "10"),
            URL_A, 12, 12, 10, True)

    def test_threshold_latest_success_zero_false(self):
        db = self.tmp / "m.sqlite"
        build_db(db, [
            R(1, URL_A, "failure", None, "timeout"),
            R(2, URL_A, "success", 200, "ok"),
        ])
        self.assertStreakThreshold(
            run_streak(db, URL_A, "--threshold", "1"),
            URL_A, 2, 0, 1, False)

    def test_threshold_empty_histories_false(self):
        # 缺库（含缺父目录）、空库、仅有其他表、空表、无匹配：
        # latest_id null、次数 0、threshold_reached false
        missing = self.tmp / "nope" / "deep" / "m.sqlite"
        self.assertStreakThreshold(
            run_streak(missing, URL_A, "--threshold", "1"),
            URL_A, None, 0, 1, False)
        self.assertFalse((self.tmp / "nope").exists())

        db = self.tmp / "empty.sqlite"
        sqlite3.connect(str(db)).close()
        self.assertStreakThreshold(
            run_streak(db, URL_A, "--threshold", "2"),
            URL_A, None, 0, 2, False)

        other = self.tmp / "other.sqlite"
        conn = sqlite3.connect(str(other))
        conn.execute("CREATE TABLE other_t (name TEXT)")
        conn.commit()
        conn.close()
        self.assertStreakThreshold(
            run_streak(other, URL_A, "--threshold", "2"),
            URL_A, None, 0, 2, False)

        rowsdb = self.tmp / "b.sqlite"
        build_db(rowsdb, [R(1, URL_B, "success", 200, "ok")])
        self.assertStreakThreshold(
            run_streak(rowsdb, URL_A, "--threshold", "1"),
            URL_A, None, 0, 1, False)

    def test_threshold_readonly_and_case_tables(self):
        db = self.tmp / "ro.sqlite"
        build_db(db, [R(1, URL_A, "failure", None, "timeout")])
        os.chmod(db, stat.S_IRUSR)
        try:
            self.assertStreakThreshold(
                run_streak(db, URL_A, "--threshold", "1"),
                URL_A, 1, 1, 1, True)
        finally:
            os.chmod(db, stat.S_IRWXU)

        cdb = self.tmp / "c.sqlite"
        build_db(cdb, [R(1, URL_A, "failure", None, "timeout")],
                 table="CHECKS")
        self.assertStreakThreshold(
            run_streak(cdb, URL_A, "--threshold", "2"),
            URL_A, 1, 1, 2, False)

    # ---- 阈值非法：退出码 2、stdout 空、访问数据库前拒绝 ----

    def test_invalid_thresholds_rejected_before_db_access(self):
        missing_parent = self.tmp / "never"
        db = missing_parent / "x.sqlite"
        for bad in ("", "0", "000", "-1", "-0", "+1", "1.0", "1.5",
                    " 2", "2 ", "\t2", "1 2", "abc", "0x1", "1e3",
                    "²", "０", "１２", "true"):
            with self.subTest(threshold=bad):
                proc = run_streak(db, URL_A, "--threshold", bad)
                self.assertRejected(proc)
                self.assertIn("threshold", proc.stderr)
        # 裸 --threshold（缺少值）
        proc = run_streak(db, URL_A, "--threshold")
        self.assertRejected(proc)
        self.assertIn("threshold", proc.stderr)
        # 任何非法阈值都不得触发数据库路径创建
        self.assertFalse(os.path.exists(missing_parent))

    def test_invalid_threshold_rejected_before_url_validation(self):
        # threshold 先于 URL 校验：URL 与阈值同时非法时报 threshold
        db = self.tmp / "m.sqlite"
        proc = run_streak(db, "http://localhost:1/x", "--threshold", "0")
        self.assertRejected(proc)
        self.assertIn("threshold", proc.stderr)

    # ---- 阈值不改变数据错误边界 ----

    def test_threshold_illegal_status_rejected_even_if_reached(self):
        # 最新 1 条 failure 已达阈值 1，下一条非法 status 仍须拒绝并指出 id
        db = self.tmp / "weird.sqlite"
        build_db(db, [
            R(1, URL_A, "success", 200, "ok"),
            R(2, URL_A, "weird"),
            R(3, URL_A, "failure", None, "timeout"),
        ])
        proc = run_streak(db, URL_A, "--threshold", "1")
        self.assertRejected(proc)
        self.assertIn("id=2", proc.stderr)

    def test_threshold_illegal_status_older_than_success_ignored(self):
        db = self.tmp / "old.sqlite"
        build_db(db, [
            R(1, URL_A, "weird"),
            R(2, URL_A, "success", 200, "ok"),
        ])
        self.assertStreakThreshold(
            run_streak(db, URL_A, "--threshold", "1"),
            URL_A, 2, 0, 1, False)

    def test_threshold_with_db_errors_still_rejected(self):
        # 目录路径、非 SQLite、缺列：合法阈值不改变退出码 2 与空 stdout
        dirdb = self.tmp
        self.assertRejected(
            run_streak(dirdb, URL_A, "--threshold", "1"))

        garbage = self.tmp / "g.db"
        garbage.write_bytes(b"not a sqlite database\n")
        self.assertRejected(
            run_streak(garbage, URL_A, "--threshold", "1"))

        badcols = self.tmp / "badcols.db"
        conn = sqlite3.connect(str(badcols))
        conn.execute("CREATE TABLE checks (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO checks VALUES (1)")
        conn.commit()
        conn.close()
        proc = run_streak(badcols, URL_A, "--threshold", "1")
        self.assertRejected(proc)
        self.assertIn("缺少字段", proc.stderr)

    def test_threshold_with_invalid_url_rejected(self):
        db = self.tmp / "m.sqlite"
        build_db(db, [R(1, URL_A, "failure")])
        proc = run_streak(db, "http://localhost:8765/h",
                          "--threshold", "1")
        self.assertRejected(proc)

    def test_omitted_threshold_keeps_three_fields(self):
        db = self.tmp / "m.sqlite"
        build_db(db, [R(1, URL_A, "failure", None, "timeout")])
        proc = run_streak(db, URL_A)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(
            set(payload),
            {"url", "latest_id", "consecutive_failures"})
        self.assertNotIn("threshold", payload)
        self.assertNotIn("threshold_reached", payload)

    def test_threshold_query_is_readonly(self):
        db = self.tmp / "m.sqlite"
        build_db(db, [R(1, URL_A, "failure", None, "timeout")])
        before = snapshot(self.tmp, db)
        run_streak(db, URL_A, "--threshold", "1")
        self.assertEqual(snapshot(self.tmp, db), before)
        self.assertEqual(
            {p.name for p in self.tmp.iterdir()}, {"m.sqlite"})

    # ---- 题目给定验收样例 ----

    def test_spec_example_interleaved_other_target(self):
        # id 1 目标 A 成功；id 2、4 目标 A 失败；id 3 是另一目标成功
        db = self.tmp / "m.sqlite"
        build_db(db, [
            R(1, URL_A, "success", 200, "ok"),
            R(2, URL_A, "failure", None, "connection_error"),
            R(3, URL_B, "success", 200, "ok"),
            R(4, URL_A, "failure", 500, "http_status"),
        ])
        proc = run_streak(db, URL_A)
        self.assertStreak(proc, URL_A, 4, 2)
        # 另一目标独立计数：最新成功 → 0，latest_id 3
        self.assertStreak(run_streak(db, URL_B), URL_B, 3, 0)

        # 原目标再保存 id 5 成功 → 5 和 0
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
            "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (5, URL_A, "2026-10-04T00:00:05.000000+00:00", 2,
             "success", 200, "ok"),
        )
        conn.commit()
        conn.close()
        self.assertStreak(run_streak(db, URL_A), URL_A, 5, 0)

    # ---- 连续段基本语义 ----

    def test_latest_success_gives_zero_but_keeps_latest_id(self):
        db = self.tmp / "m.sqlite"
        build_db(db, [
            R(1, URL_A, "failure", None, "timeout"),
            R(2, URL_A, "failure", None, "timeout"),
            R(3, URL_A, "success", 200, "ok"),
        ])
        self.assertStreak(run_streak(db), URL_A, 3, 0)

    def test_all_failures_counts_all(self):
        db = self.tmp / "m.sqlite"
        build_db(db, [R(i, URL_A, "failure", None, "timeout")
                      for i in range(1, 6)])
        self.assertStreak(run_streak(db), URL_A, 5, 5)

    def test_stops_at_first_success_and_ignores_older_rows(self):
        # id 4 失败、id 3 成功即止；id 2 的未知 status 不应被看到
        db = self.tmp / "m.sqlite"
        build_db(db, [
            R(1, URL_A, "failure"),
            R(2, URL_A, "weird"),
            R(3, URL_A, "success", 200),
            R(4, URL_A, "failure", 502, "http_status"),
        ])
        self.assertStreak(run_streak(db), URL_A, 4, 1)

    def test_not_capped_by_recent_default_limit_five(self):
        db = self.tmp / "m.sqlite"
        build_db(db, [R(i, URL_A, "failure", None, "timeout")
                      for i in range(1, 13)])  # 12 连败
        self.assertStreak(run_streak(db), URL_A, 12, 12)

    def test_only_saved_status_matters_not_reason_or_http_status(self):
        # failure 但带 http_status 200、reason ok：只认 status
        db = self.tmp / "m.sqlite"
        build_db(db, [
            R(1, URL_A, "failure", 200, "ok"),
            R(2, URL_A, "failure", None, "ok"),
        ])
        self.assertStreak(run_streak(db), URL_A, 2, 2)

    def test_ordering_by_id_not_checked_at(self):
        # checked_at 乱序：id 大但时间早，仍按 id 判定连续段
        db = self.tmp / "m.sqlite"
        build_db(db, [
            R(1, URL_A, "success", 200,
              checked_at="2026-10-04T00:00:09.000000+00:00"),
            R(2, URL_A, "failure", None, "timeout",
              checked_at="2026-10-04T00:00:01.000000+00:00"),
        ])
        self.assertStreak(run_streak(db), URL_A, 2, 1)

    def test_url_exact_verbatim_match_no_normalization(self):
        db = self.tmp / "m.sqlite"
        url_detail = "http://127.0.0.1:8765/health?detail=1"
        build_db(db, [
            R(1, url_detail, "failure", None, "timeout"),
            R(2, URL_A, "success", 200, "ok"),
        ])
        # 不带 query 的同路径不算同一目标
        self.assertStreak(run_streak(db, URL_A), URL_A, 2, 0)
        self.assertStreak(run_streak(db, url_detail), url_detail, 1, 1)

    def test_unicode_url_echoed_verbatim(self):
        db = self.tmp / "m.sqlite"
        url_cn = "http://127.0.0.1:8765/健康检查"
        build_db(db, [R(1, url_cn, "failure", None, "timeout")])
        proc = run_streak(db, url_cn)
        self.assertStreak(proc, url_cn, 1, 1)
        self.assertIn(url_cn, proc.stdout)

    # ---- 空结果语义：url 原文、latest_id null、0 ----

    def test_no_matching_record(self):
        db = self.tmp / "m.sqlite"
        build_db(db, [R(1, URL_B, "success", 200, "ok")])
        self.assertStreak(run_streak(db), URL_A, None, 0)

    def test_missing_db_and_missing_parent_creates_nothing(self):
        missing_parent = self.tmp / "nope"
        db = missing_parent / "deep" / "m.sqlite"
        proc = run_streak(db)
        self.assertStreak(proc, URL_A, None, 0)
        self.assertFalse(os.path.exists(missing_parent))

    def test_empty_db_and_other_table_only(self):
        db = self.tmp / "empty.sqlite"
        sqlite3.connect(str(db)).close()
        self.assertStreak(run_streak(db), URL_A, None, 0)

        db2 = self.tmp / "other.sqlite"
        conn = sqlite3.connect(str(db2))
        conn.execute("CREATE TABLE other_t (name TEXT, n INTEGER)")
        conn.execute("INSERT INTO other_t VALUES ('kept', 42)")
        conn.commit()
        conn.close()
        before = snapshot(self.tmp, db2)
        self.assertStreak(run_streak(db2), URL_A, None, 0)
        self.assertEqual(snapshot(self.tmp, db2), before)

    def test_history_table_exists_but_empty(self):
        db = self.tmp / "emptyrows.sqlite"
        build_db(db, [])
        self.assertStreak(run_streak(db), URL_A, None, 0)

    # ---- recent 的兼容范围：大小写异写表、只读文件 ----

    def test_case_insensitive_table_names(self):
        for table in ("CHECKS", "Checks", "checks"):
            db = self.tmp / f"{table}.sqlite"
            build_db(db, [
                R(1, URL_A, "success", 200, "ok"),
                R(2, URL_A, "failure", None, "timeout"),
            ], table=table)
            self.assertStreak(run_streak(db), URL_A, 2, 1)

    def test_readonly_db_still_queries_and_modifies_nothing(self):
        db = self.tmp / "ro.sqlite"
        build_db(db, [
            R(1, URL_A, "failure", None, "connection_error"),
            R(2, URL_A, "failure", None, "timeout"),
        ])
        os.chmod(db, stat.S_IRUSR)
        try:
            before = snapshot(self.tmp, db)
            self.assertStreak(run_streak(db), URL_A, 2, 2)
            self.assertEqual(snapshot(self.tmp, db), before)
            # 不得产生 -wal/-journal
            self.assertEqual(
                {p.name for p in self.tmp.iterdir()}, {"ro.sqlite"})
        finally:
            os.chmod(db, stat.S_IRWXU)

    # ---- 连续段中的其他 status：退出码 2 并指出 id ----

    def test_unknown_status_in_segment_rejected_with_id(self):
        db = self.tmp / "weird.sqlite"
        build_db(db, [
            R(1, URL_A, "success", 200, "ok"),
            R(2, URL_A, "failure", None, "timeout"),
            R(3, URL_A, "degraded"),
        ])
        before = snapshot(self.tmp, db)
        proc = run_streak(db)
        self.assertRejected(proc)
        self.assertIn("id=3", proc.stderr)
        self.assertEqual(snapshot(self.tmp, db), before)

    def test_unknown_status_on_other_target_does_not_matter(self):
        db = self.tmp / "weird.sqlite"
        build_db(db, [
            R(1, URL_B, "degraded"),
            R(2, URL_A, "failure", None, "timeout"),
        ])
        self.assertStreak(run_streak(db, URL_A), URL_A, 2, 1)

    # ---- 参数错误：退出码 2、stdout 空 ----

    def test_missing_url(self):
        db = self.tmp / "m.sqlite"
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(db), "streak"],
            capture_output=True, text=True)
        self.assertRejected(proc)

    def test_missing_db(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "streak", "--url", URL_A],
            capture_output=True, text=True)
        self.assertRejected(proc)

    def test_invalid_url_rejected_before_db_access(self):
        missing_parent = self.tmp / "never"
        db = missing_parent / "x.sqlite"
        for bad in ("http://localhost:8765/health",
                    "http://127.0.0.1/health",
                    "ftp://127.0.0.1:8765/health",
                    "http://127.0.0.1:8765/health#frag",
                    ""):
            with self.subTest(url=bad):
                proc = run_streak(db, bad)
                self.assertRejected(proc)
        self.assertFalse(os.path.exists(missing_parent))

    def test_unsupported_recent_options_rejected(self):
        db = self.tmp / "m.sqlite"
        build_db(db, [R(1, URL_A, "failure")])
        for extra in (("--limit", "2"), ("--status", "failure"),
                      ("--reason", "timeout"),
                      ("--since", "2026-10-04T00:00:00Z"),
                      ("--until", "2026-10-04T00:00:00Z"),
                      ("--summary",), ("--status-summary",)):
            with self.subTest(extra=extra):
                proc = run_streak(db, URL_A, *extra)
                self.assertRejected(proc)

    def test_unknown_subcommand_or_option(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", "x.sqlite",
             "streak", "--url", URL_A, "--bogus"],
            capture_output=True, text=True)
        self.assertRejected(proc)

    # ---- 数据库错误边界（沿用 recent）----

    def test_path_is_a_directory(self):
        self.assertRejected(run_streak(self.tmp))

    def test_file_is_not_sqlite(self):
        db = self.tmp / "garbage.db"
        db.write_bytes(b"not a sqlite database\n")
        self.assertRejected(run_streak(db))

    def test_no_read_permission(self):
        if os.geteuid() == 0:
            self.skipTest("root 绕过文件读权限，无法验证")
        db = self.tmp / "noread.db"
        build_db(db, [R(1, URL_A, "failure")])
        os.chmod(db, 0)
        try:
            self.assertRejected(run_streak(db))
        finally:
            os.chmod(db, stat.S_IRWXU)

    def test_history_table_missing_columns(self):
        db = self.tmp / "badcols.db"
        conn = sqlite3.connect(str(db))
        conn.execute('CREATE TABLE checks (id INTEGER PRIMARY KEY)')
        conn.execute("INSERT INTO checks VALUES (1)")
        conn.commit()
        conn.close()
        before = snapshot(self.tmp, db)
        proc = run_streak(db)
        self.assertRejected(proc)
        self.assertIn("缺少字段", proc.stderr)
        self.assertEqual(snapshot(self.tmp, db), before)

    # ---- 既有命令行为不受影响（抽查）----

    def test_recent_and_check_unaffected_smoke(self):
        db = self.tmp / "m.sqlite"
        build_db(db, [
            R(1, URL_A, "success", 200, "ok"),
            R(2, URL_A, "failure", None, "timeout"),
        ])
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(db), "recent"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([r["id"] for r in json.loads(proc.stdout)], [2, 1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
