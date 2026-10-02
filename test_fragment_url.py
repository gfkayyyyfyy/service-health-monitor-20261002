#!/usr/bin/env python3
"""healthcheck.py 片段(fragment)边界的回归测试。

背景：check 与 recent --url 的公开约定均不接受片段，但 urlparse 对空片段
（结尾裸 '#' 或查询参数后的裸 '#'）给出 fragment == ""，旧校验因此漏掉了
http://127.0.0.1:8765/health# 这类输入。修复后两个入口统一按原始字符串中的
'#' 分隔符拒绝，而路径/查询中的 %23 仍属普通百分号编码内容。

本文件覆盖：

  * 末尾 '#'（空片段）、'#section'（非空片段）、查询参数后的 '#' / '#frag'：
      - check 与 recent --url 一律退出码 2、stdout 为空、stderr 以
        "healthcheck: error:" 开头、说明不接受片段并回显原始地址、无回溯；
      - 拒绝发生在网络请求与数据库访问之前：存活服务收不到任何请求，
        数据库与父目录不存在时不创建，已有数据库的表与记录不变，
        目录或无效文件形式的数据库路径仍优先报告片段错误；
      - 不产生 failure 记录，也不会删除片段后继续探测或查询。
  * %23 对照：http://127.0.0.1:<port>/health%23part?tag=%23 合法：
      - check 面对返回 200 的回环服务只发一次请求，路径与查询原样发送，
        一条成功记录，输出的 url 与输入一致；
      - recent --url 仍按原始字符串精确匹配。
  * 旧数据兼容：不带 --url 的 recent 仍按 id 倒序与原条数限制查询，
    即使旧记录 url 末尾已有 '#' 也原样返回，不清理、不重写、不迁移。

全部使用独立临时目录与本机 127.0.0.1 临时端口服务，不访问公网。
运行：python3 test_fragment_url.py
"""

import http.server
import json
import os
import pathlib
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "healthcheck.py"

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

# 旧版本可能落库的“末尾带 #”记录；recent 不带筛选时必须原样返回
LEGACY_HASH_URL = "http://127.0.0.1:8765/health#"
LEGACY_RECORD = (
    1,
    LEGACY_HASH_URL,
    "2026-10-02T04:40:00.000000+00:00",
    3,
    "success",
    200,
    "ok",
)
PLAIN_RECORD = (
    2,
    "http://127.0.0.1:8765/health",
    "2026-10-02T04:40:05.000000+00:00",
    1,
    "failure",
    None,
    "connection_error",
)

# 路径与查询中含百分号编码 %23 的合法地址
ENCODED_PATH = "/health%23part"
ENCODED_QUERY = "tag=%23"


def run_cli(db_path, *extra):
    """以子进程运行 healthcheck.py，返回 CompletedProcess。"""
    cmd = [
        sys.executable, str(SCRIPT), "--db", str(db_path), *extra,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def run_check(db_path, url, *extra):
    return run_cli(db_path, "check", "--url", url, *extra)


def run_recent(db_path, *extra):
    return run_cli(db_path, "recent", *extra)


def table_names(path):
    conn = sqlite3.connect(str(path))
    try:
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()


def checks_rows(path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(
            "SELECT id, url, checked_at, elapsed_ms, status, "
            "http_status, reason FROM checks ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def build_sample_db(path, *records):
    """建库并写入给定元组记录，另建一张其他表用于核对不受影响。"""
    conn = sqlite3.connect(str(path))
    conn.execute(SCHEMA_SQL)
    conn.executemany(
        "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
        "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        records,
    )
    conn.execute("CREATE TABLE other_t (name TEXT, n INTEGER)")
    conn.execute("INSERT INTO other_t VALUES ('kept', 42)")
    conn.commit()
    conn.close()


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """记录原始请求目标，返回 200，抑制日志噪音。"""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append(self.path)
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_server():
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _RecordingHandler
    )
    server.daemon_threads = True
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class FragmentUrlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-fragment-test-"))
        self._servers = []

    def tearDown(self):
        for server, thread in self._servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                os.chmod(os.path.join(root, name), stat.S_IRWXU)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start(self):
        server, thread = start_server()
        self._servers.append((server, thread))
        return server

    def fragment_urls(self, port):
        """空/非空片段 × 路径后/查询参数后的全部形态（其余部分均合法）。"""
        return [
            f"http://127.0.0.1:{port}/#",                 # 根路径后空片段
            f"http://127.0.0.1:{port}/health#",           # 路径后空片段
            f"http://127.0.0.1:{port}/health#section",    # 路径后非空片段
            f"http://127.0.0.1:{port}/health?x=1#",       # 查询参数后空片段
            f"http://127.0.0.1:{port}/health?x=1#frag",   # 查询参数后非空片段
        ]

    def assert_fragment_error(self, proc, url):
        """片段错误约定：退出码 2、stdout 空、固定前缀、说明不接受片段、
        回显原始地址、无 Python 回溯。"""
        label = f"输入 {url!r}"
        self.assertEqual(
            proc.returncode, 2,
            f"{label}：期望退出码 2，实际 {proc.returncode}，"
            f"stdout={proc.stdout!r}，stderr={proc.stderr!r}",
        )
        self.assertEqual(proc.stdout, "", f"{label}：stdout 应为空")
        self.assertTrue(
            proc.stderr.startswith("healthcheck: error:"),
            f"{label}：stderr 应以固定前缀开头，实际 {proc.stderr!r}",
        )
        self.assertIn(
            "片段", proc.stderr,
            f"{label}：stderr 应说明不接受片段，实际 {proc.stderr!r}",
        )
        self.assertIn(
            url, proc.stderr,
            f"{label}：stderr 应回显原始地址 {url!r}，"
            f"实际 {proc.stderr!r}",
        )
        self.assertNotIn(
            "Traceback", proc.stderr,
            f"{label}：stderr 不应出现 Python 回溯，实际 {proc.stderr!r}",
        )

    # ---- check：数据库与父目录均不存在时不创建任何东西 ----

    def test_check_missing_db_and_parent_nothing_created(self):
        server = self._start()
        urls = self.fragment_urls(server.server_address[1])
        missing_root = self.tmp / "does-not-exist"
        db = missing_root / "nested" / "monitor.sqlite"

        for url in urls:
            with self.subTest(url=url):
                proc = run_check(db, url)
                self.assert_fragment_error(proc, url)

        self.assertFalse(missing_root.exists())
        self.assertEqual(list(self.tmp.iterdir()), [])

    # ---- check：已有数据库的表与记录不变，不产生 failure 记录 ----

    def test_check_existing_db_tables_and_rows_untouched(self):
        server = self._start()
        urls = self.fragment_urls(server.server_address[1])
        db = self.tmp / "monitor.sqlite"
        build_sample_db(db, LEGACY_RECORD, PLAIN_RECORD)
        rows_before = checks_rows(db)
        tables_before = table_names(db)

        for url in urls:
            with self.subTest(url=url):
                proc = run_check(db, url)
                self.assert_fragment_error(proc, url)
                self.assertEqual(checks_rows(db), rows_before)
                self.assertEqual(table_names(db), tables_before)

        conn = sqlite3.connect(str(db))
        try:
            self.assertEqual(
                conn.execute("SELECT name, n FROM other_t").fetchall(),
                [("kept", 42)],
            )
        finally:
            conn.close()
        # 不产生 -wal/-journal 旁路文件
        self.assertEqual({p.name for p in self.tmp.iterdir()},
                         {"monitor.sqlite"})

    # ---- check：目录或无效文件形式的数据库路径，片段错误仍优先 ----

    def test_check_directory_db_reports_fragment_first(self):
        url = "http://127.0.0.1:8765/health#"
        proc = run_check(self.tmp, url)
        self.assert_fragment_error(proc, url)
        self.assertNotIn("目录", proc.stderr)
        self.assertNotIn("数据库", proc.stderr)

    def test_check_garbage_db_reports_fragment_first(self):
        db = self.tmp / "garbage.sqlite"
        db.write_bytes(b"this is definitely not a sqlite database\n")
        original = db.read_bytes()
        url = "http://127.0.0.1:8765/health?x=1#frag"

        proc = run_check(db, url)
        self.assert_fragment_error(proc, url)
        self.assertNotIn("数据库", proc.stderr)
        # 文件原样保留，未被初始化或覆盖
        self.assertEqual(db.read_bytes(), original)

    # ---- check：存活服务也收不到请求 ----

    def test_check_no_request_sent_even_with_live_server(self):
        server = self._start()
        port = server.server_address[1]
        urls = self.fragment_urls(port)
        db = self.tmp / "monitor.sqlite"

        for url in urls:
            with self.subTest(url=url):
                proc = run_check(db, url)
                self.assert_fragment_error(proc, url)

        # 所有片段输入均未发出请求，也未创建数据库
        self.assertEqual(server.requests, [])
        self.assertFalse(db.exists())

    # ---- recent --url：同样的拒绝契约 ----

    def test_recent_fragment_urls_missing_db_nothing_created(self):
        server = self._start()
        urls = self.fragment_urls(server.server_address[1])
        missing_root = self.tmp / "does-not-exist"
        db = missing_root / "nested" / "monitor.sqlite"

        for url in urls:
            with self.subTest(url=url):
                proc = run_recent(db, "--url", url)
                self.assert_fragment_error(proc, url)

        self.assertFalse(missing_root.exists())
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_recent_fragment_urls_existing_db_untouched(self):
        server = self._start()
        urls = self.fragment_urls(server.server_address[1])
        db = self.tmp / "monitor.sqlite"
        build_sample_db(db, LEGACY_RECORD, PLAIN_RECORD)
        rows_before = checks_rows(db)
        tables_before = table_names(db)

        for url in urls:
            with self.subTest(url=url):
                proc = run_recent(db, "--url", url)
                self.assert_fragment_error(proc, url)
                self.assertEqual(checks_rows(db), rows_before)
                self.assertEqual(table_names(db), tables_before)

        self.assertEqual({p.name for p in self.tmp.iterdir()},
                         {"monitor.sqlite"})

    def test_recent_directory_db_reports_fragment_first(self):
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir()
        hash_url = "http://127.0.0.1:8765/health#"

        proc = run_recent(db_dir, "--url", hash_url)
        self.assert_fragment_error(proc, hash_url)
        self.assertNotIn("目录", proc.stderr)

        # 换成不含片段的合法 %23 地址后，同一路径才报数据库（目录）错误
        encoded_url = "http://127.0.0.1:8765/health%23part?tag=%23"
        proc_ok = run_recent(db_dir, "--url", encoded_url)
        self.assertEqual(proc_ok.returncode, 2, proc_ok.stderr)
        self.assertEqual(proc_ok.stdout, "")
        self.assertIn("目录", proc_ok.stderr)
        self.assertNotIn("Traceback", proc_ok.stderr)

    def test_recent_garbage_db_reports_fragment_first(self):
        db = self.tmp / "garbage.sqlite"
        db.write_bytes(b"this is definitely not a sqlite database\n")
        original = db.read_bytes()

        for url in (
            "http://127.0.0.1:8765/health#",
            "http://127.0.0.1:8765/health?x=1#",
        ):
            with self.subTest(url=url):
                proc = run_recent(db, "--url", url)
                self.assert_fragment_error(proc, url)
                self.assertNotIn("数据库", proc.stderr)
        self.assertEqual(db.read_bytes(), original)

    # ---- 不得删除片段后继续匹配：旧 # 记录既不能带 # 查询，也不会被裸 URL 匹配 ----

    def test_recent_does_not_strip_fragment_to_match(self):
        db = self.tmp / "legacy.sqlite"
        build_sample_db(db, LEGACY_RECORD)

        # 即使库里存在 url 完全相等（末尾带 #）的旧记录，筛选值仍被拒绝
        proc = run_recent(db, "--url", LEGACY_HASH_URL)
        self.assert_fragment_error(proc, LEGACY_HASH_URL)

        # 去掉片段的地址是另一个原始字符串：精确匹配为空，绝不自动拼合
        proc_stripped = run_recent(db, "--url",
                                   "http://127.0.0.1:8765/health")
        self.assertEqual(proc_stripped.returncode, 0, proc_stripped.stderr)
        self.assertEqual(proc_stripped.stdout, "[]\n")
        self.assertEqual(proc_stripped.stderr, "")
        # 被拒绝/未命中均未改动旧记录
        self.assertEqual(checks_rows(db), [LEGACY_RECORD])

    # ---- %23 对照：合法地址不被误拒，路径与查询原样、仅一次请求 ----

    def test_check_percent_encoded_23_accepted_and_sent_raw(self):
        server = self._start()
        port = server.server_address[1]
        db = self.tmp / "encoded.sqlite"
        url = f"http://127.0.0.1:{port}{ENCODED_PATH}?{ENCODED_QUERY}"

        proc = run_check(db, url)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        record = json.loads(proc.stdout)
        self.assertEqual(record["url"], url)
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["http_status"], 200)
        self.assertEqual(record["reason"], "ok")

        # 路径与查询原样发送（%23 不被当作片段分隔符），且只有一次请求
        self.assertEqual(server.requests,
                         [f"{ENCODED_PATH}?{ENCODED_QUERY}"])

        rows = checks_rows(db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], url)
        self.assertEqual(rows[0][4], "success")
        self.assertEqual(rows[0][5], 200)
        self.assertEqual(rows[0][6], "ok")

    def test_recent_percent_encoded_23_exact_match(self):
        server = self._start()
        port = server.server_address[1]
        db = self.tmp / "encoded.sqlite"
        url = f"http://127.0.0.1:{port}{ENCODED_PATH}?{ENCODED_QUERY}"

        proc_check = run_check(db, url)
        self.assertEqual(proc_check.returncode, 0, proc_check.stderr)

        proc = run_recent(db, "--url", url)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        records = json.loads(proc.stdout)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["url"], url)
        self.assertEqual(records[0]["status"], "success")
        self.assertEqual(records[0]["http_status"], 200)

        # 查询值稍有不同即为不同原始字符串，不得规范化合并
        proc_other = run_recent(db, "--url",
                                f"http://127.0.0.1:{port}{ENCODED_PATH}"
                                f"?{ENCODED_QUERY}&x=1")
        self.assertEqual(proc_other.returncode, 0, proc_other.stderr)
        self.assertEqual(proc_other.stdout, "[]\n")

        # recent 只读：仍只有 check 落下的那一条记录
        self.assertEqual(len(checks_rows(db)), 1)

    # ---- 旧数据兼容：不带 --url 时末尾带 # 的旧记录原样返回 ----

    def test_recent_without_url_returns_legacy_hash_url_raw(self):
        db = self.tmp / "legacy.sqlite"
        build_sample_db(db, LEGACY_RECORD, PLAIN_RECORD)

        # 默认条数限制内按 id 倒序：id 2 在前，id 1 的末尾 # 原样保留
        proc = run_recent(db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = json.loads(proc.stdout)
        self.assertEqual([r["id"] for r in records], [2, 1])
        self.assertEqual(records[1]["url"], LEGACY_HASH_URL)

        # 条数限制仍生效，且单条返回时 # 依旧原样
        proc1 = run_recent(db, "--limit", "1")
        self.assertEqual(proc1.returncode, 0, proc1.stderr)
        self.assertEqual([r["id"] for r in json.loads(proc1.stdout)], [2])

        # 表与记录未被清理或重写，也没有新增表
        self.assertEqual(checks_rows(db), [LEGACY_RECORD, PLAIN_RECORD])
        self.assertEqual(table_names(db), {"checks", "other_t",
                                           "sqlite_sequence"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
