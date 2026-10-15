#!/usr/bin/env python3
"""PostToolUseFailure + PostToolUse hook: feed the experience graph, and replay a known fix.

WHY THIS EXISTS
===============

The experience graph (docs/experience-graph.md) is worth most at one moment:
a tool call just failed with an error the graph has already seen resolved.
This hook looks that error up by exact signature and, on a hit, says so in
two lines. It is also stage 1 of capture: cheap local rules that nominate
candidates, so no Jev call happens per tool use.

WHAT IT DOES
============

PostToolUseFailure
  1. Hash the failure to a signature (knowledge/signature.py) and look it up
     with store.fixes_for. A hit prints FAIL_LINES lines of additionalContext,
     at most once per signature per FAIL_DEDUP_SECONDS, and stops there:
     the fix-recall path makes no Jev call.
  2. Otherwise apply the local rules and, if one fires, call ingest:
       failure repeat   the same signature twice in 15 minutes
       danger           an Airlock BLOCKED / NEEDS APPROVAL / WARN marker
  3. A failed check command (tests, linter, type check) is remembered, with
     the files edited since, for the red-to-green rule below.

PostToolUse
  - A remembered check command that now succeeds is a `fix` candidate with
    fixes_signature set to the failure it resolved.
  - An Airlock marker in a result is a `danger` candidate.
  - Edit/Write calls are noted against any open red check. Nothing else
    touches disk.

An explicit "remember:" is the prompt hook's job (knowledge_prompt.py).

WHAT IT PROMISES
================

  - Advice only. It never blocks.
  - Fails open: any error, a dead store, a timeout -- exit 0, no output.
  - A hard wall-clock budget (BUDGET_S): a watchdog exits 0 when it runs out.
  - Redacted text only: summaries go through airlock/redact.py before they
    reach the store or the gate.

SWITCHES
========

  - The kit-wide kill switch wins: AIRLOCK_DISABLE=1 or ~/.config/airlock/disabled.
  - knowledge.config.enabled(): the state file ~/.config/airlock/knowledge.
"""
import json
import os
import re
import sys
import threading
import time

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HOOK_DIR)

# Same sys.path surgery as hooks/airlock.py: nothing in this directory may
# shadow a module in the packages next to it.
for _p in (HOOK_DIR, ""):
    while _p in sys.path:
        sys.path.remove(_p)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DISABLE_VARS = ("AIRLOCK_DISABLE", "PLUMBLINE_DISABLE", "JEV_GUARD_DISABLE")
STATE_NAME = "knowledge-capture.json"
BUDGET_S = 4.0
REPEAT_WINDOW_S = 900
RED_WINDOW_S = 900
DANGER_DEDUP_S = 600
MAX_EDITS = 5
SUMMARY_CHARS = 200
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

CHECK_RE = re.compile(
    r"\b(pytest|py\.test|unittest|tox|nox|mypy|ruff|flake8|pylint|eslint|tsc|jest|vitest|"
    r"npm (run )?test|yarn test|pnpm test|make (test|check|lint)|"
    r"cargo (test|build|check)|go (test|build|vet)|fdk validate)\b")
MARKER_RE = re.compile(r"(BLOCKED|NEEDS APPROVAL|WARN(ING)?)[^\n]{0,24}\(airlock[^\n]*", re.I)
PATHISH_RE = re.compile(r"[^\s'\"]*(/|\.py|\.js|\.ts|\.json|\.sh)[^\s'\"]*")


# ---- small helpers ---------------------------------------------------------

def _disabled():
    from airlock import paths
    if any(os.environ.get(v) == "1" for v in DISABLE_VARS):
        return True
    return os.path.exists(str(paths.config_dir() / "disabled"))


def _arm_watchdog(budget_s):
    """Exit 0 when the budget runs out, whatever the main thread is doing."""
    def _bail():
        try:
            sys.stdout.flush()
        except Exception:
            pass
        os._exit(0)
    timer = threading.Timer(budget_s, _bail)
    timer.daemon = True
    timer.start()
    return timer


def _redact(text):
    from airlock import redact
    return redact.redact(" ".join(str(text or "").split()))


def is_check_command(tool_name, tool_input):
    ti = tool_input if isinstance(tool_input, dict) else {}
    return tool_name == "Bash" and bool(CHECK_RE.search(str(ti.get("command") or "")))


def _command_key(tool_input):
    return " ".join(str((tool_input or {}).get("command") or "").split())[:SUMMARY_CHARS]


def _call_summary(tool_name, tool_input):
    ti = tool_input if isinstance(tool_input, dict) else {}
    for key in ("command", "file_path", "pattern", "url", "query", "description"):
        if ti.get(key):
            return _redact("%s: %s" % (key, ti.get(key)))[:SUMMARY_CHARS]
    return _redact(", ".join("%s=%s" % (k, str(v)[:60]) for k, v in list(ti.items())[:4]))[:SUMMARY_CHARS]


def entities_for(tool_name, tool_input):
    """Up to four anchor names for the call: a file, or a command word plus paths."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    out = []
    for key in ("file_path", "path", "notebook_path"):
        if ti.get(key):
            out.append(str(ti[key]))
    cmd = str(ti.get("command") or "").strip()
    if cmd:
        words = cmd.split()
        out.append(os.path.basename(words[0]) if words else cmd)
        out.extend(m.group(0) for m in PATHISH_RE.finditer(cmd))
    seen, ents = set(), []
    for e in out:
        e = _redact(e)[:120]
        if e and e not in seen:
            seen.add(e)
            ents.append(e)
    return tuple(ents[:4])


def _scopes(cwd):
    try:
        from knowledge import store
        return list(store.scopes_for(cwd or os.getcwd()))
    except Exception:
        return ["machine", "global"]


def _marker_line(*texts):
    for t in texts:
        m = MARKER_RE.search(str(t or ""))
        if m:
            return _redact(m.group(0))[:SUMMARY_CHARS]
    return ""


# ---- state: one small whole-file-locked json -------------------------------

def _with_state(fn):
    """Run fn(data) under an exclusive lock, save, return its result. None on error."""
    from airlock import paths, platform_compat
    try:
        state_dir = paths.state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        platform_compat.restrict_path(state_dir, 0o700)
        fd = os.open(str(paths.state_file(STATE_NAME)),
                     os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
    except Exception:
        return None
    try:
        platform_compat.lock_file(fd, platform_compat.LOCK_EXCLUSIVE)
        try:
            raw = os.read(fd, 4 * 1024 * 1024)
            data = json.loads(raw.decode("utf-8")) if raw else {}
            data = data if isinstance(data, dict) else {}
        except Exception:
            data = {}
        _prune(data, time.time())
        result = fn(data)
        blob = json.dumps(data).encode("utf-8")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, blob)
        return result
    except Exception:
        return None
    finally:
        try:
            platform_compat.unlock_file(fd)
            os.close(fd)
        except Exception:
            pass


def _prune(data, now):
    def live(ts, window):
        return isinstance(ts, (int, float)) and now - ts <= window

    for sess in list(data):
        e = data[sess]
        if not isinstance(e, dict):
            del data[sess]
            continue
        e["sigs"] = {s: [t for t in ts if live(t, REPEAT_WINDOW_S)]
                     for s, ts in (e.get("sigs") or {}).items() if isinstance(ts, list)}
        e["sigs"] = {s: ts for s, ts in e["sigs"].items() if ts}
        e["red"] = {k: v for k, v in (e.get("red") or {}).items()
                    if isinstance(v, dict) and live(v.get("ts"), RED_WINDOW_S)}
        e["announced"] = {s: t for s, t in (e.get("announced") or {}).items() if live(t, FAIL_DEDUP())}
        e["danger"] = {s: t for s, t in (e.get("danger") or {}).items() if live(t, DANGER_DEDUP_S)}
        if not any(e[k] for k in ("sigs", "red", "announced", "danger")):
            del data[sess]


def FAIL_DEDUP():
    try:
        from knowledge import config
        return float(config.FAIL_DEDUP_SECONDS)
    except Exception:
        return 600.0


def _entry(data, session_id):
    return data.setdefault(session_id or "", {"sigs": {}, "red": {}, "announced": {}, "danger": {}})


# ---- fix recall ------------------------------------------------------------

def recall_lines(fixes, sig, line_budget):
    """At most line_budget lines: that the failure was seen, and the fix."""
    from knowledge import config
    n = int(getattr(config, "PREVIEW_CHARS", 70))
    fix = fixes[0]
    seen = "Seen this failure before (signature %s, fix #%s, confirmed %dx)." % (
        sig[:8], getattr(fix, "id", "?"), max(1, int(getattr(fix, "evidence", 1) or 1)))
    text = _redact(getattr(fix, "text", ""))
    preview = text if len(text) <= n else text[:n - 1] + "…"
    return [seen, "Fix that worked: %s" % preview][:line_budget]


def emit(lines, event):
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": event, "additionalContext": "\n".join(lines)}}))
    sys.stdout.flush()


# ---- candidates ------------------------------------------------------------

def _candidate(Candidate, bucket, text, tool, sig, exc, ents, scope, session_id, prov, fixes_sig=None):
    return Candidate(bucket_hint=bucket, text=text, tool=tool, signature=sig, exception_type=exc,
                     entities=ents, scope=scope, session_id=session_id or "",
                     provenance=prov, source="hook", fixes_signature=fixes_sig)


def on_failure(payload, ingest=None):
    """Handle one PostToolUseFailure payload. Returns (lines or [], ingest result or None)."""
    from knowledge import config, signature, store, types

    if payload.get("is_interrupt"):
        return [], None
    tool = str(payload.get("tool_name") or "")
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    error = str(payload.get("error") or "")
    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd")
    sig = signature.signature(tool, error)
    exc = signature.exception_type(error)
    scopes = _scopes(cwd)

    hit = []
    try:
        hit = list(store.fixes_for(sig, scopes))
    except Exception:
        hit = []

    marker = _marker_line(error)
    check_key = _command_key(ti) if is_check_command(tool, ti) else ""

    def txn(data):
        e = _entry(data, session_id)
        now = time.time()
        e["sigs"].setdefault(sig, []).append(now)
        count = len(e["sigs"][sig])
        say = False
        if hit and sig not in e["announced"]:
            e["announced"][sig] = now
            say = True
        danger_ok = False
        if marker:
            dsig = signature.signature(tool, marker)
            if dsig not in e["danger"]:
                e["danger"][dsig] = now
                danger_ok = True
        if check_key:
            prev = e["red"].get(check_key) or {}
            e["red"][check_key] = {"sig": sig, "ts": now, "edits": prev.get("edits") or []}
        return count, say, danger_ok

    res = _with_state(txn)
    count, say, danger_ok = res if res else (1, False, False)

    if hit:
        if say:
            return recall_lines(hit, sig, config.FAIL_LINES), None
        return [], None  # known fix, already said: nothing to learn, no Jev call

    summary = _call_summary(tool, ti)
    head = _redact(error)[:SUMMARY_CHARS]
    ents = entities_for(tool, ti)
    prov = "session %s: %s" % (session_id[:12], summary)
    cands = []
    if count >= 2:
        cands.append(_candidate(types.Candidate, "failure", "%s | %s" % (summary, head), tool, sig,
                                exc, ents, scopes[0], session_id, prov))
    if danger_ok:
        cands.append(_candidate(types.Candidate, "danger", marker, tool,
                                signature.signature(tool, marker), "", ents, scopes[0],
                                session_id, prov))
    results = []
    if cands:
        if ingest is None:
            from knowledge import ingest as _ingest
            ingest = _ingest.ingest
        for c in cands:
            results.append(ingest(c))
    return [], results or None


def on_success(payload, ingest=None):
    """Handle one PostToolUse payload. Returns the ingest results (list) or None."""
    from knowledge import signature, types

    tool = str(payload.get("tool_name") or "")
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    session_id = payload.get("session_id") or ""
    resp = payload.get("tool_response")
    resp_text = resp if isinstance(resp, str) else json.dumps(resp, default=str)[:4000] if resp else ""
    marker = _marker_line(resp_text)
    check_key = _command_key(ti) if is_check_command(tool, ti) else ""
    edited = str(ti.get("file_path") or ti.get("notebook_path") or "") if tool in EDIT_TOOLS else ""
    if not (marker or check_key or edited):
        return None

    def txn(data):
        e = data.get(session_id)
        now = time.time()
        out = {"flip": None, "danger": False}
        if marker:
            e = _entry(data, session_id)
            dsig = signature.signature(tool, marker)
            if dsig not in e["danger"]:
                e["danger"][dsig] = now
                out["danger"] = True
        if not isinstance(e, dict):
            return out
        if edited and e.get("red"):
            for v in e["red"].values():
                if edited not in v["edits"]:
                    v["edits"] = (v["edits"] + [edited])[-MAX_EDITS:]
        if check_key and check_key in (e.get("red") or {}):
            out["flip"] = e["red"].pop(check_key)
        return out

    res = _with_state(txn)
    if not res:
        return None
    scopes = _scopes(payload.get("cwd"))
    summary = _call_summary(tool, ti)
    ents = entities_for(tool, ti)
    prov = "session %s: %s" % (session_id[:12], summary)
    cands = []
    flip = res.get("flip")
    if flip:
        edits = [os.path.basename(p) for p in flip.get("edits") or []]
        text = "check passed after failure %s: %s" % (str(flip.get("sig"))[:8], summary)
        if edits:
            text += " | edited %s" % ", ".join(_redact(x) for x in edits)
        fents = tuple(dict.fromkeys(ents + tuple(_redact(p)[:120] for p in flip.get("edits") or [])))[:4]
        cands.append(_candidate(types.Candidate, "fix", text, tool, None, "", fents, scopes[0],
                                session_id, prov, fixes_sig=flip.get("sig")))
    if res.get("danger"):
        cands.append(_candidate(types.Candidate, "danger", marker, tool,
                                signature.signature(tool, marker), "", ents, scopes[0],
                                session_id, prov))
    results = []
    if cands:
        if ingest is None:
            from knowledge import ingest as _ingest
            ingest = _ingest.ingest
        for c in cands:
            results.append(ingest(c))
    return results or None


def drain_danger(ingest=None):
    """Turn the rows Airlock spooled at deny, ask and warn time into danger candidates."""
    from knowledge import danger, types
    rows = danger.drain()
    if not rows:
        return []
    if ingest is None:
        from knowledge import ingest as ingest_mod
        ingest = ingest_mod.ingest
    keys = [danger.dedup_key(r) for r in rows]

    def txn(data):
        now = time.time()
        fresh = []
        for row, key in zip(rows, keys):
            e = _entry(data, row.get("session_id") or "")
            if key in e["danger"]:
                continue
            e["danger"][key] = now
            fresh.append(row)
        return fresh

    fresh = _with_state(txn) or []
    results = []
    for row in fresh:
        text = "Airlock %s on %s: %s" % (row.get("action"), row.get("tool") or "a tool",
                                         row.get("reason", ""))
        if row.get("summary"):
            text += " | call: %s" % row["summary"]
        cand = _candidate(types.Candidate, "danger", text, row.get("tool", ""), None, "",
                          entities_for(row.get("tool", ""), {"command": row.get("summary", "")}),
                          _scopes(row.get("cwd") or None)[0], row.get("session_id", ""),
                          "airlock %s %s" % (row.get("action"), row.get("rule") or ""))
        try:
            results.append(ingest(cand))
        except Exception:
            pass
    return results


def handle(payload):
    """Dispatch on the event. Returns the lines to print (possibly [])."""
    try:
        drain_danger()
    except Exception:
        pass
    event = str(payload.get("hook_event_name") or "")
    if event == "PostToolUse" or (not event and "error" not in payload and "tool_response" in payload):
        on_success(payload)
        return [], "PostToolUse"
    lines, _ = on_failure(payload)
    return lines, "PostToolUseFailure"


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        if _disabled():
            return 0
        from knowledge import config
        if not config.enabled():
            return 0
        _arm_watchdog(BUDGET_S)
        lines, event = handle(payload)
        if lines:
            emit(lines, event)
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
