#!/usr/bin/env python3
"""本地服务健康检查工具（仅本机 HTTP 探测 + SQLite 历史）。

用法：
    python healthcheck.py --db monitor.sqlite check --url http://127.0.0.1:8765/
    python healthcheck.py --db monitor.sqlite recent --limit 5
    python healthcheck.py --db monitor.sqlite recent --url http://127.0.0.1:8765/health?detail=1
    python healthcheck.py --db monitor.sqlite recent --summary --limit 2
"""

import argparse
import http.client
import json
import math
import os
import pathlib
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

SELECT_BY_URL_SQL = """
SELECT id, url, checked_at, elapsed_ms, status, http_status, reason
FROM checks
WHERE url = ?
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

    try:
        parsed = urlparse(raw_url)
    except ValueError:
        # 结构非法（如未配对的方括号 "http://[127.0.0.1:8765/"）
        die(f"非法 URL（结构无法解析）: {raw_url!r}")
        return  # 仅为类型检查器所知，实际不可达

    try:
        hostname = parsed.hostname
    except ValueError:
        # 括号内不是合法地址（如 http://[]/、http://[zzz]/）
        die(f"非法 URL（主机地址无法解析）: {raw_url!r}")
        return  # 仅为类型检查器所知，实际不可达

    if parsed.scheme != "http":
        die(f"仅接受 http 协议: {raw_url!r}")
    if hostname != ALLOWED_HOST:
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
    # 以原始字符串中的 '#' 分隔符判定片段：空片段（结尾或查询参数后的裸
    # '#'）经 urlparse 得到 fragment == ""，无法靠 parsed.fragment 识别，
    # 故只要出现原始 '#' 即拒绝，不论片段内容、也不论位于路径后还是查询后。
    # '%23' 是普通百分号编码内容，不含原始 '#'，仍可合法出现在路径或查询中。
    if "#" in raw_url:
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


def print_summary(rows):
    """为 recent 本来会返回的记录（同样的筛选、排序与 limit）输出耗时摘要。

    成功与失败记录均参与，零耗时也计入；不补查当前服务状态。
    无样本时三个耗时字段为 null。
    """
    elapsed_values = [row[3] for row in rows]
    count = len(elapsed_values)
    if count == 0:
        summary = {
            "count": 0,
            "min_elapsed_ms": None,
            "max_elapsed_ms": None,
            "avg_elapsed_ms": None,
        }
    else:
        summary = {
            "count": count,
            "min_elapsed_ms": min(elapsed_values),
            "max_elapsed_ms": max(elapsed_values),
            # 总和除以样本数，以 JSON 数字原样输出，不取整
            "avg_elapsed_ms": sum(elapsed_values) / count,
        }
    print(json.dumps(summary, separators=(",", ":"), ensure_ascii=False))


def command_recent(args):
    db_path = args.db

    # 先校验筛选 URL（沿用 check 的本机 URL 规则）：
    # 非法值即使数据库路径不存在或为目录，也优先报 URL 错误。
    # 仅做校验，匹配时仍使用原始字符串，不做任何规范化。
    if args.url is not None:
        validate_target_url(args.url)

    # recent 严格只读：文件尚不存在（含父目录不存在）时历史为空，
    # 不创建文件、不创建目录、不发任何网络请求
    if not os.path.exists(db_path):
        if args.summary:
            print_summary([])
        else:
            print("[]")
        return 0

    # 路径指向目录不是有效的数据库文件
    if os.path.isdir(db_path):
        die(f"读取数据库 {db_path!r} 失败: 路径是一个目录，不是 SQLite 数据库文件")

    # 以只读模式打开：可读但不可写的文件也能查询，
    # 且任何情况下都不会创建或修改文件（含 -wal/-journal）
    uri = pathlib.Path(os.path.abspath(db_path)).as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        die(f"读取数据库 {db_path!r} 失败: {exc}")

    try:
        try:
            # 只查询，绝不执行 CREATE TABLE / INSERT 等写操作
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "checks" not in tables:
                # 空数据库或仅有其他表：历史为空，原有表与数据保持不变
                rows = []
            elif args.url is not None:
                # 按数据库中保存的原始 url 字符串精确匹配：
                # 不合并路径或查询参数不同的地址，也不规范化 URL
                rows = conn.execute(
                    SELECT_BY_URL_SQL, (args.url, args.limit)
                ).fetchall()
            else:
                rows = conn.execute(SELECT_SQL, (args.limit,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        # 文件不是有效 SQLite 数据库、checks 表缺少所需字段等
        die(f"读取数据库 {db_path!r} 失败: {exc}")

    if args.summary:
        print_summary(rows)
        return 0

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
        "--url",
        default=None,
        help="可选：仅返回该目标的记录，按数据库保存的原始 url 字符串精确匹配"
             "（规则同 check：仅 http://127.0.0.1:端口/...）",
    )
    p_recent.add_argument(
        "--limit",
        type=positive_limit,
        default=DEFAULT_LIMIT,
        help=f"返回条数，正整数（默认 {DEFAULT_LIMIT}）",
    )
    p_recent.add_argument(
        "--summary",
        action="store_true",
        help="可选：不返回记录数组，只输出一行耗时摘要 JSON 对象"
             "（count/min_elapsed_ms/max_elapsed_ms/avg_elapsed_ms），"
             "统计范围与 recent 本来会返回的记录一致",
    )
    p_recent.set_defaults(handler=command_recent)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
