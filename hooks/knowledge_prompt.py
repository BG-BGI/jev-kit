#!/usr/bin/env python3
"""UserPromptSubmit hook: standing rules in, relevant nodes out.

WHY THIS EXISTS
===============

Three jobs at the prompt, in this order, and the first that applies wins:

  (a) Explicit constraint. "remember: ...", "standing rule: ..." or "from now
      on ..." stores the rule verbatim as a `constraint` node with its
      provenance. No Jev call: a constraint bypasses the confidence gate, so
      only this explicit path may write one.
  (b) Correction shape. "don't", "never", "always", "stop doing" writes
      NOTHING. It offers once per session: "want this as a standing rule?
      say 'remember: ...'". A yes arrives as (a).
  (c) Retrieval. A prompt of at least MIN_PROMPT_CHARS goes to
      gate.pick_for_prompt over a few candidate nodes, which picks at most
      PROMPT_NODES. Silence is the default; a short prompt or an empty graph
      never reaches Jev.

WHAT IT PROMISES
================

  - Advice only. It never blocks a prompt.
  - Fails open: any error, a timeout, or running out of BUDGET_S exits 0
    with no output.
  - Text is redacted (airlock/redact.py) before it is stored or sent to the gate.
  - What leaves the machine: only path (c) calls Jev, with the redacted prompt
    and candidate node previews.

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

# Same sys.path surgery as hooks/airlock.py.
for _p in (HOOK_DIR, ""):
    while _p in sys.path:
        sys.path.remove(_p)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DISABLE_VARS = ("AIRLOCK_DISABLE", "PLUMBLINE_DISABLE", "JEV_GUARD_DISABLE")
STATE_NAME = "knowledge-prompt.json"
BUDGET_S = 3.5
OFFER_TTL_S = 24 * 3600
CANDIDATE_POOL = 8
MAX_PROVENANCE = 240

EXPLICIT_PREFIX_RE = re.compile(r"^\s*(?:remember|standing rule)\s*:\s*(?P<rule>.+)", re.I | re.S)
FROM_NOW_ON_RE = re.compile(r"\bfrom now on\b[,:]?\s*(?P<rule>[^\n]*)", re.I)
CORRECTION_RE = re.compile(
    r"\b(don'?t|do not|never|always|stop (?:doing|using|adding|making))\b", re.I)
MAX_CORRECTION_PROMPT = 400

OFFER = "Say 'remember: <rule>' and it is stored. Nothing was stored yet."


def _disabled():
    from airlock import paths
    if any(os.environ.get(v) == "1" for v in DISABLE_VARS):
        return True
    return os.path.exists(str(paths.config_dir() / "disabled"))


def _arm_watchdog(budget_s):
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


def explicit_rule(prompt):
    """The rule text when the prompt states a constraint explicitly, else ''."""
    m = EXPLICIT_PREFIX_RE.match(prompt)
    if m:
        return " ".join(m.group("rule").split())
    m = FROM_NOW_ON_RE.search(prompt)
    if m:
        rule = " ".join(m.group("rule").split())
        return rule or ""
    return ""


def is_correction(prompt):
    return len(prompt) <= MAX_CORRECTION_PROMPT and bool(CORRECTION_RE.search(prompt))


def claim_offer(session_id):
    """True the first time a session is offered the standing-rule question."""
    from airlock import paths, platform_compat
    try:
        state_dir = paths.state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        platform_compat.restrict_path(state_dir, 0o700)
        fd = os.open(str(paths.state_file(STATE_NAME)),
                     os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
    except Exception:
        return False
    try:
        platform_compat.lock_file(fd, platform_compat.LOCK_EXCLUSIVE)
        try:
            raw = os.read(fd, 1024 * 1024)
            data = json.loads(raw.decode("utf-8")) if raw else {}
            data = data if isinstance(data, dict) else {}
        except Exception:
            data = {}
        now = time.time()
        data = {k: v for k, v in data.items()
                if isinstance(v, (int, float)) and now - v <= OFFER_TTL_S}
        key = session_id or ""
        first = key not in data
        if first:
            data[key] = now
        blob = json.dumps(data).encode("utf-8")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, blob)
        return first
    except Exception:
        return False
    finally:
        try:
            platform_compat.unlock_file(fd)
            os.close(fd)
        except Exception:
            pass


def node_line(node, n):
    from airlock import redact
    text = redact.redact(" ".join(str(node.text or "").split()))
    if len(text) > n:
        text = text[:n - 1] + "…"
    return "- [%s #%s] %s" % (node.bucket, node.id, text)


def store_constraint(rule, payload, ingest=None):
    from airlock import redact
    from knowledge import types
    prompt_line = " ".join(str(payload.get("prompt") or "").split())
    prov = "session %s: %s" % (str(payload.get("session_id") or "")[:12],
                               redact.redact(prompt_line)[:MAX_PROVENANCE])
    cand = types.Candidate(bucket_hint="constraint", text=redact.redact(rule), scope="machine",
                           session_id=payload.get("session_id") or "", provenance=prov,
                           source="explicit")
    if ingest is None:
        from knowledge import ingest as _ingest
        ingest = _ingest.ingest
    return ingest(cand)


def retrieve(prompt, payload, config, pick=None):
    """<= PROMPT_NODES lines for the prompt, or []. Silence is the default."""
    from airlock import redact
    from knowledge import store
    scopes = store.scopes_for(payload.get("cwd") or os.getcwd())
    clean = redact.redact_and_truncate_prompt(prompt)
    pool, seen = [], set()
    try:
        near = store.nearest(clean, "", (), scopes, k=CANDIDATE_POOL)
    except Exception:
        near = []
    for node in list(near) + list(store.top_for_scopes(scopes, CANDIDATE_POOL)):
        if node.id not in seen and node.bucket != "constraint":
            seen.add(node.id)
            pool.append(node)
    if not pool:
        return []
    if pick is None:
        from knowledge import gate
        pick = gate.pick_for_prompt
    ids = list(pick(clean, pool) or [])[:int(config.PROMPT_NODES)]
    by_id = {x.id: x for x in pool}
    return [node_line(by_id[i], int(config.PREVIEW_CHARS)) for i in ids if i in by_id]


def handle(payload):
    """The additionalContext text to emit, or ''."""
    from knowledge import config
    prompt = str(payload.get("prompt") or "")
    rule = explicit_rule(prompt)
    if rule:
        res = store_constraint(rule, payload) or {}
        if res.get("status") == "stored":
            return "Stored standing rule #%s. Review with `python3 -m knowledge.store constraints`." % res.get("id")
        if res.get("status") == "duplicate":
            return "That standing rule is already stored."
        return ""
    if is_correction(prompt):
        if claim_offer(payload.get("session_id")):
            return "Want this as a standing rule? " + OFFER
        return ""
    if len(prompt.strip()) >= int(config.MIN_PROMPT_CHARS):
        lines = retrieve(prompt, payload, config)
        if lines:
            return "<experience-graph>\nRelevant from past work:\n%s\n</experience-graph>" % "\n".join(lines)
    return ""


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
        text = handle(payload)
        if text:
            sys.stdout.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit", "additionalContext": text}}))
            sys.stdout.flush()
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
