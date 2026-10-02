#!/usr/bin/env python3
"""空片段（原始 URL 含 “#” 分隔符但片段内容为空）遗漏修复的回归测试。

背景：check 与 recent --url 的公开约定均为不接受片段（fragment），但旧实现
按 urlparse 解析后的 fragment 是否为空串判断，URL 以 “#” 结尾（或查询参数后
直接跟 “#”）时 fragment 恰为空串，于是漏过校验。修复后统一按原始字符串中的
字面 “#” 分隔符拒绝，两个入口行为一致。

本文件覆盖：

拒绝侧（check 与 recent --url 相同输入，逐一验证）：
  * 路径后空片段      http://127.0.0.1:<port>/health#
  * 路径后非空片段    http://127.0.0.1:<port>/health#section
  * 查询后空片段      http://127.0.0.1:<port>/health?detail=1#
  * 查询后非空片段    http://127.0.0.1:<port>/health?detail=1#section
统一约定：退出码 2、stdout 为空、stderr 以 “healthcheck: error:” 开头、
说明不接受片段并回显原始地址、无 Python 回溯。

无副作用（check）：
  * 数据库及父目录均不存在时不创建任何文件或目录；
  * 已有数据库的表与记录逐条不变，不新增 failure 记录；
  * 数据库路径是目录或内容不是有效 SQLite 文件时，片段错误优先报告；
  * 本机服务存活时也收不到任何请求（拒绝发生在网络请求之前）。

无副作用（recent --url）：
  * 缺库/目录路径/坏文件同样优先报片段错误，不创建、不修改任何文件。

%23 对照（合法，不得误拒）：
  * check 对 http://127.0.0.1:<port>/health%23part?tag=%23 照常探测，
    路径与查询原样发送（%23 不被当作分隔符），只发一次请求、只落一条
    成功记录，输出的 url 与输入完全一致；
  * recent --url 按原始字符串精确匹配该地址。

旧库兼容：
  * 不带 --url 的 recent 仍按 id 倒序与原有条数限制返回，旧记录 url 末尾
    即使带 “#” 也原样返回，不清理、不重写；
  * recent --url 不会通过“删掉片段再查询”接受输入：即使库中存在与输入
    完全相同的旧字符串，也先以片段错误拒绝；%23 与字面 # 互不混淆。

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

FIXED_PORT = 8765

# 四类含字面 “#” 的输入（端口在需要本机服务时替换为临时端口）
def fragment_urls(port):
    return [
        f"http://127.0.0.1:{port}/health#",             # 路径后、空片段
        f"http://127.0.0.1:{port}/health#section",      # 路径后、非空片段
        f"http://127.0.0.1:{port}/health?detail=1#",    # 查询参数后、空片段
        f"http://127.0.0.1:{port}/health?detail=1#sec", # 查询参数后、非空片段
    ]


# %23 是普通百分号编码内容，必须照常接受
def encoded_url(port):
    return f"http://127.0.0.1:{port}/health%23part?tag=%23"


ENCODED_REQUEST_TARGET = "/health%23part?tag=%23"

# 旧库中可能已存在的、url 末尾带 “#” 的历史记录（不经迁移、原样保留）
LEGACY_HASH_URL = f"http://127.0.0.1:{FIXED_PORT}/health#"
LEGACY_PLAIN_URL = f"http://127.0.0.1:{FIXED_PORT}/health?detail=1"

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


def run_cli(db_path, *extra):
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


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


def build_sample_db(path):
    """两条普通历史 + 一条 url 末尾带 “#” 的旧记录 + 一个其他表。"""
    conn = sqlite3.connect(str(path))
    conn.execute(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
        "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, LEGACY_PLAIN_URL, "2026-10-02T04:40:00.000000+00:00",
         3, "success", 200, "ok"),
    )
    conn.execute(
        "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
        "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (2, LEGACY_HASH_URL, "2026-10-02T04:40:05.000000+00:00",
         1, "failure", None, "connection_error"),
    )
    conn.execute(
        "INSERT INTO checks (id, url, checked_at, elapsed_ms, status, "
        "http_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (3, LEGACY_PLAIN_URL, "2026-10-02T04:40:09.000000+00:00",
         4, "success", 204, "ok"),
    )
    conn.execute("CREATE TABLE other_t (name TEXT, n INTEGER)")
    conn.execute("INSERT INTO other_t VALUES ('kept', 42)")
    conn.commit()
    conn.close()


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """记录完整请求目标（含查询串、百分号编码原样），抑制日志噪音。"""

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


class FragmentTestBase(unittest.TestCase):
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

    def _start_server(self):
        server, thread = start_server()
        self._servers.append((server, thread))
        return server

    def assert_fragment_error(self, proc, url):
        """含片段 URL 的统一约定：退出码 2、stdout 空、stderr 固定前缀、
        说明不接受片段并回显原始地址、无 Python 回溯。"""
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
            f"{label}：stderr 不应出现 Python 回溯",
        )


# ---------------------------------------------------------------- check 拒绝


class CheckFragmentRejectionTests(FragmentTestBase):
    def test_missing_db_and_parent_nothing_created(self):
        # 数据库文件与父目录均不存在：拒绝时不得创建任何东西
        missing_root = self.tmp / "does-not-exist"
        db = missing_root / "nested" / "monitor.sqlite"
        for url in fragment_urls(FIXED_PORT):
            with self.subTest(url=url):
                proc = run_check(db, url)
                self.assert_fragment_error(proc, url)
                self.assertFalse(os.path.exists(missing_root))
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_existing_db_tables_and_records_untouched(self):
        # 已存在的库：表集合、逐条记录、其他表内容均不变，不产生 failure 记录
        db = self.tmp / "monitor.sqlite"
        build_sample_db(db)
        tables_before = table_names(db)
        rows_before = checks_rows(db)

        for url in fragment_urls(FIXED_PORT):
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
            # 没有任何含 “#” 的新记录落库
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM checks WHERE url LIKE '%#%'"
                ).fetchone()[0],
                1,  # 仅样本中原有的旧记录 id=2
            )
        finally:
            conn.close()
        self.assertEqual(
            {p.name for p in self.tmp.iterdir()}, {"monitor.sqlite"}
        )

    def test_directory_db_path_reports_fragment_error_first(self):
        # --db 指向目录：片段错误必须优先于数据库路径错误
        for url in fragment_urls(FIXED_PORT):
            with self.subTest(url=url):
                proc = run_check(self.tmp, url)
                self.assert_fragment_error(proc, url)
                self.assertNotIn("目录", proc.stderr)

    def test_invalid_db_file_reports_fragment_error_first(self):
        # --db 指向存在但不是 SQLite 的文件：同样先报片段错误，文件内容不变
        db = self.tmp / "broken.sqlite"
        db.write_bytes(b"this is not a sqlite database\n")
        for url in fragment_urls(FIXED_PORT):
            with self.subTest(url=url):
                proc = run_check(db, url)
                self.assert_fragment_error(proc, url)
        self.assertEqual(
            db.read_bytes(), b"this is not a sqlite database\n"
        )

    def test_live_server_receives_no_request(self):
        # 即使本机服务存活且 URL 端口指向它，也必须在发请求前拒绝
        server = self._start_server()
        port = server.server_address[1]
        db = self.tmp / "monitor.sqlite"

        for url in fragment_urls(port):
            with self.subTest(url=url):
                proc = run_check(db, url)
                self.assert_fragment_error(proc, url)

        self.assertEqual(server.requests, [])
        self.assertFalse(db.exists())


# ---------------------------------------------------------------- recent 拒绝


class RecentFragmentRejectionTests(FragmentTestBase):
    def test_missing_db_and_parent_nothing_created(self):
        missing_root = self.tmp / "does-not-exist"
        db = missing_root / "nested" / "monitor.sqlite"
        for url in fragment_urls(FIXED_PORT):
            with self.subTest(url=url):
                proc = run_recent(db, "--url", url)
                self.assert_fragment_error(proc, url)
                self.assertFalse(os.path.exists(missing_root))
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_existing_db_untouched(self):
        db = self.tmp / "monitor.sqlite"
        build_sample_db(db)
        rows_before = checks_rows(db)
        tables_before = table_names(db)

        for url in fragment_urls(FIXED_PORT):
            with self.subTest(url=url):
                proc = run_recent(db, "--url", url)
                self.assert_fragment_error(proc, url)
                self.assertEqual(checks_rows(db), rows_before)
                self.assertEqual(table_names(db), tables_before)

        self.assertEqual(
            {p.name for p in self.tmp.iterdir()}, {"monitor.sqlite"}
        )

    def test_directory_db_path_reports_fragment_error_first(self):
        db_dir = self.tmp / "a-directory"
        db_dir.mkdir()
        for url in fragment_urls(FIXED_PORT):
            with self.subTest(url=url):
                proc = run_recent(db_dir, "--url", url)
                self.assert_fragment_error(proc, url)
                self.assertNotIn("目录", proc.stderr)

    def test_invalid_db_file_reports_fragment_error_first(self):
        db = self.tmp / "broken.sqlite"
        db.write_bytes(b"not a sqlite database either\n")
        for url in fragment_urls(FIXED_PORT):
            with self.subTest(url=url):
                proc = run_recent(db, "--url", url)
                self.assert_fragment_error(proc, url)
        self.assertEqual(db.read_bytes(), b"not a sqlite database either\n")

    def test_fragment_filter_rejected_even_when_identical_legacy_record_exists(self):
        # 不允许“删掉片段后继续查询”：即使库中就有完全相同的旧字符串，
        # recent --url 仍先按片段错误拒绝，绝不返回记录
        db = self.tmp / "monitor.sqlite"
        build_sample_db(db)
        proc = run_recent(db, "--url", LEGACY_HASH_URL)
        self.assert_fragment_error(proc, LEGACY_HASH_URL)


# ------------------------------------------------- 旧库兼容：不带 --url 的 recent


class LegacyDatabaseTests(FragmentTestBase):
    def test_recent_without_url_returns_hash_suffixed_legacy_url_verbatim(self):
        db = self.tmp / "monitor.sqlite"
        build_sample_db(db)
        tables_before = table_names(db)
        rows_before = checks_rows(db)

        # 不带 --url：按 id 倒序，旧记录 url 末尾的 “#” 原样返回
        proc = run_recent(db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = json.loads(proc.stdout)
        self.assertEqual([r["id"] for r in records], [3, 2, 1])
        self.assertEqual(
            [r["url"] for r in records],
            [LEGACY_PLAIN_URL, LEGACY_HASH_URL, LEGACY_PLAIN_URL],
        )

        # 条数限制仍按 id 倒序生效
        proc2 = run_recent(db, "--limit", "2")
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertEqual(
            [r["id"] for r in json.loads(proc2.stdout)], [3, 2]
        )

        # 只读：库内容不变
        self.assertEqual(checks_rows(db), rows_before)
        self.assertEqual(table_names(db), tables_before)

    def test_encoded_url_filter_does_not_match_literal_hash_legacy_record(self):
        # %23 与字面 “#” 是不同的原始字符串：精确匹配不得混淆
        db = self.tmp / "monitor.sqlite"
        build_sample_db(db)
        encoded_counterpart = f"http://127.0.0.1:{FIXED_PORT}/health%23"
        proc = run_recent(db, "--url", encoded_counterpart)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "[]\n")
        self.assertEqual(proc.stderr, "")


# ------------------------------------------------- %23 对照：合法地址不被误拒


class EncodedHashAcceptanceTests(FragmentTestBase):
    def test_check_encoded_hash_probed_once_and_saved_raw(self):
        server = self._start_server()
        port = server.server_address[1]
        db = self.tmp / "monitor.sqlite"
        url = encoded_url(port)

        proc = run_check(db, url)
        self.assertEqual(
            proc.returncode, 0,
            f"%23 输入应被接受：stderr={proc.stderr!r}",
        )
        record = json.loads(proc.stdout)
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["http_status"], 200)
        self.assertEqual(record["reason"], "ok")
        # 输出的 url 与输入逐字符一致（不规范化、不去编码）
        self.assertEqual(record["url"], url)

        # 路径与查询原样发送（%23 保留百分号编码），且只有一次请求
        self.assertEqual(server.requests, [ENCODED_REQUEST_TARGET])

        # 落库一条成功记录，url 为原始输入
        rows = checks_rows(db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], url)
        self.assertEqual(rows[0][4], "success")
        self.assertEqual(rows[0][5], 200)
        self.assertEqual(rows[0][6], "ok")

    def test_recent_url_filter_exact_matches_encoded_url(self):
        server = self._start_server()
        port = server.server_address[1]
        db = self.tmp / "monitor.sqlite"
        url = encoded_url(port)

        proc_check = run_check(db, url)
        self.assertEqual(proc_check.returncode, 0, proc_check.stderr)
        self.assertEqual(server.requests, [ENCODED_REQUEST_TARGET])

        # recent 只读，不再发请求
        proc = run_recent(db, "--url", url)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = json.loads(proc.stdout)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["url"], url)
        self.assertEqual(records[0]["status"], "success")
        self.assertEqual(server.requests, [ENCODED_REQUEST_TARGET])

        # 去掉 %23 段的近似地址查不到该记录（精确匹配，不做规范化）
        similar = f"http://127.0.0.1:{port}/healthpart?tag="
        proc_similar = run_recent(db, "--url", similar)
        self.assertEqual(proc_similar.returncode, 0, proc_similar.stderr)
        self.assertEqual(proc_similar.stdout, "[]\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
