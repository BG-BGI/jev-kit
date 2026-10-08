"""SQLite metrics store: one place to measure every jev-kit action.

~/.local/state/airlock/metrics.db (same state dir and privacy rules as the
JSONL logs; see airlock/paths.py). Three tables (the third, `compaction`, is in compaction_scrape.py):

  events     one row per logged action (guard verdict, rule deny, decide call,
             skill suggestion, failure verdict, health probe...). Fed by
             log.append(), the single choke point every component already
             writes through, so new components are measured for free.
  jev_calls  one row per Jev request that left the machine, written by
             client.ask(): transport (daemon/direct), latency, tokens, error.

The JSONL files stay the source of truth for row detail; the DB is the query
layer. `backfill` loads existing JSONL and is idempotent (rows are keyed on a
hash of source + line). Never raises on the write path: a metrics failure must
not slow or break a hook.

    python3 -m airlock.metrics backfill
    python3 -m airlock.metrics scrape-compaction   # fast-jev-compaction stats, from transcripts
    python3 -m airlock.metrics scrape-overhead     # per-session context overhead, from transcripts
    python3 -m airlock.metrics report [--since YYYY-MM-DD]
"""
import datetime
import hashlib
import json
import sqlite3
import sys

from . import paths, platform_compat

DB_NAME = "metrics.db"
# USD per million tokens (input + output), the figure decide/server.py bills at.
PRICE_PER_MTOK_USD = 0.042
BUSY_TIMEOUT_MS = 200  # hooks are latency-sensitive: drop the row, don't wait

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  source TEXT NOT NULL,
  component TEXT NOT NULL,
  session_id TEXT,
  cwd TEXT,
  tool_name TEXT,
  mode TEXT,
  rule_id TEXT,
  action TEXT,
  denied INTEGER NOT NULL DEFAULT 0,
  jev_called INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cost_usd REAL,
  error TEXT,
  row_hash TEXT NOT NULL UNIQUE,
  raw TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_component ON events(component, ts);
CREATE TABLE IF NOT EXISTS jev_calls (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  transport TEXT NOT NULL,
  ok INTEGER NOT NULL,
  latency_ms INTEGER,
  input_tokens INTEGER,
  output_tokens INTEGER,
  n_questions INTEGER,
  error TEXT
);
CREATE INDEX IF NOT EXISTS jev_calls_ts ON jev_calls(ts);
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
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _int(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _flatten(entry, source):
    """Map a JSONL row of any component onto the events columns."""
    usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}
    tokens = _int(entry.get("tokens"))  # skill-suggest logs a single total
    in_tok = _int(usage.get("input_tokens"))
    out_tok = _int(usage.get("output_tokens"))
    latency = _int(entry.get("latency_ms"))
    action = entry.get("action") or entry.get("verdict") or entry.get("status") or entry.get("skill")
    error = entry.get("error")
    # failure_verdict's last_error is the failed tool call it judged, not an error of its own.
    if error is None and entry.get("guard") != "failure_verdict":
        error = entry.get("last_error")
    cost = entry.get("jev_cost_usd")
    # Guards log action="deny" on every row and put the verdict in would_deny;
    # rows from other components have no would_deny and use action alone.
    denied = entry["would_deny"] if "would_deny" in entry else action in ("deny", "block")
    return {
        "denied": int(bool(denied)),
        "ts": str(entry.get("ts") or _now()),
        "component": str(entry.get("guard") or source),
        "session_id": entry.get("session_id"),
        "cwd": entry.get("cwd"),
        "tool_name": entry.get("tool_name"),
        "mode": entry.get("mode"),
        "rule_id": entry.get("rule_id"),
        "action": None if action is None else str(action),
        "jev_called": int(latency is not None or in_tok is not None or tokens is not None),
        "latency_ms": latency,
        "input_tokens": in_tok if in_tok is not None else tokens,
        "output_tokens": out_tok,
        "cost_usd": float(cost) if isinstance(cost, (int, float)) else None,
        "error": None if error is None else str(error)[:300],
    }


_COLS = ("ts", "source", "component", "session_id", "cwd", "tool_name", "mode", "rule_id",
         "action", "denied", "jev_called", "latency_ms", "input_tokens", "output_tokens", "cost_usd",
         "error", "row_hash", "raw")


def _insert_event(conn, entry, source, raw=None):
    raw = raw if raw is not None else json.dumps(entry, default=str, ensure_ascii=False)
    row = _flatten(entry, source)
    row.update(source=source, raw=raw,
               row_hash=hashlib.sha256((source + "\n" + raw).encode("utf-8")).hexdigest())
    cur = conn.execute(
        "INSERT OR IGNORE INTO events (%s) VALUES (%s)" % (",".join(_COLS), ",".join("?" * len(_COLS))),
        [row[c] for c in _COLS])
    return cur.rowcount


def record_event(entry, source):
    """Called from log.append(). Never raises."""
    try:
        conn = _connect()
        try:
            with conn:
                _insert_event(conn, entry, source)
        finally:
            conn.close()
    except Exception:
        return


def record_call(transport, ok, latency_ms=None, response=None, n_questions=None, error=None):
    """Called from client.ask() for every Jev request. Never raises."""
    try:
        usage = (response or {}).get("usage") if isinstance(response, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO jev_calls (ts, transport, ok, latency_ms, input_tokens, output_tokens,"
                    " n_questions, error) VALUES (?,?,?,?,?,?,?,?)",
                    (_now(), transport, int(bool(ok)), _int(latency_ms), _int(usage.get("input_tokens")),
                     _int(usage.get("output_tokens")), _int(n_questions),
                     None if error is None else str(error)[:300]))
        finally:
            conn.close()
    except Exception:
        return


def backfill(state_dir=None):
    """Load every *.jsonl in the state dir into events. Idempotent."""
    state = state_dir or paths.state_dir()
    conn = _connect(timeout_ms=5000)
    added = seen = 0
    try:
        with conn:
            for f in sorted(state.glob("*.jsonl")):
                for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    seen += 1
                    added += _insert_event(conn, entry, f.stem, raw=line)
    finally:
        conn.close()
    return seen, added


def _pct(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def report(since=None, out=sys.stdout):
    conn = _connect(timeout_ms=5000)
    since = since or "0000-00-00"
    try:
        rows = conn.execute(
            "SELECT component, denied, jev_called, latency_ms, input_tokens, output_tokens, cost_usd, error"
            " FROM events WHERE substr(ts,1,10) >= ?", (since,)).fetchall()
        calls = conn.execute(
            "SELECT transport, ok, latency_ms, input_tokens, output_tokens FROM jev_calls"
            " WHERE substr(ts,1,10) >= ?", (since,)).fetchall()
    finally:
        conn.close()

    by = {}
    for comp, denied, called, lat, tin, tout, cost, err in rows:
        b = by.setdefault(comp, {"n": 0, "jev": 0, "lat": [], "in": 0, "out": 0, "cost": 0.0, "err": 0, "deny": 0})
        b["n"] += 1
        b["jev"] += called
        b["err"] += int(bool(err))
        b["deny"] += denied
        b["in"] += tin or 0
        b["out"] += tout or 0
        b["cost"] += cost if cost is not None else ((tin or 0) + (tout or 0)) * PRICE_PER_MTOK_USD / 1e6
        if called and lat is not None:
            b["lat"].append(lat)
    out.write("events since %s (UTC dates)\n" % since)
    out.write("%-18s %7s %6s %7s %7s %9s %9s %7s %9s\n" %
              ("component", "events", "jev", "denies", "errors", "in_tok", "out_tok", "p95ms", "est_usd"))
    for comp, b in sorted(by.items(), key=lambda kv: -kv[1]["n"]):
        p95 = _pct(b["lat"], 0.95)
        out.write("%-18s %7d %6d %7d %7d %9d %9d %7s %9.4f\n" %
                  (comp[:18], b["n"], b["jev"], b["deny"], b["err"], b["in"], b["out"],
                   "-" if p95 is None else p95, b["cost"]))
    _report_compaction(out, since)
    _report_overhead(out, since)
    if calls:
        out.write("\njev requests (client.ask) since %s\n" % since)
        for t in sorted({c[0] for c in calls}):
            cs = [c for c in calls if c[0] == t]
            lat = [c[2] for c in cs if c[2] is not None]
            out.write("  %-8s n=%d ok=%d p50=%sms p95=%sms in_tok=%d out_tok=%d\n" % (
                t, len(cs), sum(c[1] for c in cs), _pct(lat, 0.5), _pct(lat, 0.95),
                sum(c[3] or 0 for c in cs), sum(c[4] or 0 for c in cs)))


def _report_compaction(out, since):
    from . import compaction_scrape
    conn = _connect(timeout_ms=5000)
    try:
        compaction_scrape.ensure_schema(conn)
        tool = conn.execute(
            "SELECT tool, COUNT(*), SUM(chars_before), SUM(chars_after), SUM(chunks_omitted), SUM(chunks),"
            " SUM((chars_before - chars_after) * COALESCE(requests_after, 0))"
            " FROM compaction WHERE outcome='trimmed' AND substr(ts,1,10) >= ? GROUP BY tool", (since,)).fetchall()
        sess = conn.execute(
            "SELECT outcome, COUNT(*), SUM(msgs_kept), SUM(msgs_total), AVG(reduction_pct), SUM(requests)"
            " FROM compaction WHERE mode='session' AND substr(ts,1,10) >= ? GROUP BY outcome", (since,)).fetchall()
        passed = conn.execute(
            "SELECT COUNT(*) FROM compaction WHERE outcome='passed_through' AND substr(ts,1,10) >= ?",
            (since,)).fetchone()[0]
    finally:
        conn.close()
    if not (tool or sess or passed):
        return
    out.write("\ncompaction (from transcripts) since %s\n" % since)
    for t, n, b, a, om, ch, eff in tool:
        out.write("  tool %-8s n=%d chars %d -> %d saved %d (%.1f%%, ~%d tok) chunks omitted %d/%d"
                  " effective ~%d tok over later requests\n" % (
                      t, n, b, a, b - a, 100.0 * (b - a) / max(1, b), (b - a) // 4, om, ch, (eff or 0) // 4))
    for o, n, k, tot, red, req in sess:
        out.write("  session %-9s n=%d msgs kept %s/%s avg reduction %.0f%% jev requests %s\n" % (
            o, n, k, tot, red or 0, req))
    if passed:
        out.write("  passed through (errors): %d\n" % passed)


def _report_overhead(out, since):
    """Per-session context overhead. first_ctx_tokens is the number that says
    whether the disclosure work shrank the per-session baseline."""
    from . import overhead_scrape
    conn = _connect(timeout_ms=5000)
    try:
        overhead_scrape.ensure_schema(conn)
        n, requests, cache_read, cost, priced = conn.execute(
            "SELECT COUNT(*), SUM(requests), SUM(cache_read_tokens), SUM(est_cost_usd),"
            " COUNT(est_cost_usd) FROM session_overhead WHERE substr(ts,1,10) >= ?",
            (since,)).fetchone()
        firsts = [r[0] for r in conn.execute(
            "SELECT first_ctx_tokens FROM session_overhead"
            " WHERE substr(ts,1,10) >= ? AND first_ctx_tokens IS NOT NULL", (since,))]
    finally:
        conn.close()
    if not n:
        return
    out.write("\nsession overhead (from transcripts) since %s\n" % since)
    out.write("  sessions %d, requests %d, cache-read %d tok\n"
              % (n, requests or 0, cache_read or 0))
    if firsts:
        out.write("  first-request context (per-session baseline): mean %d tok, median %s tok\n"
                  % (sum(firsts) / len(firsts), _pct(firsts, 0.5)))
    out.write("  est cost $%.4f over %d priced session(s) of %d\n" % (cost or 0.0, priced, n))


def _main(argv):
    cmd = argv[0] if argv else "report"
    if cmd == "backfill":
        seen, added = backfill()
        print("scanned %d rows, added %d -> %s" % (seen, added, db_path()))
        return 0
    if cmd == "report":
        since = argv[2] if len(argv) > 2 and argv[1] == "--since" else None
        report(since)
        return 0
    if cmd == "scrape-compaction":
        from . import compaction_scrape
        return compaction_scrape._main(argv[1:])
    if cmd == "scrape-overhead":
        from . import overhead_scrape
        return overhead_scrape._main(argv[1:])
    print("usage: python3 -m airlock.metrics"
          " [backfill|scrape-compaction|scrape-overhead|report [--since YYYY-MM-DD]]")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
