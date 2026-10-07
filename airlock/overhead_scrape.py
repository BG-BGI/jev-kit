"""Scrape per-session context overhead out of Claude Code transcripts into metrics.db.

Every assistant turn in a transcript carries `message.usage`: the input,
cache-read and cache-creation tokens the request was billed for. The FIRST
assistant message of a session is the interesting one: its context is almost
entirely the fixed per-session baseline (system prompt, CLAUDE.md, skill
descriptions, MCP tool schemas) that the disclosure work (disclosure/skills.py,
disclosure/mcp.py, disclosure/claudemd.py) exists to shrink. Tracking
first_ctx_tokens over time is how we prove the change worked -- or didn't.

Streamed responses write the same assistant message across several transcript
rows, each repeating the same `message.id` and the same usage block, so usage
is counted once per message id (the record `uuid` stands in when the id is
missing). Subagent transcripts live under */subagents/*.jsonl and are keyed
"sub:<sessionId>" so they never merge with -- or overwrite -- the parent
session's row.

A session that is still running grows between scrapes, so rows are keyed on
session_id and refreshed in place (DELETE+INSERT inside the transaction, the
same conservative stance as compaction_scrape's INSERT OR IGNORE: no reliance
on a newer sqlite3 upsert syntax).

est_cost_usd prices the session from airlock.pricing.rates() when the model
string names a known family and the gateway listed cache rates; anything
unknown is NULL, never a guess.

    python3 -m airlock.metrics scrape-overhead [--root PATH] [--days N]
"""
import datetime
import json
import sys
import time
from pathlib import Path

from . import metrics

ALIASES = ("haiku", "sonnet", "opus", "fable")

TABLE = """
CREATE TABLE IF NOT EXISTS session_overhead (
  id INTEGER PRIMARY KEY,
  ts TEXT,
  project TEXT,
  session_id TEXT UNIQUE,
  model TEXT,
  requests INTEGER,
  first_ctx_tokens INTEGER,
  avg_ctx_tokens INTEGER,
  max_ctx_tokens INTEGER,
  input_tokens INTEGER,
  cache_read_tokens INTEGER,
  cache_write_tokens INTEGER,
  output_tokens INTEGER,
  est_cost_usd REAL,
  scraped_at TEXT
);
CREATE INDEX IF NOT EXISTS session_overhead_ts ON session_overhead(ts);
"""

_COLS = ("ts", "project", "session_id", "model", "requests", "first_ctx_tokens", "avg_ctx_tokens",
         "max_ctx_tokens", "input_tokens", "cache_read_tokens", "cache_write_tokens",
         "output_tokens", "est_cost_usd", "scraped_at")


def ensure_schema(conn):
    conn.executescript(TABLE)


def _tok(value):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def model_alias(model):
    """haiku/sonnet/opus/fable by substring, or None for an unknown model."""
    model = model if isinstance(model, str) else ""
    for alias in ALIASES:
        if alias in model:
            return alias
    return None


def est_cost(model, input_tokens, cache_read, cache_write, output_tokens):
    """USD for the session from the cached gateway rate card, or None when
    the model family or any needed rate is unknown. Lazy import and a blanket
    except: a broken pricing cache must cost the row its price, not the scrape."""
    alias = model_alias(model)
    if alias is None:
        return None
    try:
        from . import pricing
        rates = pricing.rates(alias)
    except Exception:
        return None
    if not rates or rates.get("cache_read") is None or rates.get("cache_write") is None:
        return None
    return (input_tokens * rates["input"] + cache_read * rates["cache_read"]
            + cache_write * rates["cache_write"] + output_tokens * rates["output"])


def _scan_file(path, root, sessions):
    """Fold one transcript's assistant usage into the per-session aggregates."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    try:
        project = path.relative_to(root).parts[0]
    except (ValueError, IndexError):
        project = path.parent.name
    sub = path.parent.name == "subagents"
    for line in lines:
        if '"assistant"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        message = rec.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            continue
        sid = str(rec.get("sessionId") or path.stem)
        key = "sub:" + sid if sub else sid
        s = sessions.setdefault(key, {"project": project, "ids": set(), "models": {},
                                      "ctxs": [], "first": None,
                                      "in": 0, "cread": 0, "cwrite": 0, "out": 0})
        # Streamed messages repeat the usage block across rows that share one
        # message id: each id is counted once. The record uuid is the fallback
        # key, so an id-less row still cannot be double-counted on re-reads.
        mid = message.get("id") or rec.get("uuid")
        if mid is not None:
            if mid in s["ids"]:
                continue
            s["ids"].add(mid)
        tin = _tok(usage.get("input_tokens"))
        cread = _tok(usage.get("cache_read_input_tokens"))
        cwrite = _tok(usage.get("cache_creation_input_tokens"))
        tout = _tok(usage.get("output_tokens"))
        ctx = tin + cread + cwrite
        ts = str(rec.get("timestamp") or "")
        if s["first"] is None or (ts and (not s["first"][0] or ts < s["first"][0])):
            s["first"] = (ts, ctx)
        s["ctxs"].append(ctx)
        model = message.get("model")
        if isinstance(model, str) and model:
            s["models"][model] = s["models"].get(model, 0) + 1
        s["in"] += tin
        s["cread"] += cread
        s["cwrite"] += cwrite
        s["out"] += tout


def scrape(root=None, days=None, conn=None):
    """Returns (sessions_seen, rows_written)."""
    root = Path(root) if root else Path.home() / ".claude" / "projects"
    cutoff = None if days is None else time.time() - days * 86400
    sessions = {}
    try:
        files = sorted(root.rglob("*.jsonl"))
    except OSError:
        files = []
    for f in files:
        if cutoff is not None:
            try:
                if f.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
        _scan_file(f, root, sessions)
    own = conn is None
    conn = conn or metrics._connect(timeout_ms=5000)
    written = 0
    try:
        ensure_schema(conn)
        scraped_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with conn:
            for key, s in sorted(sessions.items()):
                if not s["ctxs"]:
                    continue
                model = (max(sorted(s["models"]), key=lambda m: s["models"][m])
                         if s["models"] else None)
                row = {
                    "ts": s["first"][0] if s["first"] else "",
                    "project": s["project"],
                    "session_id": key,
                    "model": model,
                    "requests": len(s["ctxs"]),
                    "first_ctx_tokens": s["first"][1] if s["first"] else None,
                    "avg_ctx_tokens": int(sum(s["ctxs"]) / len(s["ctxs"])),
                    "max_ctx_tokens": max(s["ctxs"]),
                    "input_tokens": s["in"],
                    "cache_read_tokens": s["cread"],
                    "cache_write_tokens": s["cwrite"],
                    "output_tokens": s["out"],
                    "est_cost_usd": est_cost(model, s["in"], s["cread"], s["cwrite"], s["out"]),
                    "scraped_at": scraped_at,
                }
                # A live session grows between scrapes: refresh its row.
                conn.execute("DELETE FROM session_overhead WHERE session_id = ?", (key,))
                conn.execute(
                    "INSERT INTO session_overhead (%s) VALUES (%s)"
                    % (",".join(_COLS), ",".join("?" * len(_COLS))),
                    [row[c] for c in _COLS])
                written += 1
    finally:
        if own:
            conn.close()
    return len(sessions), written


def _main(argv):
    root = days = None
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg == "--root" and args:
            root = args.pop(0)
        elif arg == "--days" and args:
            try:
                days = int(args.pop(0))
            except ValueError:
                print("usage: python3 -m airlock.metrics scrape-overhead [--root PATH] [--days N]")
                return 2
        else:
            print("usage: python3 -m airlock.metrics scrape-overhead [--root PATH] [--days N]")
            return 2
    sessions, written = scrape(root, days)
    print("scanned %d session(s), wrote %d row(s) -> %s" % (sessions, written, metrics.db_path()))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
