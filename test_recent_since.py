#!/usr/bin/env python3
"""recent --since 起始时刻筛选功能的独立回归测试。

仅验证 --since 的已有行为：通过公开命令行入口（子进程运行 healthcheck.py）
核对退出码、stdout 与 stderr，样本数据放在独立临时 SQLite 数据库中，只用
Python 3 标准库，不依赖演示服务或公网，临时资源在每个用例结束后释放。
不修改产品代码、表结构与文档；check 的探测落库与省略 --since 的查询行为
保持原样（省略 --since 时不做任何 checked_at 时间校验）。

验收固定样本（四条记录）：
- 目标 A：http://127.0.0.1:8765/health
- 目标 B：http://127.0.0.1:8765/ready（仅路径不同）
- id 1、2 属于 A，checked_at 分别为 2026-10-04T00:00:02Z 与
  2026-10-04T00:00:02.000000+00:00（同一时刻的两种写法）；
- id 3 属于 B，checked_at 为 2026-10-04T00:00:04Z；
- id 4 属于 A，checked_at 为 2026-10-04T00:00:01Z；
- elapsed_ms 依次为 0、10、20、30，全部是 failure/timeout，http_status 为 null。

主用例（对 A 同用 --status failure、--reason timeout、
--since 2026-10-04T00:00:02Z、--limit 2）：
- 普通输出返回 id 顺序为 2、1 的完整原始记录，checked_at 字符串不改写；
- 加 --summary 输出 {"count":2,"min_elapsed_ms":0,
  "max_elapsed_ms":10,"avg_elapsed_ms":5.0}；
- since 改用 .000000+00:00 等价写法结果相同；推进一微秒后普通输出 []，
  摘要 count 为 0、其余三项为 null。以上均退出 0、stderr 为空。

在项目目录执行：
    python3 -m unittest test_recent_since
或：
    python3 test_recent_since.py
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

URL_A = "http://127.0.0.1:8765/health"
URL_B = "http://127.0.0.1:8765/ready"

SINCE_EXACT_Z = "2026-10-04T00:00:02Z"
SINCE_EXACT_OFFSET = "2026-10-04T00:00:02.000000+00:00"
SINCE_ONE_MICROSECOND_AFTER = "2026-10-04T00:00:02.000001Z"
SINCE_ONE_MICROSECOND_AFTER_OFFSET = "2026-10-04T00:00:02.000001+00:00"

EMPTY_SUMMARY = {
    "count": 0,
    "min_elapsed_ms": None,
    "max_elapsed_ms": None,
    "avg_elapsed_ms": None,
}

# ---- 主样本：四条固定记录 -------------------------------------------------
# id 1、2 属于 A 且为同一时刻（Z 与 +00:00 两种写法），id 3 属于 B 且更新，
# id 4 属于 A 但早一秒；全部 failure/timeout、http_status 为 null。
REC1 = {
    "id": 1, "url": URL_A,
    "checked_at": "2026-10-04T00:00:02Z",
    "elapsed_ms": 0, "status": "failure", "http_status": None,
    "reason": "timeout",
}
REC2 = {
    "id": 2, "url": URL_A,
    "checked_at": "2026-10-04T00:00:02.000000+00:00",
    "elapsed_ms": 10, "status": "failure", "http_status": None,
    "reason": "timeout",
}
REC3 = {
    "id": 3, "url": URL_B,
    "checked_at": "2026-10-04T00:00:04Z",
    "elapsed_ms": 20, "status": "failure", "http_status": None,
    "reason": "timeout",
}
REC4 = {
    "id": 4, "url": URL_A,
    "checked_at": "2026-10-04T00:00:01Z",
    "elapsed_ms": 30, "status": "failure", "http_status": None,
    "reason": "timeout",
}

ALL_RECORDS = [REC1, REC2, REC3, REC4]

# 主用例固定的其余筛选
COMMON_FILTERS = ("--url", URL_A, "--status", "failure", "--reason", "timeout")


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


def build_sample_db(path, records=None):
    """创建包含指定固定记录（默认主样本四条）的临时数据库。"""
    conn = sqlite3.connect(str(path))
    conn.execute(SCHEMA_SQL)
    insert_records(conn, ALL_RECORDS if records is None else records)
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


class RecentSinceTests(unittest.TestCase):
    """主样本四条记录上的 --since 时刻比较、摘要与错误处理。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-since-"))
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

    def assert_ok_silent(self, proc, label):
        """成功场景：退出码 0、stderr 为空、单行 JSON 输出。"""
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
            f"输入 {label}：输出应为单行 JSON，实际 {proc.stdout!r}",
        )

    def assert_rejected(self, proc, label, keyword):
        """参数错误场景：退出码 2、stdout 为空、stderr 指出 since 与原因。"""
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
            "since", proc.stderr,
            f"输入 {label}：stderr 应指出 since 参数错误，"
            f"实际 {proc.stderr!r}",
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

    # ---- 主用例：时刻比较而非字符串比较，输出完整原始记录 ----

    def test_since_z_returns_full_raw_records_in_id_desc_order(self):
        label = (
            f"recent {' '.join(COMMON_FILTERS)} "
            f"--since {SINCE_EXACT_Z} --limit 2"
        )
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, *COMMON_FILTERS,
            "--since", SINCE_EXACT_Z, "--limit", "2",
        )
        self.assert_ok_silent(proc, label)
        records = json.loads(proc.stdout)
        # 按时刻比较且边界含等号：
        # - id 3（B）虽更新，但被 --url A 排除；
        # - id 4（A，00:00:01Z）早于起点，被 --since 排除；
        # - id 1 的 Z 写法与 id 2 的 +00:00 写法为同一时刻，均保留；
        # 倒序取前 2 即 id 2、1。
        self.assertEqual(
            [r["id"] for r in records], [2, 1],
            f"输入 {label}：期望 id 顺序 [2, 1]，"
            f"实际 {[r['id'] for r in records]}",
        )
        # 完整原始记录：字段齐全、时间字符串原样输出不改写
        self.assertEqual(
            records, [REC2, REC1],
            f"输入 {label}：期望完整原始记录（时间字符串不改写），"
            f"实际 {json.dumps(records, ensure_ascii=False)}",
        )
        for rec in records:
            self.assertEqual(set(rec.keys()), set(COLUMNS))
        self.assertEqual(records[0]["checked_at"], REC2["checked_at"])
        self.assertEqual(records[1]["checked_at"], REC1["checked_at"])
        self.assert_state_unchanged(before, label)

    def test_since_summary_counts_min_max_avg(self):
        label = "主用例加 --summary"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(
            self.db, *COMMON_FILTERS,
            "--since", SINCE_EXACT_Z, "--limit", "2", "--summary",
        )
        self.assert_ok_silent(proc, label)
        # id 2（10ms）与 id 1（0ms）：count 2、最小 0、最大 10、均值 5.0
        self.assertEqual(
            json.loads(proc.stdout),
            {"count": 2, "min_elapsed_ms": 0,
             "max_elapsed_ms": 10, "avg_elapsed_ms": 5.0},
            f"输入 {label}：摘要不匹配，实际 {proc.stdout!r}",
        )
        self.assert_state_unchanged(before, label)

    def test_offset_equivalent_since_gives_identical_result(self):
        """起点 .000000+00:00 与 Z 是同一时刻，普通输出与摘要逐字节相同。"""
        z_args = ("--since", SINCE_EXACT_Z, "--limit", "2")
        offset_args = ("--since", SINCE_EXACT_OFFSET, "--limit", "2")
        proc_z = run_recent(self.db, *COMMON_FILTERS, *z_args)
        proc_o = run_recent(self.db, *COMMON_FILTERS, *offset_args)
        self.assert_ok_silent(proc_z, "Z 写法")
        self.assert_ok_silent(proc_o, "+00:00 等价写法")
        self.assertEqual(
            proc_o.stdout, proc_z.stdout,
            "起点 Z 与 .000000+00:00 等价：普通输出应完全相同",
        )
        # 关键：id 1 保存的是 Z 写法、id 2 保存的是 +00:00 写法，
        # 用与记录相反的时区写法给起点仍同时命中二者——按时刻而非字符串比较。
        records = json.loads(proc_o.stdout)
        self.assertEqual([r["id"] for r in records], [2, 1])

        proc_zs = run_recent(
            self.db, *COMMON_FILTERS, *z_args, "--summary"
        )
        proc_os = run_recent(
            self.db, *COMMON_FILTERS, *offset_args, "--summary"
        )
        self.assert_ok_silent(proc_zs, "Z 写法摘要")
        self.assert_ok_silent(proc_os, "+00:00 等价写法摘要")
        self.assertEqual(
            proc_os.stdout, proc_zs.stdout,
            "起点 Z 与 .000000+00:00 等价：摘要输出应完全相同",
        )

    def test_one_microsecond_later_excludes_boundary_records(self):
        """起点推进一微秒：边界两条记录均严格早于起点（时刻比较），结果为空。

        字符串比较会因 'Z'(0x5A) > '.'(0x2E) 而误纳 id 1 的
        "00:00:02Z"；这里普通输出必须为 []，才能证明按时刻比较。
        Z 与 +00:00 两种微秒起点行为一致。
        """
        for since in (SINCE_ONE_MICROSECOND_AFTER,
                      SINCE_ONE_MICROSECOND_AFTER_OFFSET):
            with self.subTest(since=since):
                label = f"recent ... --since {since} --limit 2"
                before = snapshot_state(self.tmp, self.db)
                proc = run_recent(
                    self.db, *COMMON_FILTERS,
                    "--since", since, "--limit", "2",
                )
                self.assert_ok_silent(proc, label)
                self.assertEqual(
                    proc.stdout, "[]\n",
                    f"输入 {label}：推进一微秒后不应有记录，"
                    f"实际 {proc.stdout!r}",
                )
                proc_s = run_recent(
                    self.db, *COMMON_FILTERS,
                    "--since", since, "--limit", "2", "--summary",
                )
                self.assert_ok_silent(proc_s, label + " --summary")
                self.assertEqual(
                    json.loads(proc_s.stdout), EMPTY_SUMMARY,
                    f"输入 {label} --summary：期望空摘要，"
                    f"实际 {proc_s.stdout!r}",
                )
                self.assert_state_unchanged(before, label)

    # ---- 省略 --since：不增加时间条件，id 4（早一秒）照常返回 ----

    def test_omitting_since_keeps_legacy_query_behavior(self):
        label = f"recent {' '.join(COMMON_FILTERS)} --limit 3（省略 --since）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, *COMMON_FILTERS, "--limit", "3")
        self.assert_ok_silent(proc, label)
        records = json.loads(proc.stdout)
        # 无时间条件：A 的 failure/timeout 为 id 4、2、1，按 id 倒序取 3 条
        self.assertEqual(
            records, [REC4, REC2, REC1],
            f"输入 {label}：省略 --since 时 id 4 必须照常返回，"
            f"实际 id {[r['id'] for r in records]}",
        )
        self.assert_state_unchanged(before, label)

        # 完全不带任何筛选：默认 limit 5，四条全部返回
        proc_all = run_recent(self.db)
        self.assert_ok_silent(proc_all, "recent（无任何筛选）")
        self.assertEqual(
            [r["id"] for r in json.loads(proc_all.stdout)], [4, 3, 2, 1],
        )

    # ---- 非法 --since：退出码 2、stdout 空、stderr 指出 since 与原因 ----

    def test_invalid_since_values_rejected(self):
        cases = [
            ("", "值不能为空"),
            ("  ", "前后不允许有空白"),
            ("2026-10-04T00:00:02", "缺少时区"),
            ("2026-10-04T00:00:02+08:00", "仅接受 UTC"),
            ("2026-02-30T00:00:00Z", "真实存在"),
            ("2026-10-04T25:00:00Z", "真实存在"),
            ("2026-10-04T00:00:02.0000000Z", "格式必须为"),
            ("2026-10-04T00:00:02.0000000+00:00", "格式必须为"),
            ("2026-10-04 00:00:02Z", "格式必须为"),
        ]
        for value, keyword in cases:
            with self.subTest(value=value):
                label = f"recent --since {value!r}"
                before = snapshot_state(self.tmp, self.db)
                proc = run_recent(self.db, "--since", value)
                self.assert_rejected(proc, label, keyword)
                self.assertIn(
                    repr(value), proc.stderr,
                    f"输入 {label}：stderr 应回显非法值，"
                    f"实际 {proc.stderr!r}",
                )
                self.assert_state_unchanged(before, label)

    def test_bare_since_without_value_rejected(self):
        label = "recent --since（缺少值）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--since")
        self.assert_rejected(proc, label, "缺少值")
        self.assert_state_unchanged(before, label)

    def test_invalid_since_reported_before_directory_db(self):
        """非法起点配合目录数据库路径：仍优先报告起点错误，不读数据库。"""
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir()
        cases = [
            (["--since"], "缺少值"),
            (["--since", "2026-10-04T00:00:02"], "缺少时区"),
            (["--since", "2026-10-04T00:00:02+08:00"], "仅接受 UTC"),
            (["--since", "2026-02-30T00:00:00Z"], "真实存在"),
            (["--since", "2026-10-04T00:00:02.0000000Z"], "格式必须为"),
        ]
        for extra, keyword in cases:
            with self.subTest(extra=extra):
                label = f"目录库路径 + recent {' '.join(extra)}"
                before = snapshot_state(self.tmp, db_dir)
                proc = run_recent(db_dir, *extra)
                self.assert_rejected(proc, label, keyword)
                self.assertNotIn(
                    "目录", proc.stderr,
                    f"输入 {label}：非法 --since 应优先于目录路径错误，"
                    f"实际 {proc.stderr!r}",
                )
                self.assert_state_unchanged(before, label, db_dir)

    # ---- 表结构保持兼容，只读查询不产生旁路文件 ----

    def test_schema_remains_compatible_and_sidecar_free(self):
        conn = sqlite3.connect(str(self.db))
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(checks)")]
        finally:
            conn.close()
        self.assertEqual(cols, COLUMNS)

        proc = run_recent(
            self.db, *COMMON_FILTERS,
            "--since", SINCE_EXACT_Z, "--limit", "2", "--summary",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

        conn = sqlite3.connect(str(self.db))
        try:
            cols_after = [
                row[1] for row in conn.execute("PRAGMA table_info(checks)")
            ]
            rows_after = conn.execute(
                "SELECT id, url, checked_at, elapsed_ms, status, "
                "http_status, reason FROM checks ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(cols_after, COLUMNS)
        self.assertEqual(
            rows_after,
            [
                tuple(r[k] for k in COLUMNS) for r in ALL_RECORDS
            ],
            "查询前后记录应完全一致",
        )
        sidecars = [
            p.name for p in self.tmp.glob("monitor.sqlite*")
            if p.name != "monitor.sqlite"
        ]
        self.assertEqual(
            sidecars, [],
            f"只读查询不应产生 -wal/-journal 旁路文件：{sidecars}",
        )


# 独立数据：含非法 checked_at 的记录 ---------------------------------------
# id 1：A，failure/timeout，checked_at 非法（符合其余筛选，最旧）；
# id 2、3：A，failure/timeout，时间合法（保证 id 1 排在 limit 2 之外）；
# id 4：B，failure/timeout，checked_at 非法（被 --url A 排除）；
# id 5：A，success/ok，时间合法（被 status/reason 排除 id 1 时不受影响）。
BAD_REC1 = {
    "id": 1, "url": URL_A, "checked_at": "not-a-timestamp",
    "elapsed_ms": 0, "status": "failure", "http_status": None,
    "reason": "timeout",
}
BAD_REC2 = {
    "id": 2, "url": URL_A,
    "checked_at": "2026-10-04T00:00:02Z",
    "elapsed_ms": 10, "status": "failure", "http_status": None,
    "reason": "timeout",
}
BAD_REC3 = {
    "id": 3, "url": URL_A,
    "checked_at": "2026-10-04T00:00:03Z",
    "elapsed_ms": 20, "status": "failure", "http_status": None,
    "reason": "timeout",
}
BAD_REC4 = {
    "id": 4, "url": URL_B, "checked_at": "also-bad",
    "elapsed_ms": 5, "status": "failure", "http_status": None,
    "reason": "timeout",
}
BAD_REC5 = {
    "id": 5, "url": URL_A,
    "checked_at": "2026-10-04T00:00:05Z",
    "elapsed_ms": 7, "status": "success", "http_status": 200,
    "reason": "ok",
}

BAD_RECORDS = [BAD_REC1, BAD_REC2, BAD_REC3, BAD_REC4, BAD_REC5]


class RecentSinceBadCheckedAtTests(unittest.TestCase):
    """--since 生效时对符合其余筛选记录的 checked_at 全量校验。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-since-bad-"))
        self.db = self.tmp / "bad-checked-at.sqlite"
        build_sample_db(self.db, BAD_RECORDS)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_state_unchanged(self, before, label, db_path=None):
        db_path = self.db if db_path is None else pathlib.Path(db_path)
        after = snapshot_state(self.tmp, db_path)
        self.assertEqual(after[0], before[0], f"{label}: 文件集合发生变化")
        self.assertEqual(after[1], before[1], f"{label}: 表或记录发生变化")

    def assert_bad_record_rejected(self, proc, label, record_id):
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
            f"输入 {label}：stderr 前缀错误，实际 {proc.stderr!r}",
        )
        self.assertIn(
            f"id={record_id}", proc.stderr,
            f"输入 {label}：stderr 应指出非法记录 id={record_id}，"
            f"实际 {proc.stderr!r}",
        )
        self.assertIn(
            "checked_at", proc.stderr,
            f"输入 {label}：stderr 应指出 checked_at 格式问题，"
            f"实际 {proc.stderr!r}",
        )
        self.assertNotIn("Traceback", proc.stderr)

    def test_bad_checked_at_beyond_limit_still_rejected(self):
        """符合其余筛选的非法记录即使排在 limit 之外也退出 2（普通+摘要）。"""
        args = [
            "--url", URL_A, "--status", "failure", "--reason", "timeout",
            "--since", "2026-10-04T00:00:00Z", "--limit", "2",
        ]
        # limit 2 本只会返回 id 3、2；id 1 位于 limit 之外，
        # 但校验在 limit 截取之前针对全部符合其余筛选的记录完成。
        label = f"recent {' '.join(args)}"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, *args)
        self.assert_bad_record_rejected(proc, label, 1)
        self.assertIn(repr("not-a-timestamp"), proc.stderr)
        self.assert_state_unchanged(before, label)

        proc_s = run_recent(self.db, *args, "--summary")
        self.assert_bad_record_rejected(proc_s, label + " --summary", 1)
        self.assert_state_unchanged(before, label + " --summary")

    def test_bad_checked_at_detected_per_url_filter(self):
        """切到 B：id 4 的非法 checked_at 同样被指出（id 1 被 url 排除）。"""
        args = [
            "--url", URL_B, "--status", "failure", "--reason", "timeout",
            "--since", "2026-10-04T00:00:00Z",
        ]
        label = f"recent {' '.join(args)}"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, *args)
        self.assert_bad_record_rejected(proc, label, 4)
        self.assertIn(repr("also-bad"), proc.stderr)
        self.assert_state_unchanged(before, label)

    def test_bad_records_excluded_by_other_filters_are_ignored(self):
        """被其他筛选排除的坏记录不影响查询：status/reason 排除 id 1、4。"""
        # 只查 A 的 success/ok：SQL 筛选后只剩 id 5（时间合法），
        # id 1（timeout）、id 4（B）虽有非法 checked_at，均已被排除。
        args = [
            "--url", URL_A, "--status", "success", "--reason", "ok",
            "--since", "2026-10-04T00:00:00Z",
        ]
        label = f"recent {' '.join(args)}"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, *args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        records = json.loads(proc.stdout)
        self.assertEqual(
            [r["id"] for r in records], [5],
            f"输入 {label}：被其他筛选排除的坏记录不应影响查询，"
            f"实际 {[r['id'] for r in records]}",
        )
        self.assert_state_unchanged(before, label)

        # B 上 success/ok 无记录：B 的坏记录 id 4 被 status/reason 排除，
        # 查询正常返回空而不是报 id 4 的时间格式错误。
        args_empty = [
            "--url", URL_B, "--status", "success", "--reason", "ok",
            "--since", "2026-10-04T00:00:00Z",
        ]
        proc_empty = run_recent(self.db, *args_empty)
        self.assertEqual(proc_empty.returncode, 0, proc_empty.stderr)
        self.assertEqual(proc_empty.stderr, "")
        self.assertEqual(proc_empty.stdout, "[]\n")

    def test_omitting_since_skips_checked_at_validation(self):
        """省略 --since 时不增加时间校验：非法 checked_at 原样返回。"""
        # limit 2：id 1 在 limit 之外，查询正常
        args = [
            "--url", URL_A, "--status", "failure", "--reason", "timeout",
            "--limit", "2",
        ]
        label = f"recent {' '.join(args)}（省略 --since）"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, *args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(
            [r["id"] for r in json.loads(proc.stdout)], [3, 2],
            f"输入 {label}",
        )
        self.assert_state_unchanged(before, label)

        # 不带 limit：非法 id 1 也按原始字符串返回，不报错、不改写
        proc_all = run_recent(
            self.db,
            "--url", URL_A, "--status", "failure", "--reason", "timeout",
        )
        self.assertEqual(proc_all.returncode, 0, proc_all.stderr)
        self.assertEqual(proc_all.stderr, "")
        records = json.loads(proc_all.stdout)
        self.assertEqual(
            [r["id"] for r in records], [3, 2, 1],
            "省略 --since：非法 checked_at 记录也应原样返回",
        )
        self.assertEqual(records[2]["checked_at"], "not-a-timestamp")

        # 完全省略筛选与 --since：B 的坏记录 id 4 同样原样返回
        proc_plain = run_recent(self.db)
        self.assertEqual(proc_plain.returncode, 0, proc_plain.stderr)
        self.assertEqual(
            [r["id"] for r in json.loads(proc_plain.stdout)],
            [5, 4, 3, 2, 1],
        )


class CheckProbePersistenceUnchangedTests(unittest.TestCase):
    """check 的探测落库保持不变，且落库时间能被 --since 正常读取。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-since-check-"))
        self.db = self.tmp / "from-check.sqlite"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_check_then_recent_with_and_without_since(self):
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler
        )
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{port}/"
            proc_check = run_cli(self.db, "check", "--url", url)
            self.assertEqual(proc_check.returncode, 0, proc_check.stderr)
            written = json.loads(proc_check.stdout)
            self.assertEqual(written["url"], url)
            self.assertEqual(written["status"], "success")
            self.assertEqual(written["reason"], "ok")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        # 省略 --since：照常查到 check 落库的记录
        proc_plain = run_recent(self.db, "--url", url)
        self.assertEqual(proc_plain.returncode, 0, proc_plain.stderr)
        records = json.loads(proc_plain.stdout)
        self.assertEqual([r["id"] for r in records], [written["id"]])
        self.assertEqual(
            records[0]["checked_at"], written["checked_at"],
            "recent 输出应原样使用 check 落库的 checked_at 字符串",
        )

        # check 落库的 UTC 时刻合法：很早的起点命中、很晚的起点不命中
        proc_past = run_recent(
            self.db, "--url", url, "--since", "2000-01-01T00:00:00Z"
        )
        self.assertEqual(proc_past.returncode, 0, proc_past.stderr)
        self.assertEqual(proc_past.stderr, "")
        self.assertEqual(
            [r["id"] for r in json.loads(proc_past.stdout)], [written["id"]],
        )
        proc_future = run_recent(
            self.db, "--url", url, "--since", "2099-01-01T00:00:00Z"
        )
        self.assertEqual(proc_future.returncode, 0, proc_future.stderr)
        self.assertEqual(proc_future.stderr, "")
        self.assertEqual(proc_future.stdout, "[]\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
