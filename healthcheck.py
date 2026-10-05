#!/usr/bin/env python3
"""本地服务健康检查工具（仅本机 HTTP 探测 + SQLite 历史）。

用法：
    python healthcheck.py --db monitor.sqlite check --url http://127.0.0.1:8765/
    python healthcheck.py --db monitor.sqlite recent --limit 5
    python healthcheck.py --db monitor.sqlite recent --url http://127.0.0.1:8765/health?detail=1
    python healthcheck.py --db monitor.sqlite recent --status failure --limit 5
    python healthcheck.py --db monitor.sqlite recent --url http://127.0.0.1:8765/ --reason timeout
    python healthcheck.py --db monitor.sqlite recent --since 2026-10-04T00:00:02Z --limit 2
    python healthcheck.py --db monitor.sqlite recent --until 2026-10-04T00:00:02Z --limit 2
    python healthcheck.py --db monitor.sqlite recent --since 2026-10-04T00:00:01Z --until 2026-10-04T00:00:02.000000+00:00 --limit 2
    python healthcheck.py --db monitor.sqlite recent --summary --url http://127.0.0.1:8765/ --status failure --reason timeout --limit 2
    python healthcheck.py --db monitor.sqlite recent --status-summary --limit 2
    python healthcheck.py --db monitor.sqlite streak --url http://127.0.0.1:8765/health
    python healthcheck.py --db monitor.sqlite streak --url http://127.0.0.1:8765/health --threshold 3
"""

import argparse
import http.client
import json
import math
import os
import pathlib
import re
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

# 裸 --status（命令行上未给值）时 argparse 注入的哨兵；
# 区别于「完全省略该参数」的 None（None 表示不按状态筛选）
STATUS_FILTER_MISSING = object()

# --status 唯一合法的两个取值（区分大小写）
STATUS_FILTER_CHOICES = (STATUS_SUCCESS, STATUS_FAILURE)

# 裸 --reason（命令行上未给值）时 argparse 注入的哨兵；
# 区别于「完全省略该参数」的 None（None 表示不按原因筛选）
REASON_FILTER_MISSING = object()

# --reason 唯一合法的四个取值（区分大小写）；只按保存的 reason 精确匹配，
# 绝不从 status 或 http_status 推断
REASON_FILTER_CHOICES = (
    REASON_OK,
    REASON_HTTP_STATUS,
    REASON_CONNECTION_ERROR,
    REASON_TIMEOUT,
)

# 裸 --since / --until（命令行上未给值）时 argparse 注入的同一哨兵；
# 区别于「完全省略该参数」的 None（None 表示不按该方向筛选）。
# 两个时间边界共用同一套格式规则与缺值处理，故只需一个哨兵。
TIME_BOUND_MISSING = object()

# 裸 --threshold（命令行上未给值）时 argparse 注入的哨兵；
# 区别于「完全省略该参数」的 None（None 表示不做阈值判断，输出原有三字段）
THRESHOLD_MISSING = object()

# streak 阈值的唯一合法形态：纯 ASCII 十进制数字（允许前导零），
# 且整体表示的数值必须大于零
THRESHOLD_DIGITS_RE = re.compile(r"[0-9]+\Z")

# --since 与记录 checked_at 共用的严格 UTC 格式：
# YYYY-MM-DDTHH:MM:SS，秒后可带一至六位小数，仅以 Z 或 +00:00 结尾；
# 日期时间是否真实存在由 datetime 构造另行校验
UTC_TIMESTAMP_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|\+00:00)"
)

# 上一条正则去掉时区部分后的前缀形态，用于在 fullmatch 失败时
# 区分「缺时区」「非 UTC 偏移」等具体原因
UTC_TIMESTAMP_BODY_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
)

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

# 落库一条记录所依赖的全部字段；既有 checks 表缺其中任意一个时，
# 必须在发送探测请求前拒绝（不补列、不重建表）
REQUIRED_COLUMNS = (
    "id", "url", "checked_at", "elapsed_ms", "status", "http_status", "reason",
)

# 唯一的历史查询模板：{table} 在执行时替换为库中历史表的实际表名（加引号），
# 以兼容 CHECKS / Checks 等仅大小写不同的历史表名；{where} 替换为按当前
# 筛选组合生成的 WHERE 子句（无筛选时为空字符串）；{limit} 替换为
# "LIMIT ?" 或空字符串（--since/--until 任一生效时时间筛选在 Python 侧
# 完成，SQL 不截断，过滤后才限量）。三个可选筛选（url/status/reason）共
# 八种组合，统一由 build_recent_query 组装，不再为每种组合单独维护一条 SQL。
SELECT_SQL = """
SELECT id, url, checked_at, elapsed_ms, status, http_status, reason
FROM {table}
{where}
ORDER BY id DESC
{limit}
"""

# 三个可选筛选各自的 WHERE 条件片段，按 url → status → reason 的固定顺序
# 拼接（与原八条独立 SQL 中条件的书写顺序一致），条件之间一律 AND
RECENT_FILTER_CLAUSES = (
    ("url", "url = ?"),
    ("status", "status = ?"),
    ("reason", "reason = ?"),
)

# streak 的唯一查询模板：取指定目标按 id 倒序排列的 (id, status) 两列。
# 连续失败段只以 id 大小定义先后（checked_at 不参与排序），且不受 recent
# 默认五条限制，故不带 LIMIT；连续段在 Python 侧自最大 id 起逐条判定，
# 遇到首条 success 即停止。status 若出现 success/failure 之外的取值，
# 由调用方按记录 id 以退出码 2 拒绝。
STREAK_SQL = """
SELECT id, status
FROM {table}
WHERE url = ?
ORDER BY id DESC
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


def validate_status_filter(value):
    """--status 只接受区分大小写的 success / failure；None 表示不筛选。

    其他一切取值（空字符串、大小写变体、带空白，以及裸 --status 缺少值）
    均经 die 以退出码 2 拒绝。此检查先于 URL 校验与一切数据库访问，
    拒绝时不会读取数据库。
    """
    if value is None:
        return None
    if value is STATUS_FILTER_MISSING:
        die(
            "status 参数错误：--status 必须提供值 "
            "'success' 或 'failure'（区分大小写），实际缺少值"
        )
    if value in STATUS_FILTER_CHOICES:
        return value
    die(
        "status 参数错误：仅接受区分大小写的 'success' 或 'failure'"
        f"（省略时查询全部状态），实际值 {value!r}"
    )


def validate_reason_filter(value):
    """--reason 只接受区分大小写的 ok/http_status/connection_error/timeout；
    None 表示不筛选。

    只按数据库保存的 reason 字段精确匹配，不从 status 或 http_status 推断。
    其他一切取值（空字符串、大小写变体、前后带空白，以及裸 --reason 缺少值）
    均经 die 以退出码 2 拒绝。此检查在 status 与 URL 都合法之后、一切数据库
    访问之前执行，拒绝时不会读取数据库。
    """
    if value is None:
        return None
    if value is REASON_FILTER_MISSING:
        die(
            "reason 参数错误：--reason 必须提供值 "
            "'ok'、'http_status'、'connection_error' 或 'timeout'"
            "（区分大小写），实际缺少值"
        )
    if value in REASON_FILTER_CHOICES:
        return value
    die(
        "reason 参数错误：仅接受区分大小写的 'ok'、'http_status'、"
        "'connection_error' 或 'timeout'（省略时查询全部原因），"
        f"实际值 {value!r}"
    )


def parse_utc_timestamp(value):
    """严格解析 YYYY-MM-DDTHH:MM:SS[.1-6位小数](Z|+00:00)，返回 UTC aware
    datetime；格式不符或日期时间不真实存在时返回 None。

    Z 与 +00:00 等价，省略零小数与显式零小数解析为同一时刻。
    """
    if not isinstance(value, str):
        return None
    m = UTC_TIMESTAMP_RE.fullmatch(value)
    if m is None:
        return None
    year, month, day, hour, minute, second, fraction, _tz = m.groups()
    microsecond = int((fraction or "0").ljust(6, "0"))
    try:
        return datetime(
            int(year), int(month), int(day),
            int(hour), int(minute), int(second), microsecond,
            tzinfo=timezone.utc,
        )
    except ValueError:
        # 结构合法但日期时间不存在（如 2026-02-30、25:00:00）
        return None


def utc_timestamp_format_problem(value):
    """UTC 时间参数取值的中文错误原因；返回 None 表示格式与日期时间均合法。

    --since 与 --until 共用同一套规则与原因文案。
    """
    if not isinstance(value, str) or value == "":
        return "值不能为空"
    if value != value.strip():
        return "前后不允许有空白"
    if UTC_TIMESTAMP_RE.fullmatch(value) is not None:
        if parse_utc_timestamp(value) is None:
            return "日期或时间必须真实存在"
        return None
    body = UTC_TIMESTAMP_BODY_RE.match(value)
    if body is not None:
        suffix = value[body.end():]
        if suffix == "":
            return "缺少时区，必须以 Z 或 +00:00 结尾"
        if suffix not in ("Z", "+00:00") and re.fullmatch(
            r"[+-]\d{2}:\d{2}", suffix
        ):
            return f"仅接受 UTC 时区（Z 或 +00:00），不接受偏移 {suffix}"
    return (
        "格式必须为 YYYY-MM-DDTHH:MM:SS（秒后可带一至六位小数），"
        "并以 Z 或 +00:00 结尾"
    )


def validate_time_bound_filter(name, value):
    """--since 与 --until 共用的 UTC 时间边界校验。

    name 为命令行参数名（"since" 或 "until"），也是错误文案中唯一随边界
    变化的部分；None 表示省略该参数、不设该方向的边界。两个边界规则完全
    一致：只接受 YYYY-MM-DDTHH:MM:SS[.1-6位小数](Z|+00:00) 且日期时间
    真实存在；缺值（裸参数）、空值、前后空白、缺时区、非 UTC 偏移、非法
    日期时间均经 die 以退出码 2 拒绝（stderr 指出对应参数名及原因，
    stdout 为空）。该校验在 status、URL、reason 都合法之后、一切数据库
    访问之前执行（until 在 since 之后），拒绝时不会读取数据库。返回值为
    UTC aware datetime，用于与记录的 checked_at 按时刻比较。
    """
    if value is None:
        return None
    if value is TIME_BOUND_MISSING:
        die(
            f"{name} 参数错误：--{name} 必须提供值，格式为 "
            "YYYY-MM-DDTHH:MM:SS（秒后可带一至六位小数）"
            "并以 Z 或 +00:00 结尾，实际缺少值"
        )
    problem = utc_timestamp_format_problem(value)
    if problem is not None:
        die(f"{name} 参数错误：--{name} {problem}，实际值 {value!r}")
    return parse_utc_timestamp(value)


def disable_int_digit_limit_for_threshold():
    """解除 Python 3.11+ 的十进制整数字符串位数限制（默认 4300 位）。

    --threshold 接受任意长度的纯 ASCII 数字文本，输出与 JSON 序列化均按
    整数表示，故在 streak 路径上解除该限制；只由阈值校验调用，check/recent
    不经过此处，其既有整数解析行为保持不变。
    """
    set_limit = getattr(sys, "set_int_max_str_digits", None)
    if set_limit is not None:
        try:
            set_limit(0)
        except ValueError:
            pass


def validate_threshold(value):
    """streak 的 --threshold：None 表示省略（不做阈值判断）。

    提供时只接受纯 ASCII 十进制数字组成且数值大于零的文本（允许前导零）；
    返回正整数（前导零不保留），输出与比较均按整数进行。缺值（裸参数）、
    空字符串、零、负数、小数、空白、正号或任何非 ASCII 数字字符均经 die
    以退出码 2 拒绝（stdout 为空，stderr 指出 threshold 及原因）。该校验
    在 URL 校验之后、一切数据库访问之前执行，拒绝时不会读取数据库。
    """
    if value is None:
        return None
    if value is THRESHOLD_MISSING:
        die(
            "threshold 参数错误：--threshold 必须提供值，"
            "为纯数字组成的正整数（允许前导零），实际缺少值"
        )
    text = value if isinstance(value, str) else str(value)
    if not THRESHOLD_DIGITS_RE.fullmatch(text):
        if text == "":
            reason = "值不能为空"
        elif text != text.strip():
            reason = "前后不允许有空白"
        else:
            reason = (
                "必须是纯 ASCII 十进制数字组成的正整数，"
                "不接受符号、小数点、空白或其他字符"
            )
        die(
            "threshold 参数错误：--threshold "
            f"{reason}，实际值 {value!r}"
        )
    disable_int_digit_limit_for_threshold()
    try:
        threshold = int(text)
    except ValueError:
        # 受限解释器不允许解除位数限制、且数字串超长时走到这里
        die(
            "threshold 参数错误：--threshold 数值超出可处理范围，"
            f"实际值 {value!r}"
        )
    if threshold <= 0:
        die(
            "threshold 参数错误：--threshold 必须大于零，"
            f"实际值 {value!r}"
        )
    return threshold


def filter_rows_by_since(rows, since, limit, until=None):
    """时间窗口生效时的后处理：逐条校验 checked_at 的 UTC 格式，按 --since /
    --until 的时刻边界保留记录，最后沿用 id 倒序取前 limit 条。

    rows 已由 SQL 按其余筛选条件过滤并按 id 倒序排列。since 为 None 时不设
    下界（--until 单独使用），until 为 None 时不设上界；二者同用时两个端点
    都包含（>= since 且 <= until）。符合其余筛选的记录只要 checked_at 不
    符合严格 UTC 格式，即经 die 以退出码 2 拒绝并指出记录 id（不修改任何
    数据）；校验针对全部符合其余筛选的记录，在时间比较与 limit 截取之前
    完成——坏记录即使落在窗口之外或 limit 之外也照样拒绝。比较按时刻进行：
    Z 与 +00:00、省略零小数的写法视为同一时刻；输出仍使用记录保存的原始
    字符串。
    """
    kept = []
    for row in rows:
        checked_at = parse_utc_timestamp(row[2])
        if checked_at is None:
            die(
                f"记录 id={row[0]} 的 checked_at 不符合 UTC 时间格式"
                "（YYYY-MM-DDTHH:MM:SS，秒后可带一至六位小数，"
                f"以 Z 或 +00:00 结尾）: {row[2]!r}"
            )
        if since is not None and checked_at < since:
            continue
        if until is not None and checked_at > until:
            continue
        kept.append(row)
    return kept[:limit]


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
    """打开（必要时创建）数据库并确保表存在；任何失败均以退出码 2 结束。

    若 checks 表已存在但缺少落库所需字段，同样以退出码 2 结束：此检查发生
    在任何网络探测之前，且不补列、不重建表，原有表结构与数据保持不变。
    """
    parent = os.path.dirname(os.path.abspath(db_path))
    if not os.path.isdir(parent):
        die(f"数据库父目录不存在: {parent}")
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(CREATE_TABLE_SQL)
        conn.commit()
        ensure_checks_columns(conn)
    except sqlite3.Error as exc:
        die(f"无法打开或初始化数据库 {db_path!r}: {exc}")
    return conn


def ensure_checks_columns(conn):
    """核对既有 checks 表具备全部所需字段，缺任意一个即以退出码 2 拒绝。

    字段名比较沿用 SQLite 的大小写不敏感语义；列顺序不同或存在额外列均
    不构成问题。只检查字段是否存在，不涉及类型、索引与约束。
    """
    actual = {
        row[1].lower()
        for row in conn.execute("PRAGMA table_info(checks)")
    }
    missing = [name for name in REQUIRED_COLUMNS if name.lower() not in actual]
    if missing:
        die("checks 表缺少字段: " + ", ".join(missing))


def quote_identifier(name):
    """把表名等 SQLite 标识符加双引号引用，内部双引号按 SQL 规则翻倍。"""
    return '"' + name.replace('"', '""') + '"'


def open_history_db_readonly(db_path):
    """以严格只读方式打开历史数据库，供 recent / streak 查询共用。

    路径（含父目录）尚不存在时返回 None，表示历史为空：不创建文件、目录，
    也不产生 -wal/-journal 旁路文件。路径指向目录、文件不是有效 SQLite
    数据库或无读权限等打开/读取失败，均以退出码 2 结束（stdout 为空，
    stderr 说明原因）。可读但不可写的文件可正常打开查询。
    """
    if not os.path.exists(db_path):
        return None
    if os.path.isdir(db_path):
        die(f"读取数据库 {db_path!r} 失败: 路径是一个目录，不是 SQLite 数据库文件")
    # 以只读模式打开：可读但不可写的文件也能查询，
    # 且任何情况下都不会创建或修改文件（含 -wal/-journal）
    uri = pathlib.Path(os.path.abspath(db_path)).as_uri() + "?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        die(f"读取数据库 {db_path!r} 失败: {exc}")


def resolve_checks_table(conn):
    """在只读连接上按大小写不敏感查找历史表。

    返回库中保存的实际表名（CHECKS / Checks / checks 视为同一历史表，
    这类异写表至多存在一个）；空库或仅有其他表时返回 None。
    """
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    ]
    return next((name for name in tables if name.lower() == "checks"), None)


def encode_request_target(target):
    """把请求目标中的非 ASCII 字符按 UTF-8 字节转成大写十六进制百分号编码。

    只改动非 ASCII 字符：已存在的百分号编码（连同字母大小写）、查询中的
    '+'、路径分隔符、参数顺序及其余 ASCII 内容全部原样保留，混合中文与
    编码内容不会重复编码（%23 不会变成 %2523）。仅用于发送请求时的请求
    目标，不影响落库与输出所使用的中文原始 URL。
    """
    parts = []
    for ch in target:
        if ord(ch) > 127:
            parts.append("".join(f"%{b:02X}" for b in ch.encode("utf-8")))
        else:
            parts.append(ch)
    return "".join(parts)


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
        # 发送前把未编码的非 ASCII（如中文路径/查询值）百分号编码；
        # 落库与输出仍使用 args.url 的中文原文
        status, http_status, reason, elapsed_ms = probe_once(
            port, encode_request_target(target), args.timeout
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


def build_recent_query(quoted_table, filters, sql_limit=True):
    """按当前筛选组合组装历史查询的 SQL 与绑定参数。

    filters 为 ((列名, 值), ...) 形式的可选筛选（值为 None 的项表示不筛选）。
    所有给定条件取交集（AND）：url 用原始字符串精确匹配，status/reason 按
    保存值等值匹配；reason 只看记录自身的 reason 字段，绝不从
    status/http_status 推断。无论哪种组合，都是先按全部条件筛选，
    再按 id 倒序排列。sql_limit 为 True 时附加 LIMIT ? 占位符
    （由调用方绑定 limit）；为 False 时不加 LIMIT——--since/--until 任一
    时间边界生效时时间条件在 Python 侧按时刻比较，须先取出符合其余筛选的
    全部记录，过滤后才截取 limit 条。
    """
    clauses = {
        name: clause for name, clause in RECENT_FILTER_CLAUSES
    }
    conditions = []
    params = []
    for name, value in filters:
        if value is not None:
            conditions.append(clauses[name])
            params.append(value)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    limit_clause = "LIMIT ?" if sql_limit else ""
    sql = SELECT_SQL.format(table=quoted_table, where=where, limit=limit_clause)
    return sql, params


def row_to_record(row):
    """把一行查询结果映射为输出的七字段记录对象（字段名与顺序固定）。

    url、checked_at 等字符串原样取自数据库，不做任何归一化。
    """
    return {
        "id": row[0],
        "url": row[1],
        "checked_at": row[2],
        "elapsed_ms": row[3],
        "status": row[4],
        "http_status": row[5],
        "reason": row[6],
    }


def build_elapsed_summary(rows):
    """耗时摘要：统计给定记录集的 elapsed_ms，失败与零耗时记录均计入，
    平均值不取整（浮点除法原样输出）；空集 count 为 0、三个耗时值为 None
    （JSON null）。
    """
    elapsed_values = [row[3] for row in rows]
    if not elapsed_values:
        return {
            "count": 0,
            "min_elapsed_ms": None,
            "max_elapsed_ms": None,
            "avg_elapsed_ms": None,
        }
    count = len(elapsed_values)
    return {
        "count": count,
        "min_elapsed_ms": min(elapsed_values),
        "max_elapsed_ms": max(elapsed_values),
        "avg_elapsed_ms": sum(elapsed_values) / count,
    }


def build_status_summary(rows):
    """状态摘要：只按记录保存的 status 字段分类计数，绝不从 reason 或
    http_status 推断；空集三个计数均为 0。
    """
    return {
        "count": len(rows),
        "success_count": sum(
            1 for row in rows if row[4] == STATUS_SUCCESS
        ),
        "failure_count": sum(
            1 for row in rows if row[4] == STATUS_FAILURE
        ),
    }


def render_recent_result(rows, summary, status_summary):
    """recent 三种输出形态的唯一呈现入口。

    rows 为筛选、时间窗口与 limit 处理后的同一批记录（已按 id 倒序）；
    缺库、无历史表、空表、筛选无匹配等空结果情形同样经此入口（传空
    列表），不再单独维护空结果的输出分支。普通模式输出七字段记录数组
    （空集为 []），summary 为真输出耗时摘要，status_summary 为真输出
    状态摘要；均为紧凑单行 JSON 加一个换行。summary 与 status_summary
    的互斥在参数校验阶段已保证，此处不重复检查。
    """
    if summary:
        payload = build_elapsed_summary(rows)
    elif status_summary:
        payload = build_status_summary(rows)
    else:
        payload = [row_to_record(row) for row in rows]
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def command_recent(args):
    db_path = args.db

    # 两个摘要选项互斥：同用时在任何参数校验与数据库访问之前
    # 即以退出码 2 拒绝（stdout 为空，stderr 指出互斥）
    if args.summary and args.status_summary:
        die(
            "参数错误：--summary 与 --status-summary 互斥，"
            "一次查询只能选用其中一种摘要"
        )

    # 先校验 --status（区分大小写，仅 success/failure）：
    # 非法值（含空字符串、裸 --status 缺值）在读取数据库前即以退出码 2 拒绝。
    # status 合法后，其余参数与数据库错误的报告顺序与原先一致（先 URL 后路径）。
    status_filter = validate_status_filter(args.status)

    # 再校验筛选 URL（沿用 check 的本机 URL 规则）：
    # 非法值即使数据库路径不存在或为目录，也优先报 URL 错误。
    # 仅做校验，匹配时仍使用原始字符串，不做任何规范化。
    if args.url is not None:
        validate_target_url(args.url)

    # status、URL 都合法后再校验 --reason（区分大小写，仅四个固定值）：
    # 非法值（含空字符串、裸 --reason 缺值、前后空白）同样在读取数据库前
    # 即以退出码 2 拒绝。reason 只按保存值精确匹配，绝不从状态码推断。
    reason_filter = validate_reason_filter(args.reason)

    # status、URL、reason 都合法后再校验两个 UTC 时间边界（共用同一规则、
    # 缺值处理与错误封装，仅参数名不同）：缺值、空值、前后空白、缺时区、
    # 非 UTC 偏移、非法日期时间同样在读取数据库前即以退出码 2 拒绝。
    # since 先于 until 校验：两边界同用时两边界各自合法后再比较先后，
    # 两边界同时非法时先报告 --since；省略任一边界时不设该方向边界。
    since_filter = validate_time_bound_filter("since", args.since)
    until_filter = validate_time_bound_filter("until", args.until)
    if (
        since_filter is not None
        and until_filter is not None
        and since_filter > until_filter
    ):
        die(
            "时间范围参数错误：--since 给出的起始时刻 "
            f"{args.since!r} 晚于 --until 给出的结束时刻 {args.until!r}，"
            "起点不得晚于终点（二者相等时为只含同一时刻的合法窗口）"
        )

    # 任一时间边界生效时，时间比较都在 Python 侧完成
    time_filter_active = since_filter is not None or until_filter is not None

    # 严格只读：文件尚不存在（含父目录不存在）时历史为空，
    # 不创建文件、不创建目录、不发任何网络请求；空结果与查询后的
    # 空结果共用同一呈现入口
    conn = open_history_db_readonly(db_path)
    if conn is None:
        render_recent_result([], args.summary, args.status_summary)
        return 0

    try:
        try:
            # 只查询，绝不执行 CREATE TABLE / INSERT 等写操作
            # 历史表名按大小写不敏感识别：CHECKS、Checks 等与 checks 是同一
            # 历史表（SQLite 标识符本身大小写不敏感，这类异写表至多存在一个），
            # 查询时使用库中保存的实际表名
            checks_table = resolve_checks_table(conn)
            if checks_table is None:
                # 空数据库或仅有其他表：历史为空，原有表与数据保持不变
                rows = []
            else:
                # 表存在但缺任一所需字段：以退出码 2 说明缺列，
                # 不再误判为空历史
                ensure_checks_columns(conn)
                quoted = quote_identifier(checks_table)
                # 三个可选筛选共八种组合，统一由 build_recent_query 组装：
                # 条件之间一律 AND，先按全部条件筛选，再按 id 倒序排列。
                # 任一时间边界生效时 SQL 不加 LIMIT：时间条件在 Python 侧
                # 按时刻比较（Z 与 +00:00、省略零小数视为同一时刻），须先
                # 取出符合其余筛选的全部记录，校验并过滤后才截取 limit 条
                sql, params = build_recent_query(
                    quoted,
                    (
                        ("url", args.url),
                        ("status", status_filter),
                        ("reason", reason_filter),
                    ),
                    sql_limit=not time_filter_active,
                )
                if not time_filter_active:
                    rows = conn.execute(sql, (*params, args.limit)).fetchall()
                else:
                    rows = conn.execute(sql, params).fetchall()
                    rows = filter_rows_by_since(
                        rows, since_filter, args.limit, until_filter
                    )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        # 文件不是有效 SQLite 数据库、读取失败等
        # （checks 表缺字段已在查询前由 ensure_checks_columns 单独报告）
        die(f"读取数据库 {db_path!r} 失败: {exc}")

    # 三种形态共用同一呈现入口：rows 即筛选、时间窗口与 limit 处理后的
    # 同一批记录；--summary 输出其耗时摘要（失败与零耗时计入，均值不取整），
    # --status-summary 输出其状态计数（只依据保存的 status 字段），
    # 普通模式输出七字段记录数组；无记录时分别为 count 0 三 null、
    # 三个计数为 0、[]
    render_recent_result(rows, args.summary, args.status_summary)
    return 0


def count_consecutive_failures(rows):
    """按 id 倒序的 (id, status) 行序列计算连续失败次数。

    rows 已按 id 从大到小排列且同属一个目标；自最大 id 起累计 failure，
    遇到首条 success 即停止，最新为 success 时为 0、全部 failure 时统计
    全部。只按保存的 status 判断，http_status、reason 不参与。若遇到
    success/failure 之外的 status 取值，经 die 以退出码 2 拒绝并指出记录
    id（不修改任何数据）。
    """
    count = 0
    for record_id, status in rows:
        if status == STATUS_FAILURE:
            count += 1
        elif status == STATUS_SUCCESS:
            break
        else:
            die(
                f"记录 id={record_id} 的 status 不是受支持的取值 "
                f"（仅 'success' 或 'failure'）: {status!r}"
            )
    return count


def render_streak_result(url, latest_id, consecutive_failures, threshold=None):
    """streak 唯一输出形态：紧凑单行 JSON，字段名与顺序固定。

    url 保留命令行输入原文，不做任何规范化。省略 --threshold 时只输出
    url/latest_id/consecutive_failures 三字段（与既有行为一致）；提供阈值时
    追加整数 threshold 与布尔 threshold_reached
    （consecutive_failures >= threshold）。
    """
    payload = {
        "url": url,
        "latest_id": latest_id,
        "consecutive_failures": consecutive_failures,
    }
    if threshold is not None:
        payload["threshold"] = threshold
        payload["threshold_reached"] = consecutive_failures >= threshold
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def command_streak(args):
    # 先校验目标 URL（沿用 check 的本机地址规则），非法地址在访问数据库前
    # 即以退出码 2 拒绝。校验通过即可，匹配仍使用原始字符串，不做规范化。
    validate_target_url(args.url)

    # 再校验 --threshold（纯 ASCII 数字且大于零，允许前导零）：
    # 缺值、空值、零、负数、小数、空白或其他字符同样在访问数据库前以
    # 退出码 2 拒绝（stdout 为空，stderr 指出 threshold 及原因）。
    # None 表示省略：保留原有三字段输出，不做阈值判断。
    threshold = validate_threshold(args.threshold)

    # streak 严格只读：文件尚不存在（含父目录不存在）、空库、无历史表或
    # 该目标无记录时返回 latest_id 为 null、consecutive_failures 为 0
    # （提供阈值时 threshold_reached 为 false），不创建文件、目录或表，
    # 不发任何网络请求
    conn = open_history_db_readonly(args.db)
    if conn is None:
        render_streak_result(args.url, None, 0, threshold)
        return 0

    try:
        try:
            # 历史表名按大小写不敏感识别（与 recent 相同的兼容范围），
            # 查询时使用库中保存的实际表名
            checks_table = resolve_checks_table(conn)
            if checks_table is None:
                # 空数据库或仅有其他表：该目标无记录，原有表与数据不变
                latest_id = None
                consecutive_failures = 0
            else:
                # 表存在但缺任一既有必需字段：以退出码 2 说明缺列
                ensure_checks_columns(conn)
                quoted = quote_identifier(checks_table)
                # 只取该目标的 (id, status)，按 id 倒序、不加 LIMIT：
                # 连续段不受 recent 默认五条限制，checked_at 不参与排序
                rows = conn.execute(
                    STREAK_SQL.format(table=quoted), (args.url,)
                ).fetchall()
                if not rows:
                    latest_id = None
                    consecutive_failures = 0
                else:
                    latest_id = rows[0][0]
                    # 连续段内出现非法 status 仍以退出码 2 拒绝并指出 id，
                    # 即使 failure 次数此前已达到阈值；比首条 success 更旧的
                    # 记录不会被遍历到，不影响结果
                    consecutive_failures = count_consecutive_failures(rows)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        # 文件不是有效 SQLite 数据库、读取失败等
        # （checks 表缺字段已在查询前由 ensure_checks_columns 单独报告）
        die(f"读取数据库 {args.db!r} 失败: {exc}")

    render_streak_result(
        args.url, latest_id, consecutive_failures, threshold
    )
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
        "--status",
        nargs="?",
        const=STATUS_FILTER_MISSING,
        default=None,
        metavar="{success,failure}",
        help="可选：仅返回该状态的记录，只接受区分大小写的 success 或 failure；"
             "省略时查询全部状态。与 --url、--reason 同用时各条件都要满足。"
             "裸 --status（缺少值）或其他值均为参数错误",
    )
    p_recent.add_argument(
        "--reason",
        nargs="?",
        const=REASON_FILTER_MISSING,
        default=None,
        metavar="{ok,http_status,connection_error,timeout}",
        help="可选：仅返回保存的 reason 与该值精确相等的记录，只接受区分大小写"
             "的 ok、http_status、connection_error、timeout；不从状态码推断，"
             "省略时查询全部原因。与 --url、--status 同用时取交集。"
             "裸 --reason（缺少值）或其他值均为参数错误",
    )
    p_recent.add_argument(
        "--since",
        nargs="?",
        const=TIME_BOUND_MISSING,
        default=None,
        metavar="YYYY-MM-DDTHH:MM:SS[.ffffff](Z|+00:00)",
        help="可选：仅返回 checked_at 不早于该 UTC 时刻的记录，格式为 "
             "YYYY-MM-DDTHH:MM:SS（秒后可带一至六位小数）并以 Z 或 +00:00 "
             "结尾，日期时间须真实存在；Z 与 +00:00、省略零小数按同一时刻"
             "比较。与 --url、--status、--reason 同用时取交集，省略时不增加"
             "时间条件。裸 --since（缺少值）或格式非法均为参数错误",
    )
    p_recent.add_argument(
        "--until",
        nargs="?",
        const=TIME_BOUND_MISSING,
        default=None,
        metavar="YYYY-MM-DDTHH:MM:SS[.ffffff](Z|+00:00)",
        help="可选：仅返回 checked_at 不晚于该 UTC 时刻的记录（<=，相等命中），"
             "格式与日期真实性规则同 --since；单独使用时不设下界，与 --since "
             "同用时包含两个端点（起点不得晚于终点，相等合法）。与 --url、"
             "--status、--reason 同用时取交集，省略时不增加时间条件。"
             "裸 --until（缺少值）或格式非法均为参数错误",
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
        help="可选：不返回记录列表，改为输出这批记录的耗时摘要"
             "（count/min/max/avg，单行 JSON 对象）",
    )
    p_recent.add_argument(
        "--status-summary",
        action="store_true",
        help="可选：不返回记录列表，改为输出这批记录的状态计数摘要"
             "（count/success_count/failure_count，单行 JSON 对象，"
             "只依据保存的 status 字段分类）。与 --summary 互斥",
    )
    p_recent.set_defaults(handler=command_recent)

    p_streak = subparsers.add_parser(
        "streak",
        help="查询指定目标自最大 id 起的连续失败次数（只读，单条 JSON）",
    )
    p_streak.add_argument(
        "--url",
        required=True,
        help="必填：目标 URL，按数据库保存的原始 url 字符串精确匹配"
             "（规则同 check：仅 http://127.0.0.1:端口/...）；"
             "连续段只按 id 倒序的保存 status 判定，不支持 recent 的"
             "筛选与限量选项",
    )
    p_streak.add_argument(
        "--threshold",
        nargs="?",
        const=THRESHOLD_MISSING,
        default=None,
        metavar="N",
        help="可选：连续失败次数阈值，只接受纯 ASCII 十进制数字组成且大于"
             "零的文本（允许前导零，输出按整数表示）。省略时输出原有"
             " url/latest_id/consecutive_failures 三字段；提供时追加整数 "
             "threshold 与布尔 threshold_reached（连续失败次数大于或等于"
             "阈值为 true）。裸 --threshold（缺少值）、空值、零、负数、"
             "小数、空白或其他字符均在访问数据库前以退出码 2 拒绝",
    )
    p_streak.set_defaults(handler=command_streak)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
