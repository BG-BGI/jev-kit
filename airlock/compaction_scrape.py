"""Scrape fast-jev-compaction stats out of Claude Code transcripts into metrics.db.

The plugin (a separate TypeScript repo) writes its stats only to the session
transcript, as `type: system` records whose content starts with
"fast-jev-compaction: ". Only those records are read: the same text quoted in
a tool result or a prompt is not a compaction event.

Forked and resumed sessions copy records into new transcript files, so rows
are keyed on a hash of (timestamp, text) and a copy is ignored.

Each row also carries requests_after: the number of distinct assistant
requests later in the same transcript. A trimmed tool result would otherwise
have ridden along in every one of those requests, so the effective context
saving of a trim is (chars saved / 4) * requests_after. The count grows while
a session continues and a resumed session's file holds the longer history, so
re-scraping updates an existing row to the largest count seen.

    python3 -m airlock.compaction_scrape [--projects-dir DIR]
"""
import bisect
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

from . import metrics

PREFIX = "fast-jev-compaction: "
_TOOL = re.compile(r"^(?P<tool>\S+) output (?P<pct>\d+)% smaller \((?P<b>\d+) -> (?P<a>\d+) chars\); "
                   r"(?P<om>\d+)/(?P<ch>\d+) chunks omitted")
_PASS = re.compile(r"^(?P<tool>\S+) result passed through \((?P<err>.*)\)$")
_KEPT = re.compile(r"^kept (?P<kept>\d+)/(?P<total>\d+) messages, no summary \((?P<red>\d+)% reduction;(?P<rest>.*)\)$")
_FALL = re.compile(r"^fallback to built-in summary \((?P<why>.*)\)$")
_SKIP = re.compile(r"^auto-compact skipped \((?P<err>.*)\)$")
_ASST = re.compile(r'"type"\s*:\s*"assistant"')
_REQID = re.compile(r'"requestId"\s*:\s*"([^"]+)"')
_TS = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')

TABLE = """
CREATE TABLE IF NOT EXISTS compaction (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  project TEXT,
  session_id TEXT,
  cwd TEXT,
  mode TEXT NOT NULL,
  tool TEXT,
  outcome TEXT NOT NULL,
  chars_before INTEGER,
  chars_after INTEGER,
  chunks INTEGER,
  chunks_omitted INTEGER,
  msgs_kept INTEGER,
  msgs_total INTEGER,
  reduction_pct INTEGER,
  call_dropped INTEGER,
  pinned INTEGER,
  state_tokens INTEGER,
  requests INTEGER,
  requests_after INTEGER,
  detail TEXT,
  row_hash TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS compaction_ts ON compaction(ts);
"""

_COLS = ("ts", "project", "session_id", "cwd", "mode", "tool", "outcome", "chars_before", "chars_after",
         "chunks", "chunks_omitted", "msgs_kept", "msgs_total", "reduction_pct", "call_dropped", "pinned",
         "state_tokens", "requests", "requests_after", "detail", "row_hash")


def ensure_schema(conn):
    """Create the table, and add requests_after to a database from before it."""
    conn.executescript(TABLE)
    try:
        conn.execute("ALTER TABLE compaction ADD COLUMN requests_after INTEGER")
    except sqlite3.OperationalError:
        pass


def _num(pattern, text):
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


def parse(text):
    """Map one log message onto compaction columns, or None for a line that
    is not a measurable event (the per-line `decisions:` dumps)."""
    m = _TOOL.match(text)
    if m:
        return {"mode": "tool", "tool": m["tool"], "outcome": "trimmed", "reduction_pct": int(m["pct"]),
                "chars_before": int(m["b"]), "chars_after": int(m["a"]),
                "chunks": int(m["ch"]), "chunks_omitted": int(m["om"])}
    m = _PASS.match(text)
    if m:
        return {"mode": "tool", "tool": m["tool"], "outcome": "passed_through", "detail": m["err"][:300]}
    m = _KEPT.match(text)
    if m:
        rest = m["rest"]
        return {"mode": "session", "outcome": "kept", "msgs_kept": int(m["kept"]), "msgs_total": int(m["total"]),
                "reduction_pct": int(m["red"]), "call_dropped": _num(r"(\d+) call_dropped", rest),
                "pinned": _num(r"(\d+) pinned", rest), "state_tokens": _num(r"state ~(\d+) tokens", rest),
                "requests": _num(r"in (\d+) request", rest), "detail": rest.strip()[:300]}
    m = _FALL.match(text)
    if m:
        why = m["why"]
        return {"mode": "session", "outcome": "fallback", "reduction_pct": _num(r"(\d+)% reduction", why),
                "call_dropped": _num(r"(\d+) call_dropped", why), "pinned": _num(r"(\d+) pinned", why),
                "state_tokens": _num(r"state ~(\d+) tokens", why), "requests": _num(r"in (\d+) request", why),
                "detail": why[:300]}
    m = _SKIP.match(text)
    if m:
        return {"mode": "session", "outcome": "auto_compact_skipped", "detail": m["err"][:300]}
    return None


def _request_times(lines):
    """Sorted first-seen timestamps of each distinct assistant requestId."""
    seen = {}
    for line in lines:
        if "assistant" not in line or not _ASST.search(line):
            continue
        rid, ts = _REQID.search(line), _TS.search(line)
        if rid and ts and rid.group(1) not in seen:
            seen[rid.group(1)] = ts.group(1)
    return sorted(seen.values())


def scrape(projects_dir=None, conn=None):
    """Returns (events_seen, rows_added)."""
    root = Path(projects_dir) if projects_dir else Path.home() / ".claude" / "projects"
    own = conn is None
    conn = conn or metrics._connect(timeout_ms=5000)
    seen = added = 0
    try:
        ensure_schema(conn)
        with conn:
            for f in sorted(root.glob("*/*.jsonl")):
                try:
                    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                req_ts = _request_times(lines)
                for line in lines:
                    if PREFIX not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    content = rec.get("content")
                    if rec.get("type") != "system" or not isinstance(content, str) or not content.startswith(PREFIX):
                        continue
                    row = parse(content[len(PREFIX):])
                    if row is None:
                        continue
                    seen += 1
                    ts = rec.get("timestamp") or ""
                    after = len(req_ts) - bisect.bisect_right(req_ts, ts)
                    full = dict.fromkeys(_COLS)
                    full.update(row, ts=ts, project=f.parent.name, session_id=rec.get("sessionId"),
                                cwd=rec.get("cwd"), requests_after=after,
                                row_hash=hashlib.sha256((ts + "\n" + content).encode("utf-8")).hexdigest())
                    added += conn.execute(
                        "INSERT OR IGNORE INTO compaction (%s) VALUES (%s)" % (",".join(_COLS), ",".join("?" * len(_COLS))),
                        [full[c] for c in _COLS]).rowcount
                    # A resumed session's file holds the longer history: keep the largest count.
                    conn.execute("UPDATE compaction SET requests_after = ? WHERE row_hash = ?"
                                 " AND (requests_after IS NULL OR requests_after < ?)",
                                 (after, full["row_hash"], after))
    finally:
        if own:
            conn.close()
    return seen, added


def _main(argv):
    pdir = argv[1] if len(argv) > 1 and argv[0] == "--projects-dir" else None
    seen, added = scrape(pdir)
    print("scanned %d compaction events, added %d -> %s" % (seen, added, metrics.db_path()))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
