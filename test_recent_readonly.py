#!/usr/bin/env python3
"""recent 只读行为的回归验证（标准库，独立本地数据，不依赖公网）。

直接运行：python3 test_recent_readonly.py
所有数据放在临时目录中，结束后自动清理；recent 全程不发起网络请求。
覆盖：
  样本一：有效但没有 checks 表的数据库 → []，且不建表、不改文件；
  样本二：旧版 check 写入的两条记录（成功 id=1、失败 id=2）：
          默认查询两条且失败在前，--limit 1 仅返回 id=2；只读权限位下结果相同；
  错误边界：父目录不存在、空 checks 表、路径为目录、非 SQLite 文件、
            零字节空库、读取权限不足、checks 表缺字段、无效 --limit。
"""

import http.server
import json
import os
import shutil
import socketserver
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "healthcheck.py")

_passed = 0


def run_recent(db_path, *extra):
    """运行 recent，返回 (returncode, stdout_bytes, stderr_bytes)。"""
    proc = subprocess.run(
        [sys.executable, SCRIPT, "--db", db_path, "recent", *extra],
        capture_output=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError(f"{name} 失败{'：' + detail if detail else ''}")
    global _passed
    _passed += 1
    print(f"  ok - {name}")


def expect_empty_list(rc, out, err, label):
    check(f"{label}：退出码 0", rc == 0, f"rc={rc} err={err!r}")
    check(f"{label}：stdout 为 []", out.strip() == b"[]", f"stdout={out!r}")


def expect_error_exit(rc, out, err, label):
    check(f"{label}：退出码 2", rc == 2, f"rc={rc}")
    check(f"{label}：stdout 为空", out == b"", f"stdout={out!r}")
    check(f"{label}：stderr 说明原因", bool(err.strip()), f"stderr={err!r}")


def table_names(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()


class _Always200(http.server.BaseHTTPRequestHandler):
    """对任意 GET 返回 200 的最小本机服务。"""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def start_local_server():
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Always200)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    return httpd, f"http://127.0.0.1:{port}/health?detail=1"


def run_check(db_path, url):
    proc = subprocess.run(
        [sys.executable, SCRIPT, "--db", db_path, "check", "--url", url],
        capture_output=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def sample_two_records(db_path):
    """用 check 命令制造两条旧版记录：id=1 成功、id=2 失败（仅本机 HTTP）。"""
    httpd, url = start_local_server()
    try:
        rc, _, err = run_check(db_path, url)
        check("样本二前置：成功 check 退出码 0", rc == 0, err.decode())
    finally:
        httpd.shutdown()
        httpd.server_close()

    # 服务已关闭：连接失败落库为 failure，退出码 1
    rc, _, err = run_check(db_path, url)
    check("样本二前置：失败 check 退出码 1", rc == 1, err.decode())
    return url


def assert_two_records_sample(db_path, url, label):
    """默认查询返回两条且失败在前；--limit 1 仅返回 id=2，退出码均为 0。"""
    rc, out, err = run_recent(db_path)
    check(f"{label}：默认查询退出码 0（含失败记录）", rc == 0,
          f"rc={rc} err={err!r}")
    records = json.loads(out)
    check(f"{label}：默认返回两条", len(records) == 2, f"records={records}")
    check(f"{label}：第一条是 id=2 的失败记录",
          records[0]["id"] == 2 and records[0]["status"] == "failure"
          and records[0]["http_status"] is None
          and records[0]["reason"] == "connection_error",
          f"record={records[0]}")
    check(f"{label}：第二条是 id=1 的成功记录",
          records[1]["id"] == 1 and records[1]["status"] == "success"
          and records[1]["http_status"] == 200
          and records[1]["reason"] == "ok",
          f"record={records[1]}")
    # 记录字段与原始 URL 保持不变
    for rec in records:
        check(f"{label}：记录字段完整",
              set(rec) == {"id", "url", "checked_at", "elapsed_ms", "status",
                           "http_status", "reason"},
              f"keys={set(rec)}")
        check(f"{label}：原始 URL 保持不变", rec["url"] == url,
              f"url={rec['url']!r}")
        check(f"{label}：elapsed_ms 为非负整数",
              isinstance(rec["elapsed_ms"], int) and rec["elapsed_ms"] >= 0)

    rc, out, err = run_recent(db_path, "--limit", "1")
    check(f"{label}：--limit 1 退出码 0", rc == 0, f"rc={rc} err={err!r}")
    limited = json.loads(out)
    check(f"{label}：--limit 1 只返回 id=2 原记录",
          len(limited) == 1 and limited[0] == records[0],
          f"limited={limited}")


def main():
    tmp = tempfile.mkdtemp(prefix="healthcheck-recent-test-")
    try:
        # ---------- 样本一：有效数据库没有 checks 表 ----------
        print("样本一：有效但无 checks 表的数据库")
        db1 = os.path.join(tmp, "no_checks.sqlite")
        conn = sqlite3.connect(db1)
        conn.execute("CREATE TABLE other (id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO other (note) VALUES ('keep me')")
        conn.commit()
        conn.close()
        before = set(os.listdir(tmp))
        st_before = os.stat(db1)

        rc, out, err = run_recent(db1)
        expect_empty_list(rc, out, err, "样本一")

        st_after = os.stat(db1)
        check("样本一：文件未被修改（大小/mtime 不变）",
              st_before.st_size == st_after.st_size
              and st_before.st_mtime_ns == st_after.st_mtime_ns)
        check("样本一：查询后仍无 checks 表，原有表保留",
              table_names(db1) == {"other", "sqlite_sequence"}
              or table_names(db1) == {"other"},
              f"tables={table_names(db1)}")
        conn = sqlite3.connect(db1)
        notes = conn.execute("SELECT note FROM other").fetchall()
        conn.close()
        check("样本一：其他表数据不变", notes == [("keep me",)], f"{notes=}")
        check("样本一：不产生 -wal/-shm 等侧车文件",
              set(os.listdir(tmp)) == before)

        # 空数据库（有有效文件头、无任何表）
        db_empty = os.path.join(tmp, "empty.sqlite")
        conn = sqlite3.connect(db_empty)
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()
        rc, out, err = run_recent(db_empty)
        expect_empty_list(rc, out, err, "空数据库")
        check("空数据库：查询后仍无 checks 表", table_names(db_empty) == set())

        # ---------- 样本二：两条历史记录（成功 + 失败） ----------
        print("样本二：含成功与失败各一条记录")
        db2 = os.path.join(tmp, "monitor.sqlite")
        url = sample_two_records(db2)
        assert_two_records_sample(db2, url, "样本二（可写库）")

        # checks 表存在但无记录
        db_norows = os.path.join(tmp, "norows.sqlite")
        conn = sqlite3.connect(db_norows)
        conn.executescript("""
        CREATE TABLE checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            elapsed_ms INTEGER NOT NULL CHECK (elapsed_ms >= 0),
            status TEXT NOT NULL CHECK (status IN ('success', 'failure')),
            http_status INTEGER,
            reason TEXT NOT NULL CHECK (reason IN
                ('ok', 'http_status', 'connection_error', 'timeout'))
        )""")
        conn.commit()
        conn.close()
        rc, out, err = run_recent(db_norows)
        expect_empty_list(rc, out, err, "checks 表无记录")

        # 只读权限位：文件 444、目录 555，查询仍成功且结果一致
        print("只读权限复验")
        os.chmod(db2, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        readonly_dir = os.path.join(tmp, "readonly_dir")
        os.mkdir(readonly_dir)
        db2_ro = os.path.join(readonly_dir, "monitor.sqlite")
        shutil.copyfile(db2, db2_ro)
        os.chmod(db2_ro, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        os.chmod(readonly_dir, stat.S_IRUSR | stat.S_IXUSR
                 | stat.S_IRGRP | stat.S_IXGRP)
        try:
            assert_two_records_sample(db2_ro, url, "样本二（只读库 444/555）")
        finally:
            os.chmod(readonly_dir, 0o755)
            os.chmod(db2_ro, 0o644)
            os.chmod(db2, 0o644)

        # WAL 模式库干净关闭（wal/shm 已回收）置于只读目录：
        # mode=ro 无法建 -shm，须由 immutable=1 回退完成查询
        wal_dir = os.path.join(tmp, "wal_readonly")
        os.mkdir(wal_dir)
        db_wal = os.path.join(wal_dir, "wal.sqlite")
        wconn = sqlite3.connect(db_wal)
        wconn.execute("PRAGMA journal_mode=WAL")
        wconn.close()

        wal_httpd, wal_url = start_local_server()
        try:
            rc, _, err = run_check(db_wal, wal_url)
            check("WAL 前置：成功 check 退出码 0", rc == 0, err.decode())
        finally:
            wal_httpd.shutdown()
            wal_httpd.server_close()
        rc, _, err = run_check(db_wal, wal_url)
        check("WAL 前置：失败 check 退出码 1", rc == 1, err.decode())

        wal_files_before = set(os.listdir(wal_dir))
        os.chmod(db_wal, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        os.chmod(wal_dir, stat.S_IRUSR | stat.S_IXUSR
                 | stat.S_IRGRP | stat.S_IXGRP)
        try:
            rc, out, err = run_recent(db_wal)
            check("WAL 只读库：退出码 0", rc == 0, f"rc={rc} err={err!r}")
            wal_records = json.loads(out)
            check("WAL 只读库：读到全部两条记录且倒序",
                  len(wal_records) == 2
                  and [r["id"] for r in wal_records] == [2, 1],
                  f"records={wal_records}")
            check("WAL 只读库：只读目录中未产生任何新文件",
                  set(os.listdir(wal_dir)) == wal_files_before,
                  f"diff={set(os.listdir(wal_dir)) ^ wal_files_before}")
        finally:
            os.chmod(wal_dir, 0o755)
            os.chmod(db_wal, 0o644)

        # ---------- 错误边界 ----------
        print("错误边界")

        # 路径不存在，父目录也不存在 → [] 且不创建任何东西
        missing_parent = os.path.join(tmp, "no-such-dir", "deep", "db.sqlite")
        rc, out, err = run_recent(missing_parent)
        expect_empty_list(rc, out, err, "路径与父目录均不存在")
        check("不存在路径：未创建父目录",
              not os.path.exists(os.path.dirname(missing_parent)))

        # 路径指向目录 → 退出码 2
        a_dir = os.path.join(tmp, "a_directory")
        os.mkdir(a_dir)
        rc, out, err = run_recent(a_dir)
        expect_error_exit(rc, out, err, "路径指向目录")

        # 文件不是有效 SQLite 数据库 → 退出码 2，且文件原样保留
        garbage = os.path.join(tmp, "garbage.sqlite")
        payload = b"this is definitely not a sqlite database\n" * 8
        with open(garbage, "wb") as f:
            f.write(payload)
        rc, out, err = run_recent(garbage)
        expect_error_exit(rc, out, err, "非 SQLite 文件")
        check("非 SQLite 文件：内容未被修复或覆盖",
              open(garbage, "rb").read() == payload)

        # 零字节文件（空库）→ [] 退出 0
        zero = os.path.join(tmp, "zero.sqlite")
        open(zero, "wb").close()
        rc, out, err = run_recent(zero)
        expect_empty_list(rc, out, err, "零字节空库")
        check("零字节文件：查询后仍为零字节", os.path.getsize(zero) == 0)

        # 读取权限不足 → 退出码 2（root 可绕过权限，跳过）
        if os.geteuid() != 0:
            noread = os.path.join(tmp, "noread.sqlite")
            shutil.copyfile(db2, noread)
            os.chmod(noread, 0)
            try:
                rc, out, err = run_recent(noread)
                expect_error_exit(rc, out, err, "读取权限不足")
            finally:
                os.chmod(noread, 0o644)
        else:
            print("  skip - 以 root 运行，无法验证权限不足")

        # checks 表缺少查询所需字段 → 退出码 2，不修复表结构
        broken = os.path.join(tmp, "broken.sqlite")
        conn = sqlite3.connect(broken)
        conn.execute("CREATE TABLE checks (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO checks (id) VALUES (1)")
        conn.commit()
        conn.close()
        rc, out, err = run_recent(broken)
        expect_error_exit(rc, out, err, "checks 表缺少字段")
        conn = sqlite3.connect(broken)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(checks)")]
        vals = conn.execute("SELECT id FROM checks").fetchall()
        conn.close()
        check("缺字段：原表结构与数据保持不变", cols == ["id"] and vals == [(1,)],
              f"{cols=} {vals=}")
        check("缺字段：stderr 指出缺少的字段", b"url" in err, err.decode())

        # 无效 --limit：退出码 2、stdout 为空
        for bad in ("0", "-1", "1.5", "abc"):
            rc, out, err = run_recent(db2, "--limit", bad)
            expect_error_exit(rc, out, err, f"无效 --limit {bad!r}")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n全部通过：{_passed} 项检查")


if __name__ == "__main__":
    main()
