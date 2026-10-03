#!/usr/bin/env python3
"""recent --summary 耗时摘要功能的独立回归测试。

通过公开命令行入口（子进程运行 healthcheck.py）核对退出码与 JSON 输出，
样本数据写入独立临时 SQLite 数据库，不依赖真实服务或已有历史文件，
临时资源在每个用例结束后释放。覆盖：

- 按目标筛选 + --limit 的 count/min/max/avg（失败与零耗时同样计入）
- 省略 URL 与 limit 时对最新五条的摘要
- 摘要仅含四个字段、单行 JSON 对象、退出码 0、stderr 为空
- 相同条件下去掉 --summary 仍返回对应记录数组（普通 recent 行为不变）
- 四种空结果（无匹配 / 空 checks 表 / 仅其他表 / 数据库与父目录均不存在）
- 摘要查询只读：不发网络请求、不改表结构与记录、不新增文件或目录
- --limit 0、HTTPS 筛选地址、目录形式数据库路径均退出 2；
  非法 URL 与目录路径同时出现时优先报告 URL 错误
- check 所需的 checks 表结构保持兼容

在项目目录执行：
    python3 -m unittest test_recent_summary
或：
    python3 test_recent_summary.py
重复执行结果一致。
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

COLUMNS = ["id", "url", "checked_at", "elapsed_ms",
           "status", "http_status", "reason"]

SUMMARY_FIELDS = ["count", "min_elapsed_ms",
                  "max_elapsed_ms", "avg_elapsed_ms"]

# ---- 固定样本 -------------------------------------------------------------
# 目标 A：带路径与查询参数；目标 B 仅把 detail 改为 2
URL_A = "http://127.0.0.1:8765/health?detail=1"
URL_B = "http://127.0.0.1:8765/health?detail=2"
# 合法但样本中没有任何记录的目标
URL_NO_MATCH = "http://127.0.0.1:8765/no-such-path?x=9"
# 协议非法的筛选地址
URL_HTTPS = "https://127.0.0.1:8765/health?detail=1"

# id 1..7 的目标依次为 A、B、A、B、A、A、B；
# elapsed_ms 依次为 90、80、0、70、2、7、60；
# A 的零耗时(id=3)与七毫秒(id=6)记录为 failure，其余为 success。
# checked_at 的先后顺序与 id 相反：id 7 最早、id 1 最新。
ROWS = [
    # id, url,  elapsed, status,    http_status, reason
    (1, URL_A, 90, "success", 200,   "ok"),
    (2, URL_B, 80, "success", 200,   "ok"),
    (3, URL_A,  0, "failure", None,  "connection_error"),
    (4, URL_B, 70, "success", 200,   "ok"),
    (5, URL_A,  2, "success", 200,   "ok"),
    (6, URL_A,  7, "failure", None,  "connection_error"),
    (7, URL_B, 60, "success", 200,   "ok"),
]

# id=7 最早（:00），id=1 最新（:06）
_TS_BY_ID = {row_id: f"2026-10-02T04:40:{7 - row_id:02d}.000000+00:00"
             for row_id, *_ in ROWS}

ALL_RECORDS = [
    {
        "id": row_id,
        "url": url,
        "checked_at": _TS_BY_ID[row_id],
        "elapsed_ms": elapsed,
        "status": status,
        "http_status": http_status,
        "reason": reason,
    }
    for row_id, url, elapsed, status, http_status, reason in ROWS
]
RECORD_BY_ID = {r["id"]: r for r in ALL_RECORDS}


def run_cli(db_path, *extra):
    """以子进程运行 healthcheck.py 命令行，返回 CompletedProcess。"""
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


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
    """有效数据库，checks 表结构齐全但没有任何记录。"""
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

    # ---- 公共断言工具 ----

    def assert_summary_ok(self, proc, expected, label):
        """退出码 0、stderr 空；stdout 为仅含四字段的单行 JSON 对象。"""
        self.assertEqual(
            proc.returncode, 0,
            f"输入 {label}：期望退出码 0，实际 {proc.returncode}，"
            f"stderr={proc.stderr!r}",
        )
        self.assertEqual(
            proc.stderr, "",
            f"输入 {label}：期望 stderr 为空，实际 {proc.stderr!r}",
        )
        # 单行 JSON 对象：整体恰好一行（末尾一个换行）
        self.assertEqual(
            proc.stdout.count("\n"), 1,
            f"输入 {label}：摘要应为一行输出，实际 {proc.stdout!r}",
        )
        self.assertTrue(
            proc.stdout.endswith("\n"),
            f"输入 {label}：输出应以换行结束，实际 {proc.stdout!r}",
        )
        summary = json.loads(proc.stdout)
        self.assertIsInstance(
            summary, dict,
            f"输入 {label}：摘要应为 JSON 对象，实际 {proc.stdout!r}",
        )
        self.assertEqual(
            set(summary.keys()), set(SUMMARY_FIELDS),
            f"输入 {label}：摘要应仅含 {SUMMARY_FIELDS} 四个字段，"
            f"实际字段 {list(summary.keys())}",
        )
        self.assertEqual(
            summary["count"], expected[0],
            f"输入 {label}：count 期望 {expected[0]}，"
            f"实际 {summary['count']}",
        )
        self.assertEqual(
            summary["min_elapsed_ms"], expected[1],
            f"输入 {label}：min_elapsed_ms 期望 {expected[1]}，"
            f"实际 {summary['min_elapsed_ms']}",
        )
        self.assertEqual(
            summary["max_elapsed_ms"], expected[2],
            f"输入 {label}：max_elapsed_ms 期望 {expected[2]}，"
            f"实际 {summary['max_elapsed_ms']}",
        )
        # 平均值按数值比较（3 与 3.0 等价），允许二进制浮点正常误差
        self.assertAlmostEqual(
            summary["avg_elapsed_ms"], expected[3],
            places=10,
            msg=f"输入 {label}：avg_elapsed_ms 期望 {expected[3]}，"
                f"实际 {summary['avg_elapsed_ms']}",
        )
        return summary

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

    # ---- 样本本身：checked_at 先后顺序与 id 相反 ----

    def test_sample_checked_at_order_is_reverse_of_id(self):
        conn = sqlite3.connect(str(self.db))
        try:
            rows = conn.execute(
                "SELECT id, checked_at FROM checks ORDER BY checked_at"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            [row[0] for row in rows], [7, 6, 5, 4, 3, 2, 1],
            "按 checked_at 升序应得到 id [7..1]，即 checked_at 先后与 id 相反",
        )

    # ---- 只查 A：--limit 2 / --limit 3 ----

    def test_summary_target_a_limit_2(self):
        # A 的记录按 id 倒序为 6(7ms,failure)、5(2ms,success)、
        # 3(0ms,failure)、1(90ms,success)；limit 2 取 7、2
        label = f"recent --summary --url A --limit 2"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--summary", "--url", URL_A,
                          "--limit", "2")
        self.assert_summary_ok(proc, (2, 2, 7, 4.5), label)
        self.assert_state_unchanged(before, label)

    def test_summary_target_a_limit_3_includes_failure_and_zero(self):
        # limit 3 取 7、2、0：失败记录与零耗时都参与统计
        label = f"recent --summary --url A --limit 3"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--summary", "--url", URL_A,
                          "--limit", "3")
        self.assert_summary_ok(proc, (3, 0, 7, 3), label)
        self.assert_state_unchanged(before, label)

    def test_summary_target_a_all_four(self):
        # 不带 limit（默认 5 >= 4）：A 全部四条 90/0/2/7
        label = f"recent --summary --url A（默认 limit）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--summary", "--url", URL_A)
        self.assert_summary_ok(proc, (4, 0, 90, 24.75), label)
        self.assert_state_unchanged(before, label)

    def test_summary_target_b_uses_only_b_records(self):
        # 对照：B 的三条为 80/70/60，A 的记录不得混入
        label = f"recent --summary --url B（默认 limit）"
        proc = run_recent(self.db, "--summary", "--url", URL_B)
        self.assert_summary_ok(proc, (3, 60, 80, 70.0), label)

    # ---- 省略 URL 与 limit：最新五条 ----

    def test_summary_latest_five_with_defaults(self):
        # 不带 URL、不带 limit：跨全部目标 id 倒序 7,6,5,4,3，
        # 耗时 60、7、2、70、0
        label = "recent --summary（省略 URL 与 limit）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--summary")
        self.assert_summary_ok(proc, (5, 0, 70, 27.8), label)
        self.assert_state_unchanged(before, label)

    # ---- 相同条件去掉 --summary：仍是对应记录数组 ----

    def test_without_summary_same_conditions_returns_records(self):
        cases = [
            ("--url A --limit 2",
             ["--url", URL_A, "--limit", "2"], [6, 5]),
            ("--url A --limit 3",
             ["--url", URL_A, "--limit", "3"], [6, 5, 3]),
            ("--url A 默认 limit",
             ["--url", URL_A], [6, 5, 3, 1]),
            ("省略 URL 与 limit",
             [], [7, 6, 5, 4, 3]),
        ]
        for label, extra, expected_ids in cases:
            with self.subTest(label=label):
                proc = run_recent(self.db, *extra)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")
                records = json.loads(proc.stdout)
                self.assertIsInstance(records, list)
                self.assertEqual(
                    [r["id"] for r in records], expected_ids,
                    f"输入 recent {label}：期望记录 id {expected_ids}，"
                    f"实际 {[r['id'] for r in records]}",
                )
                # 完整记录逐值相等，字段集合与 check 写入格式一致
                self.assertEqual(
                    records,
                    [RECORD_BY_ID[i] for i in expected_ids],
                    f"输入 recent {label}：记录内容与样本不一致",
                )
                self.assertTrue(
                    all(set(r.keys()) == set(COLUMNS) for r in records),
                    f"输入 recent {label}：记录字段应恰为 {COLUMNS}",
                )

    def test_summary_and_records_describe_same_row_set(self):
        # 摘要统计的批次必须与同条件普通 recent 返回的记录一致：
        # 直接用记录数组重新计算四个统计量并与摘要互校
        for label, extra in (
            ("A limit 2", ["--url", URL_A, "--limit", "2"]),
            ("A limit 3", ["--url", URL_A, "--limit", "3"]),
            ("默认", []),
        ):
            with self.subTest(label=label):
                rec_proc = run_recent(self.db, *extra)
                sum_proc = run_recent(self.db, "--summary", *extra)
                records = json.loads(rec_proc.stdout)
                summary = json.loads(sum_proc.stdout)
                values = [r["elapsed_ms"] for r in records]
                self.assertEqual(summary["count"], len(values))
                self.assertEqual(summary["min_elapsed_ms"], min(values))
                self.assertEqual(summary["max_elapsed_ms"], max(values))
                self.assertAlmostEqual(
                    summary["avg_elapsed_ms"], sum(values) / len(values),
                    places=10,
                )

    # ---- 可重复性：连续两次执行结果逐字节一致 ----

    def test_repeated_runs_are_identical(self):
        for extra in (
            ["--summary", "--url", URL_A, "--limit", "3"],
            ["--summary"],
        ):
            with self.subTest(extra=extra):
                first = run_recent(self.db, *extra)
                second = run_recent(self.db, *extra)
                self.assertEqual((first.returncode, first.stdout,
                                  first.stderr),
                                 (second.returncode, second.stdout,
                                  second.stderr))

    # ---- 四种空结果：count 0、耗时字段 null、退出码 0 ----

    def assert_empty_summary(self, proc, label):
        self.assertEqual(
            proc.returncode, 0,
            f"输入 {label}：期望退出码 0，实际 {proc.returncode}，"
            f"stderr={proc.stderr!r}",
        )
        self.assertEqual(proc.stderr, "")
        summary = json.loads(proc.stdout)
        self.assertEqual(
            summary,
            {
                "count": 0,
                "min_elapsed_ms": None,
                "max_elapsed_ms": None,
                "avg_elapsed_ms": None,
            },
            f"输入 {label}：期望空摘要四字段，实际 {proc.stdout!r}",
        )

    def test_empty_summary_valid_url_without_match(self):
        label = f"recent --summary --url {URL_NO_MATCH!r}（无匹配）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--summary", "--url", URL_NO_MATCH)
        self.assert_empty_summary(proc, label)
        self.assert_state_unchanged(before, label)

    def test_empty_summary_empty_checks_table(self):
        db = self.tmp / "empty-checks.sqlite"
        make_empty_checks_db(db)
        label = "recent --summary（checks 表为空）"
        before = snapshot_state(self.tmp, db)
        proc = run_recent(db, "--summary", "--url", URL_A)
        self.assert_empty_summary(proc, label)
        self.assert_state_unchanged(before, label, db)

    def test_empty_summary_only_other_table(self):
        db = self.tmp / "other-only.sqlite"
        make_other_table_db(db)
        label = "recent --summary（仅有其他表）"
        before = snapshot_state(self.tmp, db)
        proc = run_recent(db, "--summary", "--url", URL_A)
        self.assert_empty_summary(proc, label)
        # 不新建 checks 表，其他表结构与数据原样保留
        self.assert_state_unchanged(before, label, db)

    def test_empty_summary_missing_database_and_parent_creates_nothing(self):
        missing_root = self.tmp / "does-not-exist"
        db = missing_root / "nested" / "monitor.sqlite"
        label = "recent --summary（数据库与父目录均不存在）"
        before = snapshot_state(self.tmp, db)
        proc = run_recent(db, "--summary", "--url", URL_A)
        self.assert_empty_summary(proc, label)
        # 缺失路径在查询后仍不存在：不新增任何文件或目录
        self.assertFalse(
            missing_root.exists(),
            f"输入 {label}：查询不应创建缺失目录 {missing_root}",
        )
        self.assertFalse(db.exists())
        self.assert_state_unchanged(before, label, db)

    # ---- 非法参数：退出码 2、stdout 为空、stderr 说明原因 ----

    def assert_rejected(self, proc, label, reason_keyword=None,
                        echo_value=None):
        self.assertEqual(
            proc.returncode, 2,
            f"输入 {label}：期望退出码 2，实际 {proc.returncode}，"
            f"stdout={proc.stdout!r}",
        )
        self.assertEqual(
            proc.stdout, "",
            f"输入 {label}：期望 stdout 为空，实际 {proc.stdout!r}",
        )
        self.assertNotEqual(
            proc.stderr, "",
            f"输入 {label}：期望 stderr 说明原因，实际为空",
        )
        self.assertNotIn(
            "Traceback", proc.stderr,
            f"输入 {label}：stderr 不应包含 Python 回溯，"
            f"实际 {proc.stderr!r}",
        )
        if reason_keyword is not None:
            self.assertIn(
                reason_keyword, proc.stderr,
                f"输入 {label}：stderr 应说明原因（含 {reason_keyword!r}），"
                f"实际 {proc.stderr!r}",
            )
        if echo_value is not None:
            self.assertIn(
                echo_value, proc.stderr,
                f"输入 {label}：stderr 应回显非法输入 {echo_value!r}，"
                f"实际 {proc.stderr!r}",
            )

    def test_limit_zero_rejected(self):
        label = "recent --summary --limit 0"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--summary", "--url", URL_A,
                          "--limit", "0")
        self.assert_rejected(proc, label, reason_keyword="limit",
                             echo_value="0")
        self.assert_state_unchanged(before, label)

    def test_https_filter_url_rejected(self):
        label = "recent --summary --url https://..."
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--summary", "--url", URL_HTTPS)
        self.assert_rejected(proc, label, reason_keyword="协议",
                             echo_value=URL_HTTPS)
        self.assert_state_unchanged(before, label)

    def test_directory_database_path_rejected(self):
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir()
        label = "recent --summary（数据库路径是目录）"
        before = snapshot_state(self.tmp, db_dir)
        proc = run_recent(db_dir, "--summary", "--url", URL_A)
        self.assert_rejected(proc, label, reason_keyword="目录")
        self.assert_state_unchanged(before, label, db_dir)

    def test_invalid_url_takes_priority_over_directory_path(self):
        # 非法 URL 与目录形式数据库路径同时出现：优先报告 URL 错误，
        # 不允许先报目录错误
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir()
        label = "recent --summary --url https://...（数据库路径是目录）"
        before = snapshot_state(self.tmp, db_dir)
        proc = run_recent(db_dir, "--summary", "--url", URL_HTTPS)
        self.assert_rejected(proc, label, reason_keyword="协议",
                             echo_value=URL_HTTPS)
        self.assertNotIn(
            "目录", proc.stderr,
            f"输入 {label}：应优先报告 URL 错误，却报告了目录错误："
            f"{proc.stderr!r}",
        )
        self.assert_state_unchanged(before, label, db_dir)

    # ---- 数据库结构兼容：check 依赖的列定义不被改变 ----

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
        # 摘要输出不包含记录级字段；普通 recent 的字段集合仍完整
        proc = run_recent(self.db, "--url", URL_A, "--limit", "1")
        records = json.loads(proc.stdout)
        self.assertEqual(len(records), 1)
        self.assertEqual(set(records[0].keys()), set(COLUMNS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
