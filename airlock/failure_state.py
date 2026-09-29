"""Per-session record of recent tool failures, for the failure-verdict hook.

hooks/airlock_failure_verdict.py records every failed tool call here and asks
Jev only once a session has failed MIN_FAILURES times inside WINDOW_S: one
failure is Claude's to handle, a run of them is where retries start to cost.
The same verdict is said at most once per ANNOUNCE_WINDOW_S, so a thrashing
session hears it once instead of after every failure.

State lives at ~/.local/state/airlock/failures.json, one list per session,
whole-file locked, 700/600 on POSIX, the same call shape as
airlock/browse_state.py. Every failure to read or write reads as "nothing
recorded", which means the hook stays quiet.
"""
import json
import os
import time

from . import paths
from . import platform_compat

STATE_DIR = paths.state_dir()
STATE_FILE = STATE_DIR / "failures.json"

WINDOW_S = 900
ANNOUNCE_WINDOW_S = 600
MAX_KEPT = 8
_TEXT_LIMIT = 400


def _ensure_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    platform_compat.restrict_path(STATE_DIR, 0o700)


def _open_locked(lock_kind):
    _ensure_dir()
    fd = os.open(str(STATE_FILE),
                 os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
    platform_compat.lock_file(fd, lock_kind)
    return fd


def _close(fd):
    platform_compat.unlock_file(fd)
    try:
        os.close(fd)
    except Exception:
        pass


def _load(fd):
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 10 * 1024 * 1024)
        data = json.loads(raw.decode("utf-8")) if raw else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(fd, data):
    raw = json.dumps(data).encode("utf-8")
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, raw)


def _fresh(rows, now, window_s):
    out = []
    for row in rows if isinstance(rows, list) else []:
        try:
            if now - float(row.get("ts")) <= window_s:
                out.append(row)
        except Exception:
            continue
    return out


def _prune(data, now):
    for sess in list(data.keys()):
        entry = data.get(sess)
        if not isinstance(entry, dict):
            del data[sess]
            continue
        entry["failures"] = _fresh(entry.get("failures"), now, WINDOW_S)[-MAX_KEPT:]
        announced = entry.get("announced") if isinstance(entry.get("announced"), dict) else {}
        entry["announced"] = {k: v for k, v in announced.items()
                              if isinstance(v, (int, float)) and now - v <= ANNOUNCE_WINDOW_S}
        if not entry["failures"] and not entry["announced"]:
            del data[sess]
    return data


def record_failure(session_id, tool_name, summary, error):
    """Record one failed call and return this session's failures inside the
    window, oldest first, this one last. [] on any error. Never raises."""
    try:
        fd = _open_locked(platform_compat.LOCK_EXCLUSIVE)
    except Exception:
        return []
    try:
        now = time.time()
        data = _prune(_load(fd), now)
        entry = data.setdefault(session_id or "", {"failures": [], "announced": {}})
        entry["failures"].append({
            "ts": now,
            "tool": str(tool_name or "")[:80],
            "summary": str(summary or "")[:_TEXT_LIMIT],
            "error": str(error or "")[:_TEXT_LIMIT],
        })
        entry["failures"] = entry["failures"][-MAX_KEPT:]
        _save(fd, data)
        return list(entry["failures"])
    except Exception:
        return []
    finally:
        _close(fd)


def claim_announcement(session_id, verdict):
    """True when `verdict` has not been said to this session inside
    ANNOUNCE_WINDOW_S, and marks it said. False on any error (stay quiet)."""
    try:
        fd = _open_locked(platform_compat.LOCK_EXCLUSIVE)
    except Exception:
        return False
    try:
        now = time.time()
        data = _prune(_load(fd), now)
        entry = data.setdefault(session_id or "", {"failures": [], "announced": {}})
        if verdict in entry["announced"]:
            return False
        entry["announced"][verdict] = now
        _save(fd, data)
        return True
    except Exception:
        return False
    finally:
        _close(fd)
