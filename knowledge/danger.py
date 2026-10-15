"""Spool for Airlock denials, so the `danger` bucket has a capture path.

A denied PreToolUse call fires neither PostToolUse nor PostToolUseFailure, so
the capture hook never sees it. `airlock/enforce.py` calls `spool` right after
it prints a deny, ask or warn. That is one redacted JSON line appended to a
file: no Jev call, no network, nothing on the critical path of the verdict.

The capture hook calls `drain` on its next event and sends each row through
the normal gate.

Everything fails open: `spool` and `drain` never raise.
"""
import json
import os
import re
import time

SPOOL_NAME = "knowledge-danger.jsonl"
MAX_BYTES = 64 * 1024
MAX_ROWS = 50
REASON_CHARS = 300
SUMMARY_CHARS = 160
ACTIONS = ("deny", "ask", "warn")
_RULE_RE = re.compile(r"\b(R\d+[\w-]*)\b")


ACTIVE_NAME = "knowledge-active"
ACTIVE_MAX_AGE_S = 7 * 24 * 3600


def _path():
    from airlock import paths
    return paths.state_dir() / SPOOL_NAME


def mark_active():
    """Touch the marker that says a capture hook is installed to drain the spool.

    The SessionStart hook calls this. Without a fresh marker `spool` writes
    nothing, so a machine that never ran `--knowledge` collects no rows, and
    one that uninstalled it stops collecting within a week.
    """
    try:
        from airlock import paths
        p = paths.state_dir() / ACTIVE_NAME
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        return True
    except Exception:
        return False


def _active():
    try:
        from airlock import paths
        age = time.time() - (paths.state_dir() / ACTIVE_NAME).stat().st_mtime
        return age < ACTIVE_MAX_AGE_S
    except Exception:
        return False


def _redact(text):
    try:
        from airlock import redact
        return redact.redact(str(text))
    except Exception:
        return ""


def _summary(tool, tool_input):
    ti = tool_input if isinstance(tool_input, dict) else {}
    for key in ("command", "file_path", "url", "pattern", "description"):
        if ti.get(key):
            return _redact(str(ti[key]))[:SUMMARY_CHARS]
    return ""


def spool(action, reason, data=None):
    """Append one danger row. `data` is the PreToolUse payload when known."""
    try:
        from knowledge import config
        if not config.enabled() or action not in ACTIONS or not _active():
            return False
        data = data if isinstance(data, dict) else {}
        reason = _redact(reason)[:REASON_CHARS]
        m = _RULE_RE.search(reason)
        row = {
            "t": time.time(),
            "action": action,
            "rule": m.group(1) if m else "",
            "reason": reason,
            "tool": str(data.get("tool_name") or ""),
            "summary": _summary(data.get("tool_name"), data.get("tool_input")),
            "session_id": str(data.get("session_id") or ""),
            "cwd": str(data.get("cwd") or ""),
        }
        p = _path()
        if p.exists() and p.stat().st_size > MAX_BYTES:
            return False
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(p), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(row) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except Exception:
        return False


def drain(limit=MAX_ROWS):
    """Return and clear the spooled rows, oldest first, at most `limit`."""
    try:
        p = _path()
        if not p.exists():
            return []
        claimed = p.with_name(p.name + ".%d" % os.getpid())
        os.replace(str(p), str(claimed))
        rows = []
        try:
            with open(str(claimed), encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(row, dict) and row.get("action") in ACTIONS:
                        rows.append(row)
        finally:
            try:
                os.remove(str(claimed))
            except Exception:
                pass
        return rows[:limit]
    except Exception:
        return []


def dedup_key(row):
    """Same rule against the same kind of call is one danger, not many."""
    return "%s|%s|%s" % (row.get("rule") or row.get("reason", "")[:60], row.get("tool", ""),
                         row.get("action", ""))
