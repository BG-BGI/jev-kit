#!/usr/bin/env python3
"""UserPromptSubmit hook: name at most one skill for this turn.

WHY THIS EXISTS
===============

An agent with a large skill roster picks a skill off an index of names and
truncated descriptions. It loads the wrong one, or loads one when nothing
fits, and every wrong load puts a SKILL.md of thousands of tokens into the
context for the rest of the session. TypeSafe's skill-suggestion cookbook
halves both mistakes with two Jev requests and one line of context. This hook
is that recipe, over the roster the session itself was shown (see
suggest/roster.py and suggest/suggest.py).

The roster and the index in the system prompt are never touched, so prompt
caching over them still holds. The one line goes in as additionalContext:

    <skill_relevance>
    Relevant to the current request: ros2. Ignore this if it does not fit
    what the user actually asked for.
    </skill_relevance>

WHAT IT PROMISES
================

  - Fail open. No key, a timeout, an error, an empty roster: exit 0, print
    nothing, and the turn goes ahead exactly as it would have.
  - Code first. A slash command, a `!` line, or a turn of fewer than four
    words is never sent anywhere (suggest.skip_reason).
  - What leaves the machine: the user's prompt, through airlock/redact.py and
    truncated, plus the roster's names and descriptions and, for three of
    them, the opening of their SKILL.md.
  - A budget: each request gets SUGGEST_TIMEOUT_S, two at most.

SWITCHES
========

  - The kit-wide kill switch wins: AIRLOCK_DISABLE=1 or ~/.config/airlock/disabled.
  - AIRLOCK_SKILL_SUGGEST, else the first word of ~/.config/airlock/skill-suggest:
      on      judge and emit (the default once the hook is wired)
      shadow  judge and log, emit nothing: read the log before trusting it
      off     do nothing at all
  - Every judged turn is a row in ~/.local/state/airlock/skill-suggest.jsonl.
"""
import datetime
import json
import os
import sys

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
MODE_ENV = "AIRLOCK_SKILL_SUGGEST"
MODE_FILE = "skill-suggest"
MODES = ("on", "shadow", "off")
DEFAULT_MODE = "on"
LOG_NAME = "skill-suggest.jsonl"
SUGGEST_TIMEOUT_S = 2.0
PROMPT_HEAD = 200


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


def judge(payload, ask=None):
    """(block or None, log row or None) for one UserPromptSubmit payload.
    Raises on anything unexpected; main() turns that into silence."""
    from airlock import client, keyfile, redact
    from suggest import roster, suggest

    prompt = str(payload.get("prompt") or "")
    skip = suggest.skip_reason(prompt)
    if skip:
        return None, None
    if ask is None:
        if not keyfile.get_api_key():
            return None, None
        ask = client.ask

    skills = roster.discover(payload.get("cwd"), transcript_path=payload.get("transcript_path"))
    clean = redact.redact_and_truncate_prompt(prompt)
    result = suggest.suggest(clean, skills, ask, timeout_s=SUGGEST_TIMEOUT_S)
    row = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "session_id": payload.get("session_id"),
        "cwd": payload.get("cwd"),
        "guard": "skill_suggest",
        "roster_size": len(skills),
        "prompt_head": clean[:PROMPT_HEAD],
    }
    row.update(result)
    if result.get("reason") == "no_roster":
        return None, row
    return suggest.context_block(result.get("skill")), row


def emit(block):
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": block,
        }
    }))


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
        block, row = judge(payload)
    except Exception as exc:
        block, row = None, {"guard": "skill_suggest", "session_id": payload.get("session_id"),
                            "error": str(exc)[:300]}
    if row is not None:
        from airlock import log, paths
        row["mode"] = mode
        row["emitted"] = bool(block) and mode == "on"
        log.append(row, log_file=paths.state_file(LOG_NAME))
    if block and mode == "on":
        emit(block)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
