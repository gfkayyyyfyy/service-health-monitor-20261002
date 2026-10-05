#!/usr/bin/env python3
"""streak 子命令「只查询、不探测」的专门回归测试。

与 test_streak.py 的分工：后者用纯临时 SQLite 数据验证连续失败统计与
--threshold 判定逻辑；本模块额外在 127.0.0.1 上启动一个可记录请求次数
的真实演示 HTTP 服务，通过公开命令入口（subprocess 调用 healthcheck.py
streak）观察查询期间是否发生 GET——零请求的结论来自服务端实际计数，
而不是仅凭 CLI 输出推断。

覆盖四条路径：
- 样本一：同一目标 id 1-3 为 success/failure/failure（其余字段符合现有
  记录要求），服务当前返回 200：省略阈值时 latest_id=3、
  consecutive_failures=2；--threshold 2 时整数 threshold=2、
  threshold_reached=true；两次查询服务收到的请求数始终为零。
- 样本二：在这些历史之后再保存 id=4 的 success，服务当前改为返回 503：
  查询仍只看历史——latest_id=4、连续失败次数为 0；阈值 2 判定 false，
  且服务请求数仍为零。
- 无历史：服务运行中，数据库文件及其父目录均不存在：返回输入 URL 原文、
  latest_id 为 null、连续失败次数为 0；阈值 2 判定 false；不创建任何
  路径，也不请求服务。
- 参数拒绝：--threshold 0 在访问数据库前以退出码 2 拒绝，stdout 为空、
  stderr 含 threshold，服务请求数为零。

所有正常查询共同约定：退出码 0、stderr 为空、stdout 恰好一行现有格式
的紧凑 JSON。观察服务本身必须被证明能真实响应并计数（测试进程直接发
一次 GET 核对状态码与计数后清零），随后 streak 查询的计数必须保持为零。

全部使用临时目录中的 SQLite 数据，服务绑定 127.0.0.1 并由本机分配端口，
不访问公网、不依赖固定端口；测试结束释放服务与临时目录。

运行：python -m unittest test_streak_no_probe
"""

import http.client
import http.server
import json
import os
import pathlib
import shutil
import stat
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "healthcheck.py"

HEALTH_PATH = "/health"

# 与 healthcheck.py 的 checks 表定义一致：测试只用标准库直接造数，
# 不经 check 探测，记录字段仍必须满足现有约束
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


class CountingHandler(http.server.BaseHTTPRequestHandler):
    """演示服务：对任意 GET 按 server.demo_status 返回，并原样记录路径。

    响应状态码可在运行时切换（200/503），用于区分「服务当前状态」与
    「数据库保存的历史状态」。所有请求目标追加到 server.demo_requests，
    供测试核对真实请求次数与路径。
    """

    def do_GET(self):
        with self.server.demo_lock:
            self.server.demo_requests.append(self.path)
        code = self.server.demo_status
        body = b"demo\n"
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            # 客户端读完响应头即关闭等写失败：演示服务无需处理
            pass

    def log_message(self, *args):
        pass


def start_demo_server():
    """绑定 127.0.0.1 的本机分配端口启动计数服务，返回 (server, thread)。"""
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), CountingHandler
    )
    server.daemon_threads = True
    server.demo_lock = threading.Lock()
    server.demo_requests = []
    server.demo_status = 200
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def direct_get(port, path=HEALTH_PATH, timeout=2.0):
    """测试进程自身直接发一次 GET，返回响应状态码。

    用于证明观察服务确实在监听、能响应、且计数有效——
    零请求结论不能只靠 streak 的输出内容推断。
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


class StreakNoProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-streak-noprobe-"))
        self.server, self.thread = start_demo_server()
        self.port = self.server.server_address[1]
        # 数据库保存的 URL 与 streak 查询原文始终使用同一个字符串
        self.url = f"http://127.0.0.1:{self.port}{HEALTH_PATH}"

    def tearDown(self):
        # 即使用例断言失败也必须释放本机服务与临时数据
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                os.chmod(os.path.join(root, name), stat.S_IRWXU)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 辅助 ----

    def run_streak(self, db_path, *extra):
        """通过公开命令入口执行 streak 查询。"""
        cmd = [
            sys.executable, str(SCRIPT), "--db", str(db_path),
            "streak", "--url", self.url, *extra,
        ]
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8"
        )

    def insert_records(self, db_path, rows):
        """rows: (id, status, http_status, reason) 元组序列。

        url 一律为当前服务的 /health（与查询原文一致），checked_at 取
        合法 UTC 时间，elapsed_ms 为非负整数。
        """
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(SCHEMA_SQL)
            conn.executemany(
                "INSERT INTO checks (id, url, checked_at, elapsed_ms, "
                "status, http_status, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        record_id,
                        self.url,
                        f"2026-10-04T00:00:{record_id:02d}.000000+00:00",
                        4,
                        status,
                        http_status,
                        reason,
                    )
                    for record_id, status, http_status, reason in rows
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def assert_observer_live(self, expected_status):
        """证明观察服务真实响应并计数：直接 GET 返回 expected_status，
        服务端恰好记录一次 /health；随后把计数清零，供后续零请求断言。"""
        self.assertEqual(direct_get(self.port), expected_status)
        with self.server.demo_lock:
            self.assertEqual(self.server.demo_requests, [HEALTH_PATH])
            self.server.demo_requests.clear()

    def assert_received_no_request(self):
        """streak 查询全程不得让服务收到任何请求。"""
        with self.server.demo_lock:
            self.assertEqual(self.server.demo_requests, [])

    def assertStreak(self, proc, latest_id, failures):
        """省略阈值：退出码 0、stderr 空、stdout 恰好一行三字段 JSON。"""
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertTrue(proc.stdout.endswith("\n"))
        self.assertEqual(proc.stdout.count("\n"), 1)
        data = json.loads(proc.stdout)
        self.assertEqual(
            data,
            {"url": self.url, "latest_id": latest_id,
             "consecutive_failures": failures},
        )
        self.assertEqual(
            list(data), ["url", "latest_id", "consecutive_failures"]
        )

    def assertStreakThreshold(self, proc, latest_id, failures,
                              threshold, reached):
        """带 --threshold：五字段、顺序与类型固定，threshold 为整数。"""
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertTrue(proc.stdout.endswith("\n"))
        self.assertEqual(proc.stdout.count("\n"), 1)
        data = json.loads(proc.stdout)
        self.assertEqual(
            data,
            {"url": self.url, "latest_id": latest_id,
             "consecutive_failures": failures,
             "threshold": threshold, "threshold_reached": reached},
        )
        self.assertEqual(
            list(data),
            ["url", "latest_id", "consecutive_failures",
             "threshold", "threshold_reached"],
        )
        self.assertIsInstance(data["threshold"], int)
        self.assertNotIsInstance(data["threshold"], bool)
        self.assertIsInstance(data["threshold_reached"], bool)

    # ---- 样本一：历史连败，服务当前 200，查询不探测 ----

    def test_failure_history_read_without_probe(self):
        db = self.tmp / "m.sqlite"
        self.insert_records(db, [
            (1, "success", 200, "ok"),
            (2, "failure", 503, "http_status"),
            (3, "failure", 503, "http_status"),
        ])
        # 服务当前实际返回 200，且观察通道（响应 + 计数）确实有效
        self.assertEqual(self.server.demo_status, 200)
        self.assert_observer_live(200)

        # 省略阈值：只看历史 → latest_id=3、连续失败 2，不发 GET
        proc = self.run_streak(db)
        self.assertStreak(proc, 3, 2)
        self.assert_received_no_request()

        # 提供 --threshold 2：整数 2、threshold_reached=true，仍不发 GET
        proc = self.run_streak(db, "--threshold", "2")
        self.assertStreakThreshold(proc, 3, 2, 2, True)
        self.assert_received_no_request()

    # ---- 样本二：历史之后最新为 success，服务当前 503，查询不探测 ----

    def test_latest_success_history_read_without_probe(self):
        db = self.tmp / "m.sqlite"
        self.insert_records(db, [
            (1, "success", 200, "ok"),
            (2, "failure", 503, "http_status"),
            (3, "failure", 503, "http_status"),
            (4, "success", 200, "ok"),
        ])
        # 服务当前改为实际返回 503：先证明 503 也能被真实观察到
        self.server.demo_status = 503
        self.assert_observer_live(503)

        # 最新历史是 success：latest_id=4、连续失败 0，不受当前 503 影响
        proc = self.run_streak(db)
        self.assertStreak(proc, 4, 0)
        self.assert_received_no_request()

        # 阈值 2：0 < 2 → false，零请求
        proc = self.run_streak(db, "--threshold", "2")
        self.assertStreakThreshold(proc, 4, 0, 2, False)
        self.assert_received_no_request()

    # ---- 无历史：数据库与父目录均不存在 ----

    def test_missing_db_and_parent_returns_empty_without_probe(self):
        missing_parent = self.tmp / "nope"
        db = missing_parent / "deep" / "m.sqlite"
        self.assert_observer_live(200)

        # 省略阈值：URL 原文、latest_id null、连续失败 0
        proc = self.run_streak(db)
        self.assertStreak(proc, None, 0)
        self.assert_received_no_request()
        # 不创建文件，也不创建父目录
        self.assertFalse(os.path.exists(missing_parent))

        # 阈值 2：空历史判定 false，同样不创建路径、不请求服务
        proc = self.run_streak(db, "--threshold", "2")
        self.assertStreakThreshold(proc, None, 0, 2, False)
        self.assert_received_no_request()
        self.assertFalse(os.path.exists(missing_parent))

    # ---- 参数拒绝：--threshold 0 ----

    def test_threshold_zero_rejected_without_probe(self):
        # 库里即便有历史，拒绝也发生在访问数据库之前
        db = self.tmp / "m.sqlite"
        self.insert_records(db, [
            (1, "success", 200, "ok"),
            (2, "failure", 503, "http_status"),
            (3, "failure", 503, "http_status"),
        ])
        self.assert_observer_live(200)

        proc = self.run_streak(db, "--threshold", "0")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(proc.stdout, "")
        self.assertIn("threshold", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assert_received_no_request()


if __name__ == "__main__":
    unittest.main(verbosity=2)
