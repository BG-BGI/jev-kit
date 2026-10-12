"""Local store for text that fast-jev-compaction cut out of a tool result.

The compaction plugin drops interior chunks of a Bash/Read/Grep/MCP result so
they never reach the model. It pipes each dropped range here first, gets a ref
ID back, and puts the ID in the omission note. The model can then ask the
plugin's `recall` tool for the range, which calls `get` below.

Nothing leaves the machine. The file is `recall.db` in the state directory,
mode 600 inside a 700 directory. Rows expire after a TTL and the table is held
under a size cap, both enforced on every `put`.

Everything fails open: `put` and `get` print one JSON object and exit 0 even on
error, so a broken store never blocks or slows a tool call.

    python3 -m airlock.recall put   < {"session","tool","ranges":[{"start","end","text"}]}
    python3 -m airlock.recall get   REF [--range A-B] [--max-chars N]
    python3 -m airlock.recall list | show REF | prune | purge
"""
import argparse
import datetime
import hashlib
import json
import sqlite3
import sys

from airlock import paths, platform_compat

DB_NAME = "recall.db"
BUSY_TIMEOUT_MS = 200
DEFAULT_TTL_DAYS = 7
DEFAULT_MAX_MB = 50
DEFAULT_MAX_CHARS = 20000
MAX_PUT_BYTES = 8 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS recall (
  ref TEXT NOT NULL,
  range_start INTEGER NOT NULL,
  range_end INTEGER NOT NULL,
  session TEXT,
  tool TEXT,
  created_at TEXT NOT NULL,
  size INTEGER NOT NULL,
  text TEXT NOT NULL,
  PRIMARY KEY (ref, range_start, range_end)
);
CREATE INDEX IF NOT EXISTS recall_created ON recall(created_at);
"""


def db_path():
    return paths.state_dir() / DB_NAME


def _connect(timeout_ms=BUSY_TIMEOUT_MS):
    d = db_path().parent
    d.mkdir(parents=True, exist_ok=True)
    platform_compat.restrict_path(d, 0o700)
    conn = sqlite3.connect(str(db_path()), timeout=timeout_ms / 1000.0)
    conn.executescript(SCHEMA)
    platform_compat.restrict_path(str(db_path()), 0o600)
    return conn


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def make_ref(session, tool, ranges):
    h = hashlib.sha256()
    h.update(("%s\0%s\0" % (session, tool)).encode("utf-8", "replace"))
    for r in ranges:
        h.update(("%d-%d\0" % (r["start"], r["end"])).encode())
        h.update(r["text"].encode("utf-8", "replace"))
        h.update(b"\0")
    return "r_" + h.hexdigest()[:8]


def _clean_ranges(ranges):
    out = []
    for r in ranges or []:
        if not isinstance(r, dict) or not isinstance(r.get("text"), str):
            continue
        s, e = r.get("start"), r.get("end")
        if not (isinstance(s, int) and isinstance(e, int) and 1 <= s <= e):
            continue
        out.append({"start": s, "end": e, "text": r["text"]})
    return out


def prune(conn, ttl_days=DEFAULT_TTL_DAYS, max_mb=DEFAULT_MAX_MB, now=None):
    """Drop expired rows, then oldest rows until under the size cap."""
    now = now or _now()
    cutoff = (now - datetime.timedelta(days=ttl_days)).isoformat()
    n = conn.execute("DELETE FROM recall WHERE created_at < ?", (cutoff,)).rowcount
    cap = int(max_mb * 1024 * 1024)
    total = conn.execute("SELECT COALESCE(SUM(size), 0) FROM recall").fetchone()[0]
    while total > cap:
        row = conn.execute(
            "SELECT ref, MIN(created_at) FROM recall GROUP BY ref ORDER BY MIN(created_at) LIMIT 1"
        ).fetchone()
        if not row:
            break
        freed = conn.execute("SELECT COALESCE(SUM(size), 0) FROM recall WHERE ref = ?",
                             (row[0],)).fetchone()[0]
        n += conn.execute("DELETE FROM recall WHERE ref = ?", (row[0],)).rowcount
        total -= freed
    return n


def put(payload, ttl_days=DEFAULT_TTL_DAYS, max_mb=DEFAULT_MAX_MB, now=None):
    """Store the ranges, return {"ok": True, "ref": ...}. Never raises."""
    try:
        ranges = _clean_ranges(payload.get("ranges"))
        if not ranges:
            return {"ok": False, "error": "no ranges"}
        session = str(payload.get("session") or "")
        tool = str(payload.get("tool") or "")
        ref = make_ref(session, tool, ranges)
        stamp = (now or _now()).isoformat()
        conn = _connect()
        try:
            with conn:
                for r in ranges:
                    conn.execute(
                        "INSERT OR REPLACE INTO recall VALUES (?,?,?,?,?,?,?,?)",
                        (ref, r["start"], r["end"], session, tool, stamp,
                         len(r["text"].encode("utf-8", "replace")), r["text"]),
                    )
                prune(conn, ttl_days, max_mb, now)
        finally:
            conn.close()
        return {"ok": True, "ref": ref}
    except Exception as exc:  # fail open
        return {"ok": False, "error": str(exc)[:200]}


def _parse_range(spec):
    if not spec:
        return None
    a, _, b = str(spec).partition("-")
    s = int(a)
    return (s, int(b) if b else s)


def get(ref, line_range=None, max_chars=DEFAULT_MAX_CHARS, ttl_days=DEFAULT_TTL_DAYS, now=None):
    """Return the stored lines for `ref`, optionally limited to a line range.

    Lines are returned in order, each prefixed with its original line number so
    the model can cite and narrow. Output is cut at `max_chars` with a hint.
    """
    try:
        rng = _parse_range(line_range) if isinstance(line_range, str) else line_range
        conn = _connect()
        try:
            cutoff = ((now or _now()) - datetime.timedelta(days=ttl_days)).isoformat()
            rows = conn.execute(
                "SELECT range_start, range_end, text FROM recall "
                "WHERE ref = ? AND created_at >= ? ORDER BY range_start",
                (ref, cutoff),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return {"ok": False, "error": "unknown or expired ref; re-run the command"}
        lines = []
        for start, _end, text in rows:
            for i, line in enumerate(text.split("\n")):
                n = start + i
                if rng is None or rng[0] <= n <= rng[1]:
                    lines.append("%d: %s" % (n, line))
        if not lines:
            return {"ok": False, "error": "range not stored; stored ranges: " +
                    ", ".join("%d-%d" % (r[0], r[1]) for r in rows)}
        text = "\n".join(lines)
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]
            text += "\n[truncated at %d chars; pass range=A-B for the rest]" % max_chars
        return {"ok": True, "text": text, "truncated": truncated}
    except Exception as exc:  # fail open
        return {"ok": False, "error": str(exc)[:200]}


def _list(conn):
    return conn.execute(
        "SELECT ref, tool, session, MIN(range_start), MAX(range_end), SUM(size), MIN(created_at) "
        "FROM recall GROUP BY ref ORDER BY MIN(created_at) DESC"
    ).fetchall()


def main(argv=None):
    p = argparse.ArgumentParser(prog="airlock.recall")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("put")
    sp.add_argument("--ttl-days", type=float, default=DEFAULT_TTL_DAYS)
    sp.add_argument("--max-mb", type=float, default=DEFAULT_MAX_MB)
    sg = sub.add_parser("get")
    sg.add_argument("ref")
    sg.add_argument("--range")
    sg.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    sg.add_argument("--ttl-days", type=float, default=DEFAULT_TTL_DAYS)
    sub.add_parser("list")
    ss = sub.add_parser("show")
    ss.add_argument("ref")
    sub.add_parser("prune")
    sub.add_parser("purge")
    a = p.parse_args(argv)

    if a.cmd == "put":
        try:
            payload = json.loads(sys.stdin.read(MAX_PUT_BYTES))
        except Exception:
            payload = {}
        print(json.dumps(put(payload, a.ttl_days, a.max_mb)))
    elif a.cmd == "get":
        print(json.dumps(get(a.ref, a.range, a.max_chars, a.ttl_days)))
    else:
        conn = _connect(timeout_ms=2000)
        try:
            if a.cmd == "list":
                for ref, tool, session, lo, hi, size, ts in _list(conn):
                    print("%s  %-6s lines %d-%d  %d B  %s" % (ref, tool, lo, hi, size, ts))
            elif a.cmd == "show":
                print(get(a.ref, max_chars=10 ** 9).get("text", "not found"))
            elif a.cmd == "prune":
                with conn:
                    print("pruned %d rows" % prune(conn))
            elif a.cmd == "purge":
                with conn:
                    print("purged %d rows" % conn.execute("DELETE FROM recall").rowcount)
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
