#!/usr/bin/env python3
"""healthcheck.py 中文（非 ASCII）URL 编码行为的独立回归测试。

覆盖既有约定（本文件只新增测试，不修改产品源码与文档）：

  * check 的输入
    http://127.0.0.1:<显式端口>/健康/%2f?name=中文&tag=%23&x=a+b 合法：
    发送前仅把非 ASCII 字符按 UTF-8 字节转为大写百分号编码，服务实际收到的
    请求目标为
    /%E5%81%A5%E5%BA%B7/%2f?name=%E4%B8%AD%E6%96%87&tag=%23&x=a+b；
    原有 %2f（小写）、%23、查询中的 '+' 与参数顺序原样保留，不重复编码。
  * 服务只收到一次 GET 并返回 200：check 退出码 0、stderr 为空、stdout 一行
    JSON，新增一条 status=success、http_status=200、reason=ok 的记录；
    stdout 与 SQLite 中的 url 都与中文输入原文逐字相同。
  * 再检查把中文预先百分号编码的等价地址：实际请求目标完全相同，但它属于
    另一次原始输入，落库为两条独立记录，绝不按“规范化后的 URL”合并。
  * recent --url 分别按两个原始地址精确查询：各自只命中自己的记录，字段与
    对应 check 的 JSON 输出逐字段一致，退出码 0、stderr 为空；查询只读：
    不发请求、不改变表结构或已有记录。
  * 中文样本末尾追加原始 '#' 后：片段校验先于网络与数据库访问，check 退出码
    2、stdout 为空、stderr 说明不接受片段，服务收不到新请求，记录不变。

仅使用 Python 3 标准库；样本服务绑定 127.0.0.1 的临时端口，数据库放在
独立临时目录，资源由测试自行准备与释放，不依赖公网、既有服务或历史文件。
运行：python -m unittest test_check_unicode_url
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
from datetime import datetime, timedelta

SCRIPT = pathlib.Path(__file__).resolve().parent / "healthcheck.py"

RECORD_FIELDS = ("id", "url", "checked_at", "elapsed_ms",
                 "status", "http_status", "reason")

# 中文样本的路径+查询（输入原文，含未编码中文、原有 %2f、%23、'+'）
RAW_PATH_QUERY = "/健康/%2f?name=中文&tag=%23&x=a+b"

# 服务端应实际收到的请求目标：仅中文按 UTF-8 字节变成大写百分号编码，
# 其余 ASCII 内容（含小写 %2f、%23、'+'、参数顺序）原样保留
EXPECTED_TARGET = (
    "/%E5%81%A5%E5%BA%B7/%2f"
    "?name=%E4%B8%AD%E6%96%87&tag=%23&x=a+b"
)

# 把中文预先编码后的等价地址（路径+查询部分），线上字节应与 EXPECTED_TARGET 相同
ENCODED_PATH_QUERY = (
    "/%E5%81%A5%E5%BA%B7/%2f"
    "?name=%E4%B8%AD%E6%96%87&tag=%23&x=a+b"
)


def run_cli(db_path, *extra):
    """以子进程运行 healthcheck.py，返回 CompletedProcess（UTF-8 解码）。"""
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


def run_check(db_path, url):
    return run_cli(db_path, "check", "--url", url)


def run_recent(db_path, url):
    return run_cli(db_path, "recent", "--url", url)


def read_checks_rows(db_path):
    """按 id 升序读取 checks 表全部行，用于核对落库内容。"""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT id, url, checked_at, elapsed_ms, status, "
            "http_status, reason FROM checks ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def rows_as_records(rows):
    return [dict(zip(RECORD_FIELDS, row)) for row in rows]


def schema_snapshot(db_path):
    """读取全部表名与 checks 表定义，用于证明 recent 不改变表结构。"""
    conn = sqlite3.connect(str(db_path))
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        columns = conn.execute("PRAGMA table_info(checks)").fetchall()
        return tables, columns
    finally:
        conn.close()


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """对任意 GET 返回 200，并原样记录请求目标（含查询），抑制日志噪音。"""

    def log_message(self, *args):
        pass

    def do_GET(self):
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
    server.requests = []  # 已收到的请求目标，用于核对次数与线上字节
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class UnicodeUrlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-unicode-test-"))
        self._servers = []

    def tearDown(self):
        # 即使断言失败也必须停掉本机服务并清掉临时数据
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

    # ---- 公共断言 ----

    def assert_success_check(self, proc, url):
        """成功 check 的公共契约：退出码 0、stderr 空、stdout 一行 success JSON。

        返回解析出的记录字典。失败信息包含具体输入、预期与实际。
        """
        label = f"输入 {url!r}"
        self.assertEqual(
            proc.returncode, 0,
            f"{label}：预期 check 退出码 0，实际 {proc.returncode}，"
            f"stdout={proc.stdout!r}，stderr={proc.stderr!r}",
        )
        self.assertEqual(
            proc.stderr, "",
            f"{label}：预期 stderr 为空，实际 {proc.stderr!r}",
        )
        lines = proc.stdout.splitlines()
        self.assertEqual(
            len(lines), 1,
            f"{label}：预期 stdout 恰为一行 JSON，实际 {proc.stdout!r}",
        )
        record = json.loads(lines[0])
        self.assertEqual(
            sorted(record.keys()), sorted(RECORD_FIELDS),
            f"{label}：JSON 字段集合预期 {sorted(RECORD_FIELDS)}，"
            f"实际 {sorted(record.keys())}",
        )
        self.assertEqual(
            record["url"], url,
            f"{label}：输出 url 必须与输入原文逐字相同，"
            f"预期 {url!r}，实际 {record['url']!r}",
        )
        self.assertEqual(
            record["status"], "success",
            f"{label}：预期 status='success'，实际 {record['status']!r}",
        )
        self.assertEqual(
            record["http_status"], 200,
            f"{label}：预期 http_status=200，实际 {record['http_status']!r}",
        )
        self.assertEqual(
            record["reason"], "ok",
            f"{label}：预期 reason='ok'，实际 {record['reason']!r}",
        )
        self.assertIsInstance(record["elapsed_ms"], int)
        self.assertNotIsInstance(record["elapsed_ms"], bool)
        self.assertGreaterEqual(
            record["elapsed_ms"], 0,
            f"{label}：elapsed_ms 必须为非负整数，实际 {record['elapsed_ms']!r}",
        )
        checked_at = datetime.fromisoformat(record["checked_at"])
        self.assertIsNotNone(
            checked_at.tzinfo,
            f"{label}：checked_at 必须带时区，实际 {record['checked_at']!r}",
        )
        self.assertEqual(
            checked_at.utcoffset(), timedelta(0),
            f"{label}：checked_at 必须为 UTC 时间，实际 {record['checked_at']!r}",
        )
        return record

    def assert_wire_target_encoding(self, actual):
        """逐项核对线上请求目标的编码约定（每项失败都能指出具体差异）。"""
        label = f"实际请求目标 {actual!r}"
        with self.subTest("全部为 ASCII（无原始中文残留）", target=actual):
            self.assertTrue(
                actual.isascii(),
                f"{label}：编码后不应残留非 ASCII 字符",
            )
        with self.subTest("中文路径段为大写 UTF-8 百分号编码"):
            self.assertIn(
                "/%E5%81%A5%E5%BA%B7/", actual,
                f"{label}：预期路径段“健康”编码为 "
                f"/%E5%81%A5%E5%BA%B7/（健=%E5%81%A5，康=%E5%BA%B7）",
            )
            self.assertNotIn(
                "%e5", actual,
                f"{label}：百分号编码十六进制字母必须大写，发现小写 %e5",
            )
        with self.subTest("中文查询值为大写 UTF-8 百分号编码"):
            self.assertIn(
                "name=%E4%B8%AD%E6%96%87", actual,
                f"{label}：预期查询值“中文”编码为 "
                f"%E4%B8%AD%E6%96%87（中=%E4%B8%AD，文=%E6%96%87）",
            )
        with self.subTest("原有 %2f 大小写保持不变（仍为小写）"):
            self.assertIn("%2f", actual, f"{label}：预期保留原有小写 %2f")
            self.assertNotIn(
                "%2F", actual,
                f"{label}：不得把原有小写 %2f 改成大写 %2F",
            )
        with self.subTest("%23 原样保留且不被重复编码"):
            self.assertIn("tag=%23", actual, f"{label}：预期保留 tag=%23")
            self.assertNotIn(
                "%25", actual,
                f"{label}：不得对既有百分号编码二次编码（发现 %25）",
            )
        with self.subTest("查询中的加号原样保留"):
            self.assertIn(
                "x=a+b", actual,
                f"{label}：预期加号原样保留为 x=a+b，不得改成 %20 或空格",
            )
            self.assertNotIn("x=a%20b", actual)
        with self.subTest("查询参数顺序保持 name → tag → x"):
            pos_name = actual.index("name=")
            pos_tag = actual.index("tag=")
            pos_x = actual.index("x=a+b")
            self.assertTrue(
                pos_name < pos_tag < pos_x,
                f"{label}：参数顺序预期 name<tag<x，"
                f"实际位置 name={pos_name}, tag={pos_tag}, x={pos_x}",
            )
        with self.subTest("与完整预期请求目标逐字相等"):
            self.assertEqual(
                actual, EXPECTED_TARGET,
                f"请求目标编码结果预期 {EXPECTED_TARGET!r}，实际 {actual!r}",
            )

    # ---- 场景一：中文原文 URL 仅在线上编码，输出与落库保留原文 ----

    def test_raw_unicode_url_encoded_on_wire_preserved_everywhere(self):
        server = self._start()
        port = server.server_address[1]
        db = self.tmp / "unicode.sqlite"
        url = f"http://127.0.0.1:{port}{RAW_PATH_QUERY}"

        proc = run_check(db, url)
        record = self.assert_success_check(proc, url)

        # 服务只收到一次 GET，且请求目标准确为预期的编码结果
        self.assertEqual(
            server.requests, [EXPECTED_TARGET],
            f"输入 {url!r}：预期服务只收到一次 GET 且目标为 "
            f"{EXPECTED_TARGET!r}，实际收到 {server.requests!r}",
        )
        self.assert_wire_target_encoding(server.requests[0])

        # 数据库恰好新增一条记录：success/200/ok，url 为中文原文
        rows = read_checks_rows(db)
        self.assertEqual(
            len(rows), 1,
            f"输入 {url!r}：预期数据库新增 1 条记录，实际 {len(rows)} 条",
        )
        db_record = rows_as_records(rows)[0]
        self.assertEqual(
            db_record, record,
            f"输入 {url!r}：落库记录预期与 stdout 逐字段一致，"
            f"stdout={record!r}，落库={db_record!r}",
        )
        self.assertEqual(
            rows[0][1], url,
            f"输入 {url!r}：数据库 url 预期为输入原文 {url!r}，"
            f"实际 {rows[0][1]!r}（不得保存编码后的地址）",
        )

    # ---- 场景二：预先编码的等价地址线上目标相同，但记录不合并 ----

    def test_preencoded_url_same_wire_target_kept_as_separate_record(self):
        server = self._start()
        port = server.server_address[1]
        db = self.tmp / "unicode.sqlite"
        raw_url = f"http://127.0.0.1:{port}{RAW_PATH_QUERY}"
        encoded_url = f"http://127.0.0.1:{port}{ENCODED_PATH_QUERY}"

        proc_raw = run_check(db, raw_url)
        record_raw = self.assert_success_check(proc_raw, raw_url)

        proc_enc = run_check(db, encoded_url)
        record_enc = self.assert_success_check(proc_enc, encoded_url)

        # 两次 check 的实际请求目标必须完全相同，且各自只探测一次
        self.assertEqual(
            server.requests, [EXPECTED_TARGET, EXPECTED_TARGET],
            f"两次输入预期发送相同请求目标 {EXPECTED_TARGET!r} 各一次，"
            f"实际 {server.requests!r}",
        )

        # 两次输入是不同的原始字符串：两条独立记录，不得按编码结果合并
        self.assertNotEqual(
            record_raw["id"], record_enc["id"],
            "两次 check 应生成不同 id 的记录",
        )
        rows = read_checks_rows(db)
        self.assertEqual(
            len(rows), 2,
            f"预期 2 条独立记录，实际 {len(rows)} 条：{rows!r}",
        )
        self.assertEqual(
            [row[1] for row in rows], [raw_url, encoded_url],
            "两条记录必须分别保留各自的输入原文，不能合并成同一条 URL",
        )
        self.assertEqual(
            rows_as_records(rows), [record_raw, record_enc],
            "落库两条记录应分别与两次 check 的 stdout 一致",
        )

    # ---- 场景三：recent --url 分别精确命中，且只读 ----

    def test_recent_url_matches_each_raw_input_independently_and_readonly(self):
        server = self._start()
        port = server.server_address[1]
        db = self.tmp / "unicode.sqlite"
        raw_url = f"http://127.0.0.1:{port}{RAW_PATH_QUERY}"
        encoded_url = f"http://127.0.0.1:{port}{ENCODED_PATH_QUERY}"

        record_raw = self.assert_success_check(run_check(db, raw_url), raw_url)
        record_enc = self.assert_success_check(
            run_check(db, encoded_url), encoded_url
        )

        rows_before = read_checks_rows(db)
        tables_before, columns_before = schema_snapshot(db)
        requests_before = list(server.requests)

        # 用中文原文查询：只命中原文那条，字段与对应 check 输出一致
        proc_raw = run_recent(db, raw_url)
        self.assertEqual(
            proc_raw.returncode, 0,
            f"recent --url {raw_url!r}：预期退出码 0，"
            f"实际 {proc_raw.returncode}，stderr={proc_raw.stderr!r}",
        )
        self.assertEqual(
            proc_raw.stderr, "",
            f"recent --url {raw_url!r}：预期 stderr 为空，"
            f"实际 {proc_raw.stderr!r}",
        )
        raw_rows = json.loads(proc_raw.stdout)
        self.assertEqual(
            raw_rows, [record_raw],
            f"recent --url 原文 {raw_url!r}：预期只命中原文记录 "
            f"{[record_raw]!r}，实际 {raw_rows!r}（不得混入预编码地址记录）",
        )

        # 用预编码地址查询：只命中预编码那条
        proc_enc = run_recent(db, encoded_url)
        self.assertEqual(
            proc_enc.returncode, 0,
            f"recent --url {encoded_url!r}：预期退出码 0，"
            f"实际 {proc_enc.returncode}，stderr={proc_enc.stderr!r}",
        )
        self.assertEqual(
            proc_enc.stderr, "",
            f"recent --url {encoded_url!r}：预期 stderr 为空，"
            f"实际 {proc_enc.stderr!r}",
        )
        enc_rows = json.loads(proc_enc.stdout)
        self.assertEqual(
            enc_rows, [record_enc],
            f"recent --url 预编码地址 {encoded_url!r}：预期只命中该地址记录 "
            f"{[record_enc]!r}，实际 {enc_rows!r}（不得混入中文原文记录）",
        )

        # 两个结果必须互不相同，证明两种 URL 没有被合并查询
        self.assertNotEqual(
            raw_rows[0]["id"], enc_rows[0]["id"],
            "两个原始地址的 recent 查询必须各自命中不同记录",
        )

        # recent 只读：不发请求、不改已有记录、不改表结构
        self.assertEqual(
            server.requests, requests_before,
            "recent 查询不得发起任何网络请求，"
            f"查询前 {requests_before!r}，查询后 {server.requests!r}",
        )
        self.assertEqual(
            read_checks_rows(db), rows_before,
            "recent 查询不得改变已有记录",
        )
        tables_after, columns_after = schema_snapshot(db)
        self.assertEqual(tables_after, tables_before, "recent 不得改变表集合")
        self.assertEqual(
            columns_after, columns_before, "recent 不得改变 checks 表结构"
        )

    # ---- 场景四：中文样本末尾追加原始 '#' 在探测前被拒绝 ----

    def test_fragment_appended_to_unicode_url_rejected_before_request(self):
        server = self._start()
        port = server.server_address[1]
        db = self.tmp / "unicode.sqlite"
        raw_url = f"http://127.0.0.1:{port}{RAW_PATH_QUERY}"
        fragment_url = raw_url + "#"

        # 先落一条合法记录，随后证明片段输入不会改动它
        self.assert_success_check(run_check(db, raw_url), raw_url)
        rows_before = read_checks_rows(db)
        tables_before, columns_before = schema_snapshot(db)
        requests_before = list(server.requests)

        proc = run_check(db, fragment_url)
        self.assertEqual(
            proc.returncode, 2,
            f"输入 {fragment_url!r}：预期退出码 2，实际 {proc.returncode}，"
            f"stdout={proc.stdout!r}，stderr={proc.stderr!r}",
        )
        self.assertEqual(
            proc.stdout, "",
            f"输入 {fragment_url!r}：预期 stdout 为空，实际 {proc.stdout!r}",
        )
        self.assertTrue(
            proc.stderr.startswith("healthcheck: error:"),
            f"输入 {fragment_url!r}：stderr 预期以固定前缀开头，"
            f"实际 {proc.stderr!r}",
        )
        self.assertIn(
            "片段", proc.stderr,
            f"输入 {fragment_url!r}：stderr 应说明片段不合法，"
            f"实际 {proc.stderr!r}",
        )
        self.assertIn(
            fragment_url, proc.stderr,
            f"输入 {fragment_url!r}：stderr 应回显原始地址，"
            f"实际 {proc.stderr!r}",
        )
        self.assertNotIn(
            "Traceback", proc.stderr,
            f"输入 {fragment_url!r}：stderr 不应出现 Python 回溯，"
            f"实际 {proc.stderr!r}",
        )

        # 拒绝先于网络与数据库：服务收不到新请求，表结构与记录不变
        self.assertEqual(
            server.requests, requests_before,
            f"片段输入 {fragment_url!r} 不得触发任何请求，"
            f"查询前 {requests_before!r}，之后 {server.requests!r}",
        )
        self.assertEqual(
            read_checks_rows(db), rows_before,
            f"片段输入 {fragment_url!r} 不得改动已有记录",
        )
        tables_after, columns_after = schema_snapshot(db)
        self.assertEqual(tables_after, tables_before)
        self.assertEqual(columns_after, columns_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
