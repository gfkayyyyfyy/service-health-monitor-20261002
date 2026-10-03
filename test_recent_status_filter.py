#!/usr/bin/env python3
"""recent --status 状态筛选功能的独立回归测试。

通过公开命令行入口（子进程运行 healthcheck.py）观察退出码与 JSON 输出，
全部数据放在独立临时 SQLite 数据库中，不依赖固定端口上的服务或已有历史
文件，临时资源在每个用例结束后释放。

验收固定样本（五条记录）：
- 目标 A：http://127.0.0.1:8765/health?detail=1
- 目标 B：http://127.0.0.1:8765/health?detail=2（仅 detail 值不同）
- id 1..5 的目标依次为 A、A、B、A、A；
- 状态依次为 failure、success、failure、failure、success；
- elapsed_ms 依次为 0、2、7、3、9。

在项目目录执行：
    python3 -m unittest test_recent_status_filter
或：
    python3 test_recent_status_filter.py
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

URL_A = "http://127.0.0.1:8765/health?detail=1"
URL_B = "http://127.0.0.1:8765/health?detail=2"
URL_NO_MATCH = "http://127.0.0.1:8765/no-such-path?x=9"

# id 1..5：A、A、B、A、A；failure、success、failure、failure、success；
# elapsed_ms：0、2、7、3、9。失败原因各不相同以证明不区分失败原因。
REC1 = {
    "id": 1, "url": URL_A,
    "checked_at": "2026-10-04T00:00:01.000000+00:00",
    "elapsed_ms": 0, "status": "failure", "http_status": None,
    "reason": "connection_error",
}
REC2 = {
    "id": 2, "url": URL_A,
    "checked_at": "2026-10-04T00:00:02.000000+00:00",
    "elapsed_ms": 2, "status": "success", "http_status": 200, "reason": "ok",
}
REC3 = {
    "id": 3, "url": URL_B,
    "checked_at": "2026-10-04T00:00:03.000000+00:00",
    "elapsed_ms": 7, "status": "failure", "http_status": 500,
    "reason": "http_status",
}
REC4 = {
    "id": 4, "url": URL_A,
    "checked_at": "2026-10-04T00:00:04.000000+00:00",
    "elapsed_ms": 3, "status": "failure", "http_status": None,
    "reason": "timeout",
}
REC5 = {
    "id": 5, "url": URL_A,
    "checked_at": "2026-10-04T00:00:05.000000+00:00",
    "elapsed_ms": 9, "status": "success", "http_status": 204, "reason": "ok",
}

ALL_RECORDS = [REC1, REC2, REC3, REC4, REC5]

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


def build_sample_db(path):
    """创建包含验收固定样本 5 条记录的临时数据库。"""
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


class RecentStatusFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-statusfilter-"))
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

    # ---- 验收主用例：--url A --status failure --limit 2 → id [4, 1] ----

    def test_url_and_status_failure_limit_2(self):
        label = f"recent --url {URL_A!r} --status failure --limit 2"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--url", URL_A,
                          "--status", "failure", "--limit", "2")
        self.assertEqual(
            proc.returncode, 0,
            f"输入 {label}：期望退出码 0，实际 {proc.returncode}，"
            f"stderr={proc.stderr!r}",
        )
        self.assertEqual(proc.stderr, "")
        records = json.loads(proc.stdout)
        # A 的 failure 记录为 id 1（0ms）、4（3ms）；倒序取前 2 即 4、1。
        # B 的 id 3 虽同为 failure，但 url 不同，必须被排除；
        # A 的 success（id 2、5）必须被排除。
        self.assertEqual(
            records, [REC4, REC1],
            f"输入 {label}：期望 id [4, 1] 的完整记录，"
            f"实际 id {[r['id'] for r in records]}，"
            f"完整输出={json.dumps(records, ensure_ascii=False)}",
        )
        self.assert_state_unchanged(before, label)

    def test_url_and_status_failure_limit_2_summary(self):
        label = f"recent --url {URL_A!r} --status failure --limit 2 --summary"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--url", URL_A,
                          "--status", "failure", "--limit", "2", "--summary")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        summary = json.loads(proc.stdout)
        # id 4（3ms）与 id 1（0ms）：零耗时计入，均值 (3+0)/2 = 1.5 不取整
        self.assertEqual(
            summary,
            {"count": 2, "min_elapsed_ms": 0,
             "max_elapsed_ms": 3, "avg_elapsed_ms": 1.5},
            f"输入 {label}：摘要不匹配，实际 {summary}",
        )
        self.assert_state_unchanged(before, label)

    # ---- 仅 --status：跨全部目标按状态筛选 ----

    def test_status_failure_across_all_urls(self):
        label = "recent --status failure"
        proc = run_recent(self.db, "--status", "failure", "--limit", "10")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        records = json.loads(proc.stdout)
        # 三条失败记录（不区分失败原因）：id 4、3、1，B 的 id 3 也应包含
        self.assertEqual(
            [r["id"] for r in records], [4, 3, 1],
            f"输入 {label}：期望 id [4, 3, 1]，"
            f"实际 {[r['id'] for r in records]}",
        )
        self.assertEqual(
            {r["status"] for r in records}, {"failure"},
            f"输入 {label}：结果只能含 failure 记录",
        )
        self.assertEqual(
            {r["reason"] for r in records},
            {"connection_error", "timeout", "http_status"},
            f"输入 {label}：筛选只看 status，三种失败原因都应出现",
        )

    def test_status_success_across_all_urls(self):
        proc = run_recent(self.db, "--status", "success", "--limit", "10")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            [r["id"] for r in json.loads(proc.stdout)], [5, 2],
            "仅 status success：期望 id [5, 2]",
        )

    def test_status_combined_with_url_success(self):
        # A 的 success 为 id 2、5；默认 limit 5 不截断
        proc = run_recent(self.db, "--url", URL_A, "--status", "success")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            [r["id"] for r in json.loads(proc.stdout)], [5, 2],
            "--url A --status success：期望 id [5, 2]",
        )
        # B 的 failure（id 3）用 success 查不到
        proc_b = run_recent(self.db, "--url", URL_B, "--status", "success")
        self.assertEqual(proc_b.stdout, "[]\n")

    def test_status_limit_applies_after_filtering(self):
        # 先筛选（A 且 failure：id 4、1）再倒序取 1 条 → id 4
        proc = run_recent(
            self.db, "--url", URL_A, "--status", "failure", "--limit", "1"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            [r["id"] for r in json.loads(proc.stdout)], [4],
            "limit 在筛选之后生效：期望仅 id 4",
        )

    # ---- 省略 --status：两种查询输出与原先完全一致 ----

    def test_omitting_status_keeps_legacy_behavior(self):
        # 不带 --status：全部状态、全部目标，默认 limit 5 → 5、4、3、2、1
        proc = run_recent(self.db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            [r["id"] for r in json.loads(proc.stdout)], [5, 4, 3, 2, 1],
            "省略 --status 应返回全部状态记录",
        )
        # 带 --url 但不带 --status：成功失败都返回
        proc_a = run_recent(self.db, "--url", URL_A)
        self.assertEqual(
            [r["id"] for r in json.loads(proc_a.stdout)], [5, 4, 2, 1],
            "省略 --status 时 A 的成功与失败记录都应返回",
        )
        # 摘要口径也不变：A 四条耗时 9、3、2、0
        proc_s = run_recent(self.db, "--url", URL_A, "--summary")
        self.assertEqual(
            json.loads(proc_s.stdout),
            {"count": 4, "min_elapsed_ms": 0, "max_elapsed_ms": 9,
             "avg_elapsed_ms": 3.5},
            "省略 --status 的摘要应统计 A 的全部 4 条记录",
        )

    # ---- 空结果：普通 [] / 摘要 count0 三 null，退出码 0，无副作用 ----

    def test_status_filter_without_matching_records(self):
        label = f"recent --url {URL_NO_MATCH!r} --status failure"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--url", URL_NO_MATCH,
                          "--status", "failure")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "[]\n")
        self.assert_state_unchanged(before, label)

        proc_s = run_recent(self.db, "--url", URL_NO_MATCH,
                            "--status", "failure", "--summary")
        self.assertEqual(proc_s.returncode, 0, proc_s.stderr)
        self.assertEqual(json.loads(proc_s.stdout), EMPTY_SUMMARY)

    def test_empty_results_when_db_or_table_missing(self):
        # 数据库与父目录都不存在
        missing_root = self.tmp / "does-not-exist"
        missing_db = missing_root / "nested" / "monitor.sqlite"
        for add_summary in (False, True):
            with self.subTest(case="missing-db", summary=add_summary):
                extra = ("--status", "failure")
                if add_summary:
                    extra += ("--summary",)
                before = snapshot_state(self.tmp, missing_db)
                proc = run_recent(missing_db, *extra)
                self.assertEqual(proc.returncode, 0, proc.stderr)
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
                extra = ("--status", "success")
                if add_summary:
                    extra += ("--summary",)
                proc = run_recent(other_db, *extra)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                if add_summary:
                    self.assertEqual(json.loads(proc.stdout), EMPTY_SUMMARY)
                else:
                    self.assertEqual(proc.stdout, "[]\n")
                after = snapshot_state(self.tmp, other_db)
                self.assertEqual(set(after[1]), {"other_t"})
                self.assertEqual(after, before)

        # checks 表存在但为空表
        empty_db = self.tmp / "empty.sqlite"
        make_empty_checks_db(empty_db)
        for add_summary in (False, True):
            with self.subTest(case="empty-checks", summary=add_summary):
                extra = ("--status", "failure")
                if add_summary:
                    extra += ("--summary",)
                proc = run_recent(empty_db, *extra)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                if add_summary:
                    self.assertEqual(json.loads(proc.stdout), EMPTY_SUMMARY)
                else:
                    self.assertEqual(proc.stdout, "[]\n")

    # ---- 非法 --status：退出码 2、stdout 空、stderr 指出 status 错误 ----

    def assert_status_rejected(self, proc, label):
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
            "status", proc.stderr,
            f"输入 {label}：stderr 应指出 status 参数错误，"
            f"实际 {proc.stderr!r}",
        )
        self.assertNotIn(
            "Traceback", proc.stderr,
            f"输入 {label}：stderr 不应包含 Python 回溯",
        )

    def test_invalid_status_values_rejected(self):
        # 数据库刻意不存在：status 错误必须在读取数据库前拒绝
        missing_db = self.tmp / "not-yet-created.sqlite"
        cases = [
            "Success", "FAILURE", "Failure", "succes", "fail",
            " failure", "failure ", "ok", "", "SUCCESS",
        ]
        for value in cases:
            with self.subTest(value=value):
                before = snapshot_state(self.tmp, missing_db)
                proc = run_recent(missing_db, "--status", value)
                self.assert_status_rejected(
                    proc, f"recent --status {value!r}"
                )
                # 拒绝前不得创建数据库文件
                self.assertFalse(
                    missing_db.exists(),
                    f"非法 status {value!r} 被拒绝时不应创建数据库文件",
                )
                self.assert_state_unchanged(
                    before, f"非法 status {value!r}", missing_db
                )

    def test_bare_status_without_value_rejected(self):
        missing_db = self.tmp / "bare-status.sqlite"
        before = snapshot_state(self.tmp, missing_db)
        # 子进程参数中裸 --status 后不接值
        proc = run_recent(missing_db, "--status")
        self.assert_status_rejected(proc, "recent --status（缺少值）")
        self.assertFalse(
            missing_db.exists(), "裸 --status 被拒绝时不应创建数据库文件"
        )
        self.assert_state_unchanged(before, "裸 --status", missing_db)

    def test_status_error_reported_before_url_and_db_errors(self):
        # 非法 status + 非法 URL + 目录形式数据库：只报 status 错误
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir()
        before = snapshot_state(self.tmp, db_dir)
        proc = run_recent(
            db_dir, "--status", "nope",
            "--url", "https://127.0.0.1:8765/x",
        )
        self.assert_status_rejected(proc, "非法 status + 非法 URL + 目录库")
        self.assertNotIn("协议", proc.stderr)
        self.assertNotIn("目录", proc.stderr)
        self.assert_state_unchanged(before, "status 优先", db_dir)

        # status 合法后，非法 URL 仍优先于目录路径错误（报告顺序不变）
        proc_url = run_recent(
            db_dir, "--status", "success",
            "--url", "https://127.0.0.1:8765/x",
        )
        self.assertEqual(proc_url.returncode, 2)
        self.assertEqual(proc_url.stdout, "")
        self.assertIn("协议", proc_url.stderr)
        self.assertNotIn("目录", proc_url.stderr)

        # URL 也合法时，目录路径错误照常报告
        proc_dir = run_recent(db_dir, "--status", "success")
        self.assertEqual(proc_dir.returncode, 2)
        self.assertIn("目录", proc_dir.stderr)

    # ---- 只读、无副作用：可读不可写库可查，不产生旁路文件 ----

    def test_readonly_database_is_queryable(self):
        import os
        import stat
        ro_db = self.tmp / "readonly.sqlite"
        shutil.copy(self.db, ro_db)
        os.chmod(ro_db, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        try:
            proc = run_recent(ro_db, "--url", URL_A,
                              "--status", "failure", "--limit", "2")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                [r["id"] for r in json.loads(proc.stdout)], [4, 1],
                "可读不可写的数据库也应能完成 status 筛选查询",
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

    # ---- 数据库结构与记录字段保持兼容 ----

    def test_schema_and_record_fields_remain_compatible(self):
        conn = sqlite3.connect(str(self.db))
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(checks)")]
        finally:
            conn.close()
        self.assertEqual(cols, COLUMNS)
        proc = run_recent(self.db, "--status", "failure")
        for rec in json.loads(proc.stdout):
            self.assertEqual(set(rec.keys()), set(COLUMNS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
