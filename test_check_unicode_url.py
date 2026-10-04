#!/usr/bin/env python3
"""healthcheck.py 本机 HTTP 目标中中文路径/查询值编码发送的回归测试。

覆盖 encode_request_target 接入 check 的既有公开行为：
  * 原始 URL 含中文路径段与中文查询值（并与既有百分号编码 %2f/%23、
    查询中的 '+' 混排）时：
      - check 只发送一次 GET，服务端收到的请求目标里中文按 UTF-8 字节
        转成大写十六进制百分号编码（健康→%E5%81%A5%E5%BA%B7，
        中文→%E4%B8%AD%E6%96%87）；
      - 原有 %2f 的大小写、%23、'+' 与查询参数顺序一律原样保留，
        不重复编码（不会出现 %252f / %2523）；
      - 退出码 0、stderr 为空、stdout 恰一行 JSON，新增一条 success /
        http_status=200 / reason=ok 记录；stdout 与数据库中的 url 都是
        输入的中文原文，不做任何规范化。
  * 把中文预先百分号编码后的等价地址再 check 一次：实际请求目标与上面
    完全相同，但落库与输出保留各自当次输入，两条记录不得被合并。
  * recent --url 分别以两个原始地址精确查询：各自只命中自己的记录，
    返回字段与对应 check 的 JSON 逐字段一致；recent 为只读操作：退出码 0、
    stderr 为空、不发请求、不改变表结构与既有记录。
  * 在中文样本末尾追加原始 '#'（片段）后：check 在网络与数据库访问之前
    即以退出码 2 拒绝，stdout 为空、stderr 说明不接受片段；服务收不到
    新请求，数据库记录保持不变。

全部使用独立临时目录中的数据库与绑定 127.0.0.1 的随机端口服务，
不访问公网、不依赖固定空闲端口或既有历史文件；重复执行相互独立。
运行：python3 -m unittest test_check_unicode_url
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

RECORD_FIELDS = ("id", "url", "checked_at", "elapsed_ms",
                 "status", "http_status", "reason")

# 输入 1：中文路径段 + 中文查询值，与既有百分号编码 %2f/%23 及查询 '+' 混排
RAW_PATH_AND_QUERY = "/健康/%2f?name=中文&tag=%23&x=a+b"
RAW_URL_SUFFIX = RAW_PATH_AND_QUERY

# 服务端应当收到的唯一请求目标：仅非 ASCII 字符按 UTF-8 字节大写百分号编码；
# 原有 %2f（小写 f）、%23、'+' 与参数顺序保持不变
EXPECTED_TARGET = (
    "/%E5%81%A5%E5%BA%B7/%2f"
    "?name=%E4%B8%AD%E6%96%87&tag=%23&x=a+b"
)

# 输入 2：把同一目标的中文预先编码后的等价地址；请求目标应与输入 1 相同，
# 但它是另一个原始字符串，落库与输出都必须保留该形态
PREENCODED_PATH_AND_QUERY = (
    "/%E5%81%A5%E5%BA%B7/%2f"
    "?name=%E4%B8%AD%E6%96%87&tag=%23&x=a+b"
)


def run_cli(db_path, *args):
    """以子进程运行 healthcheck.py，返回 CompletedProcess。"""
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *args]
    return subprocess.run(cmd, capture_output=True, text=True)


def run_check(db_path, url, *extra):
    return run_cli(db_path, "check", "--url", url, *extra)


def run_recent(db_path, *extra):
    return run_cli(db_path, "recent", *extra)


def read_checks_rows(db_path):
    """直接读取 checks 表全部行（按 id 升序），用于核对落库内容。"""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT id, url, checked_at, elapsed_ms, status, "
            "http_status, reason FROM checks ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def table_names(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()


def rows_as_records(rows):
    return [dict(zip(RECORD_FIELDS, row)) for row in rows]


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """记录原始请求目标并对一切 GET 返回 200，抑制日志噪音。"""

    def log_message(self, *args):
        pass

    def do_GET(self):
        # self.path 是请求行上的原始请求目标（已编码形态），原样记录
        self.server.requests.append(self.path)
        body = b"ok"
        try:
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            # 客户端读完即关闭时忽略写失败
            pass


def start_server():
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _RecordingHandler
    )
    server.daemon_threads = True
    server.requests = []  # 已收到的请求目标列表
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class UnicodeUrlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-unicode-test-"))
        self._servers = []

    def tearDown(self):
        # 即使用例断言失败也必须释放本机服务与临时数据
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

    def assert_success_record(self, record, expected_id, expected_url, label):
        """核对一条 check 成功记录的公共字段（不含毫秒具体值）。"""
        self.assertEqual(
            sorted(record.keys()), sorted(RECORD_FIELDS),
            f"{label}：JSON 字段集合必须为 {sorted(RECORD_FIELDS)}，"
            f"实际 {sorted(record.keys())}",
        )
        self.assertEqual(
            record["id"], expected_id,
            f"{label}：记录 id 应为 {expected_id}，实际 {record['id']}",
        )
        self.assertEqual(
            record["url"], expected_url,
            f"{label}：输出 url 必须与输入原文逐字相同（不编码、不规范化），"
            f"期望 {expected_url!r}，实际 {record['url']!r}",
        )
        self.assertEqual(
            record["status"], "success",
            f"{label}：status 应为 success，实际 {record['status']!r}",
        )
        self.assertEqual(
            record["http_status"], 200,
            f"{label}：http_status 应为 200，实际 {record['http_status']!r}",
        )
        self.assertEqual(
            record["reason"], "ok",
            f"{label}：reason 应为 ok，实际 {record['reason']!r}",
        )
        self.assertIsInstance(record["elapsed_ms"], int)
        self.assertNotIsInstance(record["elapsed_ms"], bool)
        self.assertGreaterEqual(record["elapsed_ms"], 0)
        self.assertIsInstance(record["checked_at"], str)
        self.assertTrue(record["checked_at"])

    # ---- 主样本：中文按 UTF-8 字节大写百分号编码，其余原样，仅一次 GET ----

    def test_check_unicode_path_and_query_encoded_once(self):
        server = self._start()
        port = server.server_address[1]
        db = self.tmp / "unicode.sqlite"
        url = f"http://127.0.0.1:{port}{RAW_URL_SUFFIX}"

        proc = run_check(db, url)

        self.assertEqual(
            proc.returncode, 0,
            f"输入 {url!r}：期望退出码 0，实际 {proc.returncode}，"
            f"stdout={proc.stdout!r}，stderr={proc.stderr!r}",
        )
        self.assertEqual(
            proc.stderr, "",
            f"输入 {url!r}：stderr 应为空，实际 {proc.stderr!r}",
        )
        lines = proc.stdout.splitlines()
        self.assertEqual(
            len(lines), 1,
            f"输入 {url!r}：stdout 应恰有一行 JSON，实际 {proc.stdout!r}",
        )
        record = json.loads(lines[0])
        self.assert_success_record(record, 1, url, f"输入 {url!r}")

        # 服务只收到一次 GET，请求目标准确为编码后的形态
        self.assertEqual(
            server.requests, [EXPECTED_TARGET],
            f"输入 {url!r}：服务应只收到一次 GET 且请求目标为 "
            f"{EXPECTED_TARGET!r}（中文按 UTF-8 字节大写百分号编码，"
            "原 %2f 大小写、%23、'+' 与参数顺序不变），"
            f"实际 {server.requests!r}",
        )

        # 落库恰一条记录，与 stdout 逐字段一致，url 为中文原文
        rows = read_checks_rows(db)
        self.assertEqual(
            len(rows), 1,
            f"输入 {url!r}：数据库应恰好新增一条记录，实际 {len(rows)} 条",
        )
        db_record = rows_as_records(rows)[0]
        self.assertEqual(
            db_record, record,
            f"输入 {url!r}：落库记录应与 stdout 逐字段一致，"
            f"期望 {record!r}，实际 {db_record!r}",
        )
        self.assertEqual(
            rows[0][1], url,
            f"输入 {url!r}：数据库 url 必须保留中文原文，"
            f"期望 {url!r}，实际 {rows[0][1]!r}",
        )

    # ---- 预编码等价地址：请求目标相同，但保留各自输入，不合并记录 ----

    def test_check_preencoded_url_same_target_but_separate_records(self):
        server = self._start()
        port = server.server_address[1]
        db = self.tmp / "unicode.sqlite"
        raw_url = f"http://127.0.0.1:{port}{RAW_PATH_AND_QUERY}"
        pre_url = f"http://127.0.0.1:{port}{PREENCODED_PATH_AND_QUERY}"

        # 第一次：中文原文
        proc_raw = run_check(db, raw_url)
        self.assertEqual(proc_raw.returncode, 0, proc_raw.stderr)
        self.assertEqual(proc_raw.stderr, "")
        record_raw = json.loads(proc_raw.stdout)
        self.assert_success_record(record_raw, 1, raw_url,
                                   f"输入 {raw_url!r}")

        # 第二次：预编码地址
        proc_pre = run_check(db, pre_url)
        self.assertEqual(
            proc_pre.returncode, 0,
            f"输入 {pre_url!r}：期望退出码 0，实际 {proc_pre.returncode}，"
            f"stderr={proc_pre.stderr!r}",
        )
        self.assertEqual(
            proc_pre.stderr, "",
            f"输入 {pre_url!r}：stderr 应为空，实际 {proc_pre.stderr!r}",
        )
        record_pre = json.loads(proc_pre.stdout)
        self.assert_success_record(record_pre, 2, pre_url,
                                   f"输入 {pre_url!r}")

        # 两次实际请求目标完全相同，且各自只请求一次（共两次，目标相同）
        self.assertEqual(
            server.requests, [EXPECTED_TARGET, EXPECTED_TARGET],
            f"输入 {raw_url!r} 与 {pre_url!r}：两次实际请求目标都应为 "
            f"{EXPECTED_TARGET!r}，实际 {server.requests!r}",
        )

        # 数据库有两条记录，id 不同，url 各自保留当次输入，不被合并
        rows = read_checks_rows(db)
        self.assertEqual(
            len(rows), 2,
            "两个不同的原始 URL 即使请求目标相同，也必须各落一条记录，"
            f"实际 {len(rows)} 条",
        )
        self.assertEqual(
            rows[0][1], raw_url,
            f"第一条记录 url 应为中文原文 {raw_url!r}，实际 {rows[0][1]!r}",
        )
        self.assertEqual(
            rows[1][1], pre_url,
            "第二条记录 url 应保留预编码当次输入 "
            f"{pre_url!r}，实际 {rows[1][1]!r}",
        )
        self.assertEqual(rows_as_records(rows), [record_raw, record_pre])

    # ---- recent --url 分别精确查询：只命中各自记录，只读 ----

    def test_recent_url_filters_each_raw_input_independently(self):
        server = self._start()
        port = server.server_address[1]
        db = self.tmp / "unicode.sqlite"
        raw_url = f"http://127.0.0.1:{port}{RAW_PATH_AND_QUERY}"
        pre_url = f"http://127.0.0.1:{port}{PREENCODED_PATH_AND_QUERY}"

        proc_raw = run_check(db, raw_url)
        self.assertEqual(proc_raw.returncode, 0, proc_raw.stderr)
        record_raw = json.loads(proc_raw.stdout)

        proc_pre = run_check(db, pre_url)
        self.assertEqual(proc_pre.returncode, 0, proc_pre.stderr)
        record_pre = json.loads(proc_pre.stdout)

        requests_after_checks = list(server.requests)
        rows_after_checks = read_checks_rows(db)
        tables_after_checks = table_names(db)
        self.assertEqual(len(rows_after_checks), 2)

        # 以中文原文查询：只命中第一条，字段与对应 check 结果逐字段一致
        q_raw = run_recent(db, "--url", raw_url)
        self.assertEqual(
            q_raw.returncode, 0,
            f"recent --url {raw_url!r}：期望退出码 0，"
            f"实际 {q_raw.returncode}，stderr={q_raw.stderr!r}",
        )
        self.assertEqual(
            q_raw.stderr, "",
            f"recent --url {raw_url!r}：stderr 应为空，"
            f"实际 {q_raw.stderr!r}",
        )
        got_raw = json.loads(q_raw.stdout)
        self.assertEqual(
            got_raw, [record_raw],
            f"recent --url {raw_url!r}：应只命中中文原文那一条且字段与 "
            f"check 输出一致，期望 {[record_raw]!r}，实际 {got_raw!r}",
        )

        # 以预编码地址查询：只命中第二条，不与中文原文记录串扰
        q_pre = run_recent(db, "--url", pre_url)
        self.assertEqual(
            q_pre.returncode, 0,
            f"recent --url {pre_url!r}：期望退出码 0，"
            f"实际 {q_pre.returncode}，stderr={q_pre.stderr!r}",
        )
        self.assertEqual(
            q_pre.stderr, "",
            f"recent --url {pre_url!r}：stderr 应为空，"
            f"实际 {q_pre.stderr!r}",
        )
        got_pre = json.loads(q_pre.stdout)
        self.assertEqual(
            got_pre, [record_pre],
            f"recent --url {pre_url!r}：应只命中预编码那一条且字段与 "
            f"check 输出一致，期望 {[record_pre]!r}，实际 {got_pre!r}",
        )

        # recent 只读：不发新请求，表结构与既有记录不变
        self.assertEqual(
            server.requests, requests_after_checks,
            "recent 查询期间不得向服务发起任何请求，"
            f"查询前 {requests_after_checks!r}，实际 {server.requests!r}",
        )
        self.assertEqual(
            read_checks_rows(db), rows_after_checks,
            "recent 查询不得改变既有记录",
        )
        self.assertEqual(
            table_names(db), tables_after_checks,
            "recent 查询不得改变表结构",
        )

    # ---- 中文样本末尾追加原始 #：退出码 2、无请求、记录不变 ----

    def test_check_unicode_url_with_fragment_rejected_before_request(self):
        server = self._start()
        port = server.server_address[1]
        db = self.tmp / "unicode.sqlite"
        raw_url = f"http://127.0.0.1:{port}{RAW_PATH_AND_QUERY}"

        # 先落一条合法的中文记录，作为“记录不变”的对照基线
        proc_base = run_check(db, raw_url)
        self.assertEqual(proc_base.returncode, 0, proc_base.stderr)
        rows_before = read_checks_rows(db)
        tables_before = table_names(db)
        self.assertEqual(len(rows_before), 1)
        requests_before = list(server.requests)
        self.assertEqual(requests_before, [EXPECTED_TARGET])

        # 在中文样本末尾追加原始 '#'（空片段）
        frag_url = raw_url + "#"
        proc = run_check(db, frag_url)

        self.assertEqual(
            proc.returncode, 2,
            f"输入 {frag_url!r}：期望退出码 2，实际 {proc.returncode}，"
            f"stdout={proc.stdout!r}，stderr={proc.stderr!r}",
        )
        self.assertEqual(
            proc.stdout, "",
            f"输入 {frag_url!r}：stdout 应为空，实际 {proc.stdout!r}",
        )
        self.assertTrue(
            proc.stderr.startswith("healthcheck: error:"),
            f"输入 {frag_url!r}：stderr 应以固定前缀开头，"
            f"实际 {proc.stderr!r}",
        )
        self.assertIn(
            "片段", proc.stderr,
            f"输入 {frag_url!r}：stderr 应说明片段不合法，"
            f"实际 {proc.stderr!r}",
        )
        self.assertIn(
            frag_url, proc.stderr,
            f"输入 {frag_url!r}：stderr 应回显原始地址，"
            f"实际 {proc.stderr!r}",
        )
        self.assertNotIn(
            "Traceback", proc.stderr,
            f"输入 {frag_url!r}：stderr 不应出现 Python 回溯，"
            f"实际 {proc.stderr!r}",
        )

        # 拒绝发生在网络与数据库访问之前：无新请求、表结构与记录不变
        self.assertEqual(
            server.requests, requests_before,
            f"输入 {frag_url!r}：服务不应收到新请求，"
            f"期望 {requests_before!r}，实际 {server.requests!r}",
        )
        self.assertEqual(
            read_checks_rows(db), rows_before,
            f"输入 {frag_url!r}：拒绝后数据库记录必须保持不变",
        )
        self.assertEqual(
            table_names(db), tables_before,
            f"输入 {frag_url!r}：拒绝后表结构必须保持不变",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
