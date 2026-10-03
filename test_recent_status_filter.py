#!/usr/bin/env python3
"""recent --status 状态筛选功能的独立回归测试。

通过公开命令行入口（子进程运行 healthcheck.py）观察退出码与 JSON 输出，
全部数据放在独立临时 SQLite 数据库中，不依赖固定端口上的服务或已有历史
文件，临时资源在每个用例结束后释放。

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

# ---- 固定样本（验收用五条记录）-------------------------------------------
# 目标 A：带路径与查询参数；B 仅将 detail 改为 2
URL_A = "http://127.0.0.1:8765/health?detail=1"
URL_B = "http://127.0.0.1:8765/health?detail=2"

# id 1..5 目标依次 A、A、B、A、A；状态依次 failure、success、failure、
# failure、success；elapsed_ms 依次为 0、2、7、3、9
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

NULL_SUMMARY = {
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
    """创建包含验收样本 5 条记录的临时数据库。"""
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
    """存在 checks 表但为空表的数据库。"""
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

    # ---- 验收示例：--url A --status failure --limit 2 → id 4、1 ----

    def test_acceptance_url_status_failure_limit_2(self):
        label = f"recent --url A --status failure --limit 2"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--url", URL_A, "--status",
                          "failure", "--limit", "2")
        self.assertEqual(
            proc.returncode, 0,
            f"输入 {label}：期望退出码 0，实际 {proc.returncode}，"
            f"stderr={proc.stderr!r}",
        )
        self.assertEqual(proc.stderr, "")
        records = json.loads(proc.stdout)
        self.assertEqual(
            records, [REC4, REC1],
            f"输入 {label}：期望返回 id [4, 1] 的完整记录，"
            f"实际 {json.dumps(records, ensure_ascii=False)}",
        )
        # 完整七字段
        self.assertTrue(all(set(r.keys()) == set(COLUMNS) for r in records))
        # 两条记录的失败原因不同（timeout / connection_error），
        # 证明筛选只看 status，不区分失败原因
        self.assertEqual(
            {r["reason"] for r in records}, {"timeout", "connection_error"}
        )
        self.assert_state_unchanged(before, label)

    def test_acceptance_summary_same_conditions(self):
        label = "recent --url A --status failure --limit 2 --summary"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--url", URL_A, "--status",
                          "failure", "--limit", "2", "--summary")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        summary = json.loads(proc.stdout)
        self.assertEqual(
            summary,
            {"count": 2, "min_elapsed_ms": 0, "max_elapsed_ms": 3,
             "avg_elapsed_ms": 1.5},
            f"输入 {label}：摘要应为 count=2/min=0/max=3/avg=1.5，"
            f"实际 {json.dumps(summary, ensure_ascii=False)}",
        )
        self.assert_state_unchanged(before, label)

    # ---- 仅 --status：跨全部目标按状态筛选 ----

    def test_status_only_failure_and_success(self):
        proc = run_recent(self.db, "--status", "failure")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        # 全部 failure：id 4（A）、3（B）、1（A），按 id 倒序
        self.assertEqual(
            json.loads(proc.stdout), [REC4, REC3, REC1],
            "--status failure：期望 id [4, 3, 1]",
        )

        proc = run_recent(self.db, "--status", "success")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            json.loads(proc.stdout), [REC5, REC2],
            "--status success：期望 id [5, 2]",
        )

    def test_status_limit_applies_after_filtering(self):
        # 先筛选 failure（4、3、1），再按 id 倒序取前 2 条：4、3
        proc = run_recent(self.db, "--status", "failure", "--limit", "2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            [r["id"] for r in json.loads(proc.stdout)], [4, 3],
            "--status failure --limit 2：期望先筛选后限量 id [4, 3]",
        )

    def test_status_summary_counts_same_records(self):
        # failure 记录 id 1/3/4，耗时 0/7/3
        proc = run_recent(self.db, "--status", "failure", "--summary")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            json.loads(proc.stdout),
            {"count": 3, "min_elapsed_ms": 0, "max_elapsed_ms": 7,
             "avg_elapsed_ms": 10 / 3},
            "--status failure --summary：应统计普通查询返回的同批记录",
        )

    # ---- 无匹配 / 缺库 / 无 checks 表 / 空表：[] 与空摘要 ----

    def assert_empty_normal(self, proc, label):
        self.assertEqual(
            proc.returncode, 0,
            f"输入 {label}：期望退出码 0，实际 {proc.returncode}，"
            f"stderr={proc.stderr!r}",
        )
        self.assertEqual(proc.stdout, "[]\n", f"输入 {label}：期望 []")
        self.assertEqual(proc.stderr, "")

    def assert_empty_summary(self, proc, label):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            json.loads(proc.stdout), NULL_SUMMARY,
            f"输入 {label}：期望 count 0 且三个耗时字段为 null",
        )
        self.assertEqual(proc.stderr, "")

    def test_no_matching_status_for_url_is_empty(self):
        # B 只有 id 3 一条 failure，没有 success
        label = "recent --url B --status success"
        before = snapshot_state(self.tmp, self.db)
        proc = run_recent(self.db, "--url", URL_B, "--status", "success")
        self.assert_empty_normal(proc, label)
        proc_s = run_recent(self.db, "--url", URL_B, "--status",
                            "success", "--summary")
        self.assert_empty_summary(proc_s, label + " --summary")
        self.assert_state_unchanged(before, label)

    def test_missing_database_and_parent_empty_creates_nothing(self):
        missing_root = self.tmp / "does-not-exist"
        db = missing_root / "nested" / "monitor.sqlite"
        for extra in (
            ("--status", "failure"),
            ("--status", "failure", "--summary"),
        ):
            with self.subTest(extra=extra):
                before = snapshot_state(self.tmp, db)
                proc = run_recent(db, *extra)
                if "--summary" in extra:
                    self.assert_empty_summary(proc, f"缺库 {extra}")
                else:
                    self.assert_empty_normal(proc, f"缺库 {extra}")
                self.assert_state_unchanged(before, f"缺库 {extra}", db)
        self.assertFalse(
            missing_root.exists(),
            "查询不应创建缺失的数据库目录或文件",
        )

    def test_no_checks_table_empty(self):
        db = self.tmp / "other-only.sqlite"
        make_other_table_db(db)
        label = "仅有其他表 --status success"
        before = snapshot_state(self.tmp, db)
        self.assert_empty_normal(
            run_recent(db, "--status", "success"), label)
        self.assert_empty_summary(
            run_recent(db, "--status", "success", "--summary"),
            label + " --summary",
        )
        # 不新建 checks 表，其他表原样保留
        after = snapshot_state(self.tmp, db)
        self.assertEqual(set(after[1]), {"other_t"})
        self.assertEqual(after, before)

    def test_empty_checks_table_empty(self):
        db = self.tmp / "empty.sqlite"
        make_empty_checks_db(db)
        label = "空表 --status failure"
        self.assert_empty_normal(
            run_recent(db, "--status", "failure"), label)
        self.assert_empty_summary(
            run_recent(db, "--status", "failure", "--summary"),
            label + " --summary",
        )

    # ---- 非法 --status：退出 2、stdout 空、stderr 指出 status 参数错误 ----

    def assert_status_rejected(self, proc, value):
        label = f"recent --status {value!r}"
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
            "status 参数错误", proc.stderr,
            f"输入 {label}：stderr 应指出 status 参数错误，"
            f"实际 {proc.stderr!r}",
        )
        self.assertIn(
            value, proc.stderr,
            f"输入 {label}：stderr 应回显非法值 {value!r}，"
            f"实际 {proc.stderr!r}",
        )
        self.assertNotIn("Traceback", proc.stderr)

    def test_invalid_status_values_exit_2(self):
        # 区分大小写；其他值与空字符串一律拒绝
        for value in ("Success", "FAILURE", "SUCCESS", "failure ",
                      " failure", "ok", "FAILED", ""):
            with self.subTest(value=value):
                # 数据库路径刻意指向目录：status 错误必须在读库前优先报告
                db_dir = self.tmp / "a-directory"
                db_dir.mkdir(exist_ok=True)
                before = snapshot_state(self.tmp, db_dir)
                proc = run_recent(db_dir, "--status", value)
                self.assert_status_rejected(proc, value)
                self.assertNotIn(
                    "目录", proc.stderr,
                    f"输入 --status {value!r} 且数据库路径是目录时，"
                    f"应优先报 status 错误，却报告了目录错误："
                    f"{proc.stderr!r}",
                )
                self.assert_state_unchanged(
                    before, f"非法 status {value!r}（目录路径）", db_dir
                )

    def test_status_missing_value_exit_2(self):
        proc = run_recent(self.db, "--status")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertNotIn("Traceback", proc.stderr)

    def test_status_validated_before_url_and_db(self):
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir(exist_ok=True)
        # 非法 status + 非法 URL + 目录路径：只报 status 错误
        proc = run_recent(db_dir, "--status", "nope",
                          "--url", "not-a-url")
        self.assert_status_rejected(proc, "nope")
        self.assertNotIn("URL", proc.stderr.replace("status 参数错误", ""))
        self.assertNotIn("目录", proc.stderr)

        # status 合法后，非法 URL 才被报告（报告顺序不变）
        proc2 = run_recent(db_dir, "--status", "failure",
                           "--url", "not-a-url")
        self.assertEqual(proc2.returncode, 2)
        self.assertEqual(proc2.stdout, "")
        self.assertIn("协议", proc2.stderr)
        self.assertNotIn("Traceback", proc2.stderr)

        # status 与 URL 都合法后，目录路径才被报告
        proc3 = run_recent(db_dir, "--status", "failure",
                           "--url", URL_A)
        self.assertEqual(proc3.returncode, 2)
        self.assertEqual(proc3.stdout, "")
        self.assertIn("目录", proc3.stderr)

    # ---- 未提供 --status：两种查询输出与既有行为一致 ----

    def test_without_status_keeps_default_behavior(self):
        proc = run_recent(self.db, "--url", URL_A)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            json.loads(proc.stdout), [REC5, REC4, REC2, REC1],
            "不带 --status：A 的全部记录（成功与失败）按 id 倒序",
        )

        proc_all = run_recent(self.db)
        self.assertEqual(
            [r["id"] for r in json.loads(proc_all.stdout)],
            [5, 4, 3, 2, 1],
            "不带 --status/--url：默认 limit 5，全部目标按 id 倒序",
        )

        proc_s = run_recent(self.db, "--url", URL_A, "--summary")
        self.assertEqual(
            json.loads(proc_s.stdout),
            {"count": 4, "min_elapsed_ms": 0, "max_elapsed_ms": 9,
             "avg_elapsed_ms": (0 + 2 + 3 + 9) / 4},
            "不带 --status 的摘要：成功与失败记录全部计入",
        )

    # ---- 只读数据库可查询，无 -wal/-journal 等副作用 ----

    def test_readonly_database_is_queryable(self):
        ro_db = self.tmp / "readonly.sqlite"
        build_sample_db(ro_db)
        ro_db.chmod(0o444)
        try:
            proc = run_recent(ro_db, "--url", URL_A, "--status",
                              "failure", "--limit", "2")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                [r["id"] for r in json.loads(proc.stdout)], [4, 1])
            sidecars = [
                p.name for p in self.tmp.iterdir()
                if p.name.startswith("readonly.sqlite") and p.name != "readonly.sqlite"
            ]
            self.assertEqual(
                sidecars, [],
                f"只读查询不应产生 -wal/-journal 旁路文件：{sidecars}",
            )
        finally:
            ro_db.chmod(0o644)

    # ---- check 子命令不接受 --status ----

    def test_check_subcommand_rejects_status(self):
        proc = run_cli(self.db, "check", "--url", URL_A,
                       "--status", "failure")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("unrecognized arguments", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
