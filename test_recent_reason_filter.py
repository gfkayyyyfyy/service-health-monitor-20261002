#!/usr/bin/env python3
"""recent --reason 原因筛选功能的独立回归测试。

通过公开命令行入口（子进程运行 healthcheck.py）观察退出码与 JSON 输出，
全部数据放在独立临时 SQLite 数据库中，不依赖固定端口上的服务或已有历史
文件，临时资源在每个用例结束后释放。

验收固定样本（五条记录）：
- 目标 A：http://127.0.0.1:8765/health
- 目标 B：http://127.0.0.1:8765/ready（仅路径不同）
- id 1..5 依次保存：A 的 timeout、A 的 ok、A 的 timeout、B 的 timeout、A 的 ok；
- elapsed_ms 依次为 0、4、10、20、9 毫秒；
- ok 对应 success（http_status 200），timeout 对应 failure（http_status null）。

主用例：
    recent --url A --reason timeout --limit 2
返回 id 为 3、1 的完整记录；加 --summary 输出
{"count":2,"min_elapsed_ms":0,"max_elapsed_ms":10,"avg_elapsed_ms":5.0}。

在项目目录执行：
    python3 -m unittest test_recent_reason_filter
或：
    python3 test_recent_reason_filter.py
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

ALL_REASONS = ("ok", "http_status", "connection_error", "timeout")

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
                    f'SELECT * FROM "{name}"'
                ).fetchall()
        finally:
            conn.close()
    return files, tables


class RecentReasonFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-reasonfilter-"))
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

    # ---- 验收主用例：--url A --reason timeout --limit 2 → id [3, 1] ----

    def test_url_and_reason_timeout_limit_2(self):
        label = f"recent --url {URL_A!r} --reason timeout --limit 2"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--url", URL_A,
                          "--reason", "timeout", "--limit", "2")
        self.assertEqual(
            proc.returncode, 0,
            f"输入 {label}：期望退出码 0，实际 {proc.returncode}，"
            f"stderr={proc.stderr!r}",
        )
        self.assertEqual(proc.stderr, "")
        records = json.loads(proc.stdout)
        # A 的 timeout 记录为 id 1（0ms）、3（10ms）；倒序取前 2 即 3、1。
        # B 的 id 4 同为 timeout，但 url 不同，必须被排除；
        # A 的 ok（id 2、5）必须被排除——只看保存的 reason，不从状态推断。
        self.assertEqual(
            records, [REC3, REC1],
            f"输入 {label}：期望 id [3, 1] 的完整记录，"
            f"实际 id {[r['id'] for r in records]}，"
            f"完整输出={json.dumps(records, ensure_ascii=False)}",
        )
        for rec in records:
            self.assertEqual(set(rec.keys()), set(COLUMNS))
        self.assert_state_unchanged(before, label)

    def test_url_and_reason_timeout_limit_2_summary(self):
        label = f"recent --url {URL_A!r} --reason timeout --limit 2 --summary"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--url", URL_A,
                          "--reason", "timeout", "--limit", "2", "--summary")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        summary = json.loads(proc.stdout)
        # id 3（10ms）与 id 1（0ms）：零耗时计入，均值 (10+0)/2 = 5.0 不取整
        self.assertEqual(
            summary,
            {"count": 2, "min_elapsed_ms": 0,
             "max_elapsed_ms": 10, "avg_elapsed_ms": 5.0},
            f"输入 {label}：摘要不匹配，实际 {summary}",
        )
        self.assert_state_unchanged(before, label)

    # ---- 仅 --reason：跨全部目标按保存的 reason 精确筛选 ----

    def test_reason_timeout_across_all_urls(self):
        label = "recent --reason timeout"
        proc = run_recent(self.db, "--reason", "timeout")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        records = json.loads(proc.stdout)
        # 三条 timeout 记录（含 B 的 id 4）：id 4、3、1
        self.assertEqual(
            [r["id"] for r in records], [4, 3, 1],
            f"输入 {label}：期望 id [4, 3, 1]，"
            f"实际 {[r['id'] for r in records]}",
        )
        self.assertEqual({r["reason"] for r in records}, {"timeout"})

    def test_reason_ok_across_all_urls(self):
        proc = run_recent(self.db, "--reason", "ok")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = json.loads(proc.stdout)
        self.assertEqual(
            [r["id"] for r in records], [5, 2],
            "仅 reason ok：期望 id [5, 2]",
        )
        self.assertEqual({r["status"] for r in records}, {"success"})

    def test_reason_does_not_infer_from_status_or_http_status(self):
        # 样本中有三条 failure 记录，但它们保存的 reason 都是 timeout：
        # failure 状态或 http_status 字段都不能让 http_status/connection_error
        # 这两个 reason 命中任何记录。
        for reason in ("http_status", "connection_error"):
            with self.subTest(reason=reason):
                proc = run_recent(self.db, "--reason", reason)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(
                    proc.stdout, "[]\n",
                    f"--reason {reason} 必须按保存值精确匹配，"
                    f"不得从 status/http_status 推断",
                )
        # 对照：failure 状态确实有记录，证明空结果是 reason 精确匹配所致
        proc_f = run_recent(self.db, "--status", "failure")
        self.assertEqual(
            [r["id"] for r in json.loads(proc_f.stdout)], [4, 3, 1],
        )

    def test_all_four_reason_choices_each_match_saved_value(self):
        """四种合法 reason 各放一条记录，逐一精确命中，互不串值。"""
        db = self.tmp / "four-reasons.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute(SCHEMA_SQL)
        # id 1..4 分别为 ok / http_status / connection_error / timeout
        rows = [
            (1, URL_A, "2026-10-04T00:00:01+00:00", 1,
             "success", 200, "ok"),
            (2, URL_A, "2026-10-04T00:00:02+00:00", 2,
             "failure", 500, "http_status"),
            (3, URL_A, "2026-10-04T00:00:03+00:00", 3,
             "failure", None, "connection_error"),
            (4, URL_A, "2026-10-04T00:00:04+00:00", 4,
             "failure", None, "timeout"),
        ]
        conn.executemany(
            "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
            "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        conn.close()
        for rid, reason in enumerate(ALL_REASONS, start=1):
            with self.subTest(reason=reason):
                proc = run_recent(db, "--reason", reason)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                records = json.loads(proc.stdout)
                self.assertEqual(
                    [r["id"] for r in records], [rid],
                    f"--reason {reason} 应只精确命中 id {rid}",
                )
                self.assertEqual(records[0]["reason"], reason)
                proc_s = run_recent(db, "--reason", reason, "--summary")
                self.assertEqual(
                    json.loads(proc_s.stdout),
                    {"count": 1, "min_elapsed_ms": rid,
                     "max_elapsed_ms": rid, "avg_elapsed_ms": float(rid)},
                )

    # ---- 与 --url、--status 的交集 ----

    def test_reason_combined_with_url(self):
        # A 的 timeout 为 id 1、3
        proc_a = run_recent(self.db, "--url", URL_A, "--reason", "timeout")
        self.assertEqual(proc_a.returncode, 0, proc_a.stderr)
        self.assertEqual(
            [r["id"] for r in json.loads(proc_a.stdout)], [3, 1],
            "--url A --reason timeout：期望 id [3, 1]",
        )
        # B 只有 id 4 一条 timeout
        proc_b = run_recent(self.db, "--url", URL_B, "--reason", "timeout")
        self.assertEqual(
            [r["id"] for r in json.loads(proc_b.stdout)], [4],
            "--url B --reason timeout：期望 id [4]",
        )
        # B 没有 ok 记录：合法组合无匹配 → []
        proc_none = run_recent(self.db, "--url", URL_B, "--reason", "ok")
        self.assertEqual(proc_none.returncode, 0, proc_none.stderr)
        self.assertEqual(proc_none.stdout, "[]\n")

    def test_reason_combined_with_status(self):
        # timeout 在样本中全部是 failure：id 4、3、1
        proc = run_recent(self.db, "--status", "failure",
                          "--reason", "timeout")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            [r["id"] for r in json.loads(proc.stdout)], [4, 3, 1],
            "--status failure --reason timeout：期望 id [4, 3, 1]",
        )
        # 交集为空（ok 记录是 success；timeout 记录是 failure）
        for status, reason in (("success", "timeout"), ("failure", "ok")):
            with self.subTest(status=status, reason=reason):
                proc_empty = run_recent(
                    self.db, "--status", status, "--reason", reason
                )
                self.assertEqual(proc_empty.returncode, 0, proc_empty.stderr)
                self.assertEqual(proc_empty.stdout, "[]\n")
                proc_s = run_recent(
                    self.db, "--status", status, "--reason", reason,
                    "--summary",
                )
                self.assertEqual(json.loads(proc_s.stdout), EMPTY_SUMMARY)

    def test_reason_combined_with_url_and_status(self):
        # 三个条件同时满足：A failure timeout 为 id 3、1
        proc = run_recent(
            self.db, "--url", URL_A, "--status", "failure",
            "--reason", "timeout",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            [r["id"] for r in json.loads(proc.stdout)], [3, 1],
            "url+status+reason 三条件交集：期望 id [3, 1]",
        )
        # A success timeout 无交集
        proc_empty = run_recent(
            self.db, "--url", URL_A, "--status", "success",
            "--reason", "timeout",
        )
        self.assertEqual(proc_empty.stdout, "[]\n")

    def test_reason_limit_applies_after_filtering(self):
        # 先筛选（A 且 timeout：id 3、1）再倒序取 1 条 → id 3
        proc = run_recent(
            self.db, "--url", URL_A, "--reason", "timeout", "--limit", "1"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            [r["id"] for r in json.loads(proc.stdout)], [3],
            "limit 在筛选之后生效：期望仅 id 3",
        )

    def test_reason_default_limit_is_five(self):
        # 省略 --limit 时默认 5 条；样本中 timeout 恰好 3 条全部返回
        proc = run_recent(self.db, "--reason", "timeout")
        self.assertEqual(
            len(json.loads(proc.stdout)), 3,
            "默认 limit 5：3 条 timeout 记录应全部返回",
        )

    # ---- 省略 --reason：查询输出与新增该参数前完全一致 ----

    def test_omitting_reason_keeps_legacy_behavior(self):
        # 不带任何筛选：默认 limit 5 → 5、4、3、2、1
        proc = run_recent(self.db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            [r["id"] for r in json.loads(proc.stdout)], [5, 4, 3, 2, 1],
            "省略 --reason 应返回全部原因记录",
        )
        # 带 --url 但不带 --reason：timeout 与 ok 都返回
        proc_a = run_recent(self.db, "--url", URL_A)
        self.assertEqual(
            [r["id"] for r in json.loads(proc_a.stdout)], [5, 3, 2, 1],
            "省略 --reason 时 A 的 timeout 与 ok 记录都应返回",
        )
        # 带 --status 但不带 --reason：不因 reason 收窄
        proc_f = run_recent(self.db, "--status", "failure")
        self.assertEqual(
            [r["id"] for r in json.loads(proc_f.stdout)], [4, 3, 1],
        )
        # 摘要口径也不变：A 四条耗时 0、4、10、9 → 均值 23/4 = 5.75
        proc_s = run_recent(self.db, "--url", URL_A, "--summary")
        self.assertEqual(
            json.loads(proc_s.stdout),
            {"count": 4, "min_elapsed_ms": 0, "max_elapsed_ms": 10,
             "avg_elapsed_ms": 5.75},
            "省略 --reason 的摘要应统计 A 的全部 4 条记录",
        )

    # ---- 空结果：普通 [] / 摘要 count0 三 null，退出码 0，无副作用 ----

    def test_reason_filter_without_matching_records(self):
        label = f"recent --url {URL_NO_MATCH!r} --reason timeout"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--url", URL_NO_MATCH,
                          "--reason", "timeout")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(proc.stdout, "[]\n")
        self.assert_state_unchanged(before, label)

        proc_s = run_recent(self.db, "--url", URL_NO_MATCH,
                            "--reason", "timeout", "--summary")
        self.assertEqual(proc_s.returncode, 0, proc_s.stderr)
        self.assertEqual(proc_s.stderr, "")
        self.assertEqual(json.loads(proc_s.stdout), EMPTY_SUMMARY)

        # 合法 reason 值但本库没有任何该原因的记录
        proc_other = run_recent(self.db, "--reason", "http_status")
        self.assertEqual(proc_other.returncode, 0, proc_other.stderr)
        self.assertEqual(proc_other.stdout, "[]\n")

    def test_empty_results_when_db_or_table_missing(self):
        # 数据库与父目录都不存在
        missing_root = self.tmp / "does-not-exist"
        missing_db = missing_root / "nested" / "monitor.sqlite"
        for add_summary in (False, True):
            with self.subTest(case="missing-db", summary=add_summary):
                extra = ("--reason", "timeout")
                if add_summary:
                    extra += ("--summary",)
                before = snapshot_state(self.tmp, missing_db)
                proc = run_recent(missing_db, *extra)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")
                if add_summary:
                    self.assertEqual(json.loads(proc.stdout), EMPTY_SUMMARY)
                else:
                    self.assertEqual(proc.stdout, "[]\n")
                self.assert_state_unchanged(before, "缺库", missing_db)
        self.assertFalse(
            missing_root.exists(), "查询不应创建缺失的目录或数据库文件"
        )

        # 有效库但无 checks 表
        other_db = self.tmp / "other-only.sqlite"
        make_other_table_db(other_db)
        for add_summary in (False, True):
            with self.subTest(case="other-table", summary=add_summary):
                before = snapshot_state(self.tmp, other_db)
                extra = ("--reason", "ok")
                if add_summary:
                    extra += ("--summary",)
                proc = run_recent(other_db, *extra)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                if add_summary:
                    self.assertEqual(json.loads(proc.stdout), EMPTY_SUMMARY)
                else:
                    self.assertEqual(proc.stdout, "[]\n")
                self.assertEqual(snapshot_state(self.tmp, other_db), before)

        # checks 表存在但为空表
        empty_db = self.tmp / "empty.sqlite"
        make_empty_checks_db(empty_db)
        for add_summary in (False, True):
            with self.subTest(case="empty-checks", summary=add_summary):
                extra = ("--reason", "timeout")
                if add_summary:
                    extra += ("--summary",)
                proc = run_recent(empty_db, *extra)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                if add_summary:
                    self.assertEqual(json.loads(proc.stdout), EMPTY_SUMMARY)
                else:
                    self.assertEqual(proc.stdout, "[]\n")

    # ---- 历史表名大小写异写：reason 筛选结果一致 ----

    def test_reason_filter_works_with_case_variant_table(self):
        for table in ("CHECKS", "Checks"):
            with self.subTest(table=table):
                db = self.tmp / f"{table}.sqlite"
                build_sample_db(db, table_name=table)
                proc = run_recent(db, "--url", URL_A,
                                  "--reason", "timeout", "--limit", "2")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(
                    json.loads(proc.stdout), [REC3, REC1],
                    f"表 {table}：reason 筛选应与 checks 表结果一致",
                )

    # ---- 非法 --reason：退出码 2、stdout 空、stderr 指出 reason 与非法值 ----

    def assert_reason_rejected(self, proc, label):
        self.assertEqual(
            proc.returncode, 2,
            f"输入 {label}：期望退出码 2，实际 {proc.returncode}，"
            f"stdout={proc.stdout!r}",
        )
        self.assertEqual(proc.stdout, "", f"输入 {label}：stdout 应为空")
        self.assertTrue(
            proc.stderr.startswith("healthcheck: error:"),
            f"输入 {label}：stderr 应以固定错误前缀开头，"
            f"实际 {proc.stderr!r}",
        )
        self.assertIn(
            "reason", proc.stderr,
            f"输入 {label}：stderr 应指出 reason 参数错误，"
            f"实际 {proc.stderr!r}",
        )
        self.assertNotIn(
            "Traceback", proc.stderr,
            f"输入 {label}：stderr 不应包含 Python 回溯",
        )

    def test_invalid_reason_values_rejected(self):
        # 数据库刻意不存在：reason 错误必须在读取数据库前拒绝
        missing_db = self.tmp / "not-yet-created.sqlite"
        cases = [
            "OK", "Ok", "TIMEOUT", "Timeout", "http_Status",
            " timeout", "timeout ", "connection_error\n",
            "http", "status", "success", "failure",
            "connection-error", "http-status", "time out", "OK ",
            "", "  ",
        ]
        for value in cases:
            with self.subTest(value=value):
                before = snapshot_state(self.tmp, missing_db)
                proc = run_recent(missing_db, "--reason", value)
                self.assert_reason_rejected(
                    proc, f"recent --reason {value!r}"
                )
                # stderr 必须回显非法值（缺值情形另有专用用例）
                self.assertIn(
                    repr(value), proc.stderr,
                    f"输入 --reason {value!r}：stderr 应回显非法值，"
                    f"实际 {proc.stderr!r}",
                )
                # 拒绝前不得创建数据库文件或目录
                self.assertFalse(
                    missing_db.exists(),
                    f"非法 reason {value!r} 被拒绝时不应创建数据库文件",
                )
                self.assert_state_unchanged(
                    before, f"非法 reason {value!r}", missing_db
                )

    def test_bare_reason_without_value_rejected(self):
        missing_db = self.tmp / "bare-reason.sqlite"
        before = snapshot_state(self.tmp, missing_db)
        # 子进程参数中裸 --reason 后不接值
        proc = run_recent(missing_db, "--reason")
        self.assert_reason_rejected(proc, "recent --reason（缺少值）")
        self.assertIn(
            "缺少值", proc.stderr,
            f"裸 --reason：stderr 应说明缺少值，实际 {proc.stderr!r}",
        )
        self.assertFalse(
            missing_db.exists(), "裸 --reason 被拒绝时不应创建数据库文件"
        )
        self.assert_state_unchanged(before, "裸 --reason", missing_db)

    # ---- 校验顺序：status → URL → reason → 数据库路径 ----

    def test_validation_order_status_url_reason_db(self):
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir()

        # 非法 status + 非法 reason + 目录库：只报 status 错误
        before = snapshot_state(self.tmp, db_dir)
        proc = run_recent(db_dir, "--status", "nope", "--reason", "bad")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("status", proc.stderr)
        self.assertNotIn("reason 参数错误", proc.stderr)
        self.assertNotIn("目录", proc.stderr)
        self.assert_state_unchanged(before, "status 优先", db_dir)

        # status 合法后，非法 URL + 非法 reason：只报 URL 错误
        proc_url = run_recent(
            db_dir, "--status", "success", "--reason", "bad",
            "--url", "https://127.0.0.1:8765/x",
        )
        self.assertEqual(proc_url.returncode, 2)
        self.assertEqual(proc_url.stdout, "")
        self.assertIn("协议", proc_url.stderr)
        self.assertNotIn("reason 参数错误", proc_url.stderr)
        self.assertNotIn("目录", proc_url.stderr)

        # status、URL 都合法后，非法 reason 优先于目录路径错误
        proc_reason = run_recent(
            db_dir, "--status", "success",
            "--url", URL_A, "--reason", "bad",
        )
        self.assertEqual(proc_reason.returncode, 2)
        self.assertEqual(proc_reason.stdout, "")
        self.assertIn("reason", proc_reason.stderr)
        self.assertIn("'bad'", proc_reason.stderr)
        self.assertNotIn("目录", proc_reason.stderr)

        # 裸 --reason 缺值同样晚于 status、URL 校验、早于数据库访问
        proc_missing = run_recent(
            db_dir, "--url", URL_A, "--status", "failure", "--reason",
        )
        self.assertEqual(proc_missing.returncode, 2)
        self.assertEqual(proc_missing.stdout, "")
        self.assertIn("reason", proc_missing.stderr)
        self.assertIn("缺少值", proc_missing.stderr)
        self.assertNotIn("目录", proc_missing.stderr)

        # 全部参数合法时，目录路径错误照常报告
        proc_dir = run_recent(db_dir, "--reason", "timeout")
        self.assertEqual(proc_dir.returncode, 2)
        self.assertEqual(proc_dir.stdout, "")
        self.assertIn("目录", proc_dir.stderr)

    # ---- 只读、无副作用：可读不可写库可查，不产生旁路文件、不发请求 ----

    def test_readonly_database_is_queryable(self):
        import os
        import stat
        ro_db = self.tmp / "readonly.sqlite"
        shutil.copy(self.db, ro_db)
        os.chmod(ro_db, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        try:
            proc = run_recent(ro_db, "--url", URL_A,
                              "--reason", "timeout", "--limit", "2")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                [r["id"] for r in json.loads(proc.stdout)], [3, 1],
                "可读不可写的数据库也应能完成 reason 筛选查询",
            )
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

    def test_reason_query_makes_no_network_requests(self):
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
            for extra in (
                ("--reason", "timeout"),
                ("--url", URL_A, "--reason", "ok"),
                ("--url", URL_A, "--status", "failure",
                 "--reason", "timeout", "--summary"),
            ):
                proc = run_recent(self.db, *extra)
                self.assertEqual(proc.returncode, 0, proc.stderr)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(
            hits, [],
            f"reason 筛选查询不应发出任何网络请求，实际收到 {hits}",
        )

    # ---- check 行为不受影响 ----

    def test_check_subcommand_does_not_accept_reason(self):
        proc = run_cli(self.db, "check", "--url", URL_A, "--reason", "ok")
        # argparse 无法识别该参数：退出码 2、stdout 为空、无 Python 回溯
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
