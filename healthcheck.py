#!/usr/bin/env python3
"""本地服务健康检查工具（仅本机 HTTP 探测 + SQLite 历史）。

用法：
    python healthcheck.py --db monitor.sqlite check --url http://127.0.0.1:8765/
    python healthcheck.py --db monitor.sqlite recent --limit 5
"""

import argparse
import http.client
import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

ALLOWED_HOST = "127.0.0.1"
DEFAULT_TIMEOUT = 1.0
DEFAULT_LIMIT = 5

REASON_OK = "ok"
REASON_HTTP_STATUS = "http_status"
REASON_CONNECTION_ERROR = "connection_error"
REASON_TIMEOUT = "timeout"

STATUS_SUCCESS = "success"
STATUS_FAILURE = "failure"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS checks (
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

INSERT_SQL = """
INSERT INTO checks (url, checked_at, elapsed_ms, status, http_status, reason)
VALUES (?, ?, ?, ?, ?, ?)
"""

SELECT_SQL = """
SELECT id, url, checked_at, elapsed_ms, status, http_status, reason
FROM checks
ORDER BY id DESC
LIMIT ?
"""


def die(message):
    """参数或数据库错误：写 stderr，以退出码 2 结束（stdout 保持为空）。"""
    print(f"healthcheck: error: {message}", file=sys.stderr)
    raise SystemExit(2)


def positive_timeout(value):
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"超时值必须是数字: {value!r}")
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError(f"超时必须是有限正数秒: {value!r}")
    return timeout


def positive_limit(value):
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"limit 必须是正整数: {value!r}")
    # 拒绝 "1.5"、"+1 " 之外的空白等 int() 能容忍但语义不符的写法
    if str(limit) != str(value).strip() or limit <= 0:
        raise argparse.ArgumentTypeError(f"limit 必须是正整数: {value!r}")
    return limit


def validate_target_url(raw_url):
    """只接受 http://127.0.0.1:<显式端口>[/path][?query]，拒绝其余一切。"""
    if not isinstance(raw_url, str) or not raw_url:
        die("URL 不能为空")
    # 拒绝空白与控制字符
    if any(ch.isspace() or ord(ch) < 32 for ch in raw_url):
        die(f"非法 URL（含空白或控制字符）: {raw_url!r}")

    parsed = urlparse(raw_url)

    if parsed.scheme != "http":
        die(f"仅接受 http 协议: {raw_url!r}")
    if parsed.hostname != ALLOWED_HOST:
        die(f"仅接受主机 {ALLOWED_HOST}: {raw_url!r}")
    try:
        port = parsed.port
    except ValueError:
        die(f"端口非法或超出 1-65535: {raw_url!r}")
        return  # 仅为类型检查器所知，实际不可达
    if port is None:
        die(f"必须显式指定端口: {raw_url!r}")
    if not (1 <= port <= 65535):
        die(f"端口必须在 1-65535 之间: {raw_url!r}")
    if parsed.username is not None or parsed.password is not None:
        die(f"不接受用户信息(userinfo): {raw_url!r}")
    if parsed.fragment != "":
        die(f"不接受片段(fragment): {raw_url!r}")

    path = parsed.path or "/"
    target = path
    if parsed.params:
        target += ";" + parsed.params
    if parsed.query != "":
        target += "?" + parsed.query
    return port, target


def open_database(db_path):
    """打开（必要时创建）数据库并确保表存在；任何失败均以退出码 2 结束。"""
    parent = os.path.dirname(os.path.abspath(db_path))
    if not os.path.isdir(parent):
        die(f"数据库父目录不存在: {parent}")
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(CREATE_TABLE_SQL)
        conn.commit()
    except sqlite3.Error as exc:
        die(f"无法打开或初始化数据库 {db_path!r}: {exc}")
    return conn


def probe_once(port, target, timeout):
    """发送且仅发送一次 GET，不跟随重定向。

    返回 (status, http_status, reason, elapsed_ms)。
    """
    conn = http.client.HTTPConnection(ALLOWED_HOST, port, timeout=timeout)
    start = time.monotonic()
    http_status = None
    try:
        conn.request("GET", target)
        resp = conn.getresponse()
        http_status = resp.status
        elapsed_ms = max(0, round((time.monotonic() - start) * 1000))
        if 200 <= http_status < 300:
            return STATUS_SUCCESS, http_status, REASON_OK, elapsed_ms
        return STATUS_FAILURE, http_status, REASON_HTTP_STATUS, elapsed_ms
    except TimeoutError:
        elapsed_ms = max(0, round((time.monotonic() - start) * 1000))
        return STATUS_FAILURE, None, REASON_TIMEOUT, elapsed_ms
    except (OSError, http.client.HTTPException):
        # 连接被拒、重置、DNS（此处无）、响应畸形/对端直接断开等
        elapsed_ms = max(0, round((time.monotonic() - start) * 1000))
        return STATUS_FAILURE, None, REASON_CONNECTION_ERROR, elapsed_ms
    finally:
        conn.close()


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def command_check(args):
    port, target = validate_target_url(args.url)

    # 探测前先确保数据库可用，不可用则不发请求
    conn = open_database(args.db)
    try:
        status, http_status, reason, elapsed_ms = probe_once(
            port, target, args.timeout
        )
        checked_at = utc_now_iso()
        record = {
            "id": None,
            "url": args.url,
            "checked_at": checked_at,
            "elapsed_ms": elapsed_ms,
            "status": status,
            "http_status": http_status,
            "reason": reason,
        }
        try:
            cur = conn.execute(
                INSERT_SQL,
                (
                    record["url"],
                    record["checked_at"],
                    record["elapsed_ms"],
                    record["status"],
                    record["http_status"],
                    record["reason"],
                ),
            )
            conn.commit()
            record["id"] = cur.lastrowid
        except sqlite3.Error as exc:
            die(f"写入检查记录失败: {exc}")
    finally:
        conn.close()

    # 仅在持久化成功后向 stdout 输出
    print(json.dumps(record, separators=(",", ":"), ensure_ascii=False))
    return 0 if status == STATUS_SUCCESS else 1


def command_recent(args):
    db_path = args.db
    # recent 只读历史：文件尚不存在时历史为空，不创建文件
    if not os.path.exists(db_path):
        print("[]")
        return 0

    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(CREATE_TABLE_SQL)
            rows = conn.execute(SELECT_SQL, (args.limit,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        die(f"读取数据库 {db_path!r} 失败: {exc}")

    records = [
        {
            "id": row[0],
            "url": row[1],
            "checked_at": row[2],
            "elapsed_ms": row[3],
            "status": row[4],
            "http_status": row[5],
            "reason": row[6],
        }
        for row in rows
    ]
    print(json.dumps(records, separators=(",", ":"), ensure_ascii=False))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="healthcheck.py",
        description="本机 HTTP 健康检查与 SQLite 历史查询",
    )
    parser.add_argument("--db", required=True, help="SQLite 数据库文件路径")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_check = subparsers.add_parser("check", help="对登记的 URL 执行一次检查")
    p_check.add_argument("--url", required=True, help="检查目标（仅 http://127.0.0.1:端口/...）")
    p_check.add_argument(
        "--timeout",
        type=positive_timeout,
        default=DEFAULT_TIMEOUT,
        help=f"超时秒数，有限正数（默认 {DEFAULT_TIMEOUT}）",
    )
    p_check.set_defaults(handler=command_check)

    p_recent = subparsers.add_parser("recent", help="按 id 倒序查询最近检查记录")
    p_recent.add_argument(
        "--limit",
        type=positive_limit,
        default=DEFAULT_LIMIT,
        help=f"返回条数，正整数（默认 {DEFAULT_LIMIT}）",
    )
    p_recent.set_defaults(handler=command_recent)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
