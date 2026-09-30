#!/usr/bin/env python3
"""PostToolUseFailure hook: when failures pile up, say whether to keep going.

WHY THIS EXISTS
===============

The expensive failure mode of a coding agent is not one failed call. It is
the fifth retry of an approach that was never going to work, each one paying
for the whole context again. Claude handles a single failure well, so this
hook stays out of the way until a session has failed MIN_FAILURES times in
airlock/failure_state.py's window. Then one Jev request reads the run
(airlock/failure_verdict.py) and, when it is confident the answer is to
change approach, look first, or ask the person, says so in two lines of
additionalContext. Retry-with-a-fix and expected failures stay silent.

WHAT IT PROMISES
================

  - Advice only. It never blocks: the call has already failed.
  - Fail open: no key, a timeout, an error -- exit 0 and print nothing.
  - Code first: an interrupt (Esc) is ignored, and a first failure never
    reaches the network.
  - Each verdict is said at most once per 10 minutes per session.
  - What leaves the machine: for each recent failure, the tool name, a
    one-line summary of the call and the head of the error, all through
    airlock/redact.py.

SWITCHES
========

  - The kit-wide kill switch wins: AIRLOCK_DISABLE=1 or ~/.config/airlock/disabled.
  - AIRLOCK_FAILURE_VERDICT, else the first word of ~/.config/airlock/failure-verdict:
    on (the default once wired), shadow (judge and log, say nothing), off.
  - Every judged run is a row in ~/.local/state/airlock/failure-verdict.jsonl,
    with the last call and its error head, redacted, so a spoken verdict can
    be checked against what actually failed.
"""
import datetime
import json
import os
import sys

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HOOK_DIR)

for _p in (HOOK_DIR, ""):
    while _p in sys.path:
        sys.path.remove(_p)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DISABLE_VARS = ("AIRLOCK_DISABLE", "PLUMBLINE_DISABLE", "JEV_GUARD_DISABLE")
MODE_ENV = "AIRLOCK_FAILURE_VERDICT"
MODE_FILE = "failure-verdict"
MODES = ("on", "shadow", "off")
DEFAULT_MODE = "on"
LOG_NAME = "failure-verdict.jsonl"
MIN_FAILURES = 2
TIMEOUT_S = 2.0


def _disabled():
    from airlock import paths
    if any(os.environ.get(v) == "1" for v in DISABLE_VARS):
        return True
    return os.path.exists(str(paths.config_dir() / "disabled"))


def resolve_mode():
    from airlock import paths
    env = (os.environ.get(MODE_ENV) or "").strip()
    if env in MODES:
        return env
    try:
        with open(str(paths.config_file(MODE_FILE)), "r") as f:
            words = f.read().split()
        if words and words[0] in MODES:
            return words[0]
    except Exception:
        pass
    return DEFAULT_MODE


def judge(payload, mode, ask=None):
    """(advice text or None, log row or None) for one failure payload."""
    from airlock import client, failure_state, failure_verdict, keyfile

    if payload.get("is_interrupt"):
        return None, None
    session_id = payload.get("session_id")
    summary = failure_verdict.call_summary(payload.get("tool_name"), payload.get("tool_input"))
    failures = failure_state.record_failure(
        session_id, payload.get("tool_name"), summary, payload.get("error"))
    if len(failures) < MIN_FAILURES:
        return None, None
    if ask is None:
        if not keyfile.get_api_key():
            return None, None
        ask = client.ask

    result = failure_verdict.decide(failures, ask, timeout_s=TIMEOUT_S)
    row = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "session_id": session_id,
        "cwd": payload.get("cwd"),
        "guard": "failure_verdict",
        "tool_name": payload.get("tool_name"),
        "failures": len(failures),
        "last_call": summary,
        "last_error": failures[-1].get("error"),
        "mode": mode,
    }
    row.update(result)
    text = None
    if result["speak"] and mode == "on":
        if failure_state.claim_announcement(session_id, result["verdict"]):
            text = failure_verdict.advice(result["verdict"], len(failures), failures)
        else:
            row["suppressed"] = "said_recently"
    row["emitted"] = bool(text)
    return text, row


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        return 0
    if not isinstance(payload, dict) or _disabled():
        return 0
    mode = resolve_mode()
    if mode == "off":
        return 0
    try:
        text, row = judge(payload, mode)
    except Exception as exc:
        text, row = None, {"guard": "failure_verdict", "session_id": payload.get("session_id"),
                           "error": str(exc)[:300]}
    if row is not None:
        from airlock import log, paths
        log.append(row, log_file=paths.state_file(LOG_NAME))
    if text:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUseFailure", "additionalContext": text}}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
