#!/usr/bin/env python3
"""Minimal HTTP health check with SQLite history (Python 3 standard library only).

Usage:
    python healthcheck.py --db monitor.sqlite check --url http://127.0.0.1:8765/
    python healthcheck.py --db monitor.sqlite recent --limit 5
"""

import argparse
import http.client
import json
import math
import socket
import sqlite3
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    http_status INTEGER,
    reason TEXT NOT NULL
)
"""

COLUMNS = ("id", "url", "timestamp", "duration_ms", "status", "http_status", "reason")


def fail(message):
    """Print an error to stderr and exit 2 (usage / database errors)."""
    print(f"error: {message}", file=sys.stderr)
    sys.exit(2)


def positive_float(value):
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid number: {value!r}")
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return result


def positive_int(value):
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid integer: {value!r}")
    if result <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def validate_url(raw):
    """Accept only http://127.0.0.1:<port> with optional path/query.

    Returns (port, target) where target is the request path (with query).
    Exits 2 on any violation.
    """
    try:
        parsed = urlparse(raw)
    except ValueError:
        fail(f"invalid URL: {raw!r}")
    if parsed.scheme != "http":
        fail(f"URL must use the http scheme: {raw!r}")
    if parsed.username is not None or parsed.password is not None:
        fail(f"URL must not contain user info: {raw!r}")
    if parsed.hostname != "127.0.0.1":
        fail(f"URL host must be 127.0.0.1: {raw!r}")
    try:
        port = parsed.port
    except ValueError:
        fail(f"invalid port in URL: {raw!r}")
    if port is None:
        fail(f"URL must include an explicit port: {raw!r}")
    if not 1 <= port <= 65535:
        fail(f"port must be between 1 and 65535: {raw!r}")
    if parsed.fragment:
        fail(f"URL must not contain a fragment: {raw!r}")
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    return port, target


def open_db(path):
    """Open (creating if needed) the database and ensure the schema exists."""
    try:
        conn = sqlite3.connect(path)
        conn.execute(SCHEMA)
        conn.commit()
    except sqlite3.Error as exc:
        fail(f"cannot open database {path!r}: {exc}")
    return conn


def probe(port, target, timeout):
    """Send exactly one GET, never following redirects.

    Returns (http_status, reason, duration_ms).
    """
    start = time.monotonic()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    http_status = None
    try:
        conn.request("GET", target)
        response = conn.getresponse()
        response.read()
        http_status = response.status
        reason = "ok" if 200 <= http_status < 300 else "http_status"
    except (socket.timeout, TimeoutError):
        reason = "timeout"
    except (OSError, http.client.HTTPException):
        reason = "connection_error"
    finally:
        conn.close()
    duration_ms = max(0, round((time.monotonic() - start) * 1000))
    return http_status, reason, duration_ms


def cmd_check(args):
    port, target = validate_url(args.url)
    # The database must be usable before any request is sent.
    conn = open_db(args.db)
    http_status, reason, duration_ms = probe(port, target, args.timeout)
    status = "success" if reason == "ok" else "failure"
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        cursor = conn.execute(
            "INSERT INTO checks (url, timestamp, duration_ms, status, http_status, reason)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (args.url, timestamp, duration_ms, status, http_status, reason),
        )
        conn.commit()
    except sqlite3.Error as exc:
        fail(f"cannot write to database {args.db!r}: {exc}")
    finally:
        conn.close()
    record = {
        "id": cursor.lastrowid,
        "url": args.url,
        "timestamp": timestamp,
        "duration_ms": duration_ms,
        "status": status,
        "http_status": http_status,
        "reason": reason,
    }
    print(json.dumps(record))
    sys.exit(0 if status == "success" else 1)


def cmd_recent(args):
    conn = open_db(args.db)
    try:
        rows = conn.execute(
            "SELECT id, url, timestamp, duration_ms, status, http_status, reason"
            " FROM checks ORDER BY id DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
    except sqlite3.Error as exc:
        fail(f"cannot read database {args.db!r}: {exc}")
    finally:
        conn.close()
    print(json.dumps([dict(zip(COLUMNS, row)) for row in rows]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="path to the SQLite database file")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check", help="probe a URL once and record the result")
    check_parser.add_argument("--url", required=True, help="http://127.0.0.1:<port>[path][?query]")
    check_parser.add_argument("--timeout", type=positive_float, default=1.0,
                              help="probe timeout in seconds (default: 1)")
    check_parser.set_defaults(func=cmd_check)

    recent_parser = subparsers.add_parser("recent", help="show recorded checks, newest first")
    recent_parser.add_argument("--limit", type=positive_int, default=5,
                               help="maximum number of records (default: 5)")
    recent_parser.set_defaults(func=cmd_recent)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
