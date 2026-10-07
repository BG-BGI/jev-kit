#!/usr/bin/env python3
"""SessionStart hook: re-inject the slimmed CLAUDE.md where it applies.

WHY THIS EXISTS
===============

disclosure/claudemd.py `slim` moves the Freshworks developer-toolkit block
out of ~/.claude/CLAUDE.md (into CLAUDE-full.md) because it costs thousands
of tokens in every session everywhere while only mattering inside Freshworks
app directories. This hook is the other half of that bargain: at session
start, if the cwd (or a near ancestor) looks like a Freshworks app, the full
backed-up text goes back in as additionalContext. Anywhere else, the hook
says nothing and the session runs on the slim pointer alone.

WHAT IT PROMISES
================

  - Local reads only: three path probes per directory, four directories at
    most, one file read when they hit. No network, nothing beyond stdlib,
    milliseconds end to end.
  - Fail open: no backup file, unreadable payload, any exception at all
    means exit 0 with nothing printed. A broken inject must never break a
    session start -- the worst case is simply the slim CLAUDE.md, which is
    the state the user chose.

A Freshworks app directory is one with a manifest.json mentioning
"platform-version", a .fdk directory, or a config/iparams.json -- the same
artifacts the FDK itself creates or requires. cwd plus up to three ancestors
covers a session started in a subdirectory of the app. The home directory
itself is never treated as an app dir: the FDK CLI keeps a global ~/.fdk
state dir, and $HOME is an ancestor of every project, so counting it would
inject the text everywhere and undo the slimming.
"""
import json
import os
import sys
from pathlib import Path

ANCESTORS = 3

REPO_ROOT = str(Path(__file__).resolve().parent.parent)


def _log_inject(cwd, chars):
    """One best-effort metrics row, written only when context was emitted.
    One lazy import and one call, every exception swallowed: the hook's
    latency budget is milliseconds and its stdout/exit-code contract must
    never change because logging broke. The sys.path insert exists because
    Python runs this file as a script, so sys.path[0] is hooks/, not the
    repo root that holds the airlock package."""
    try:
        if REPO_ROOT not in sys.path:
            sys.path.insert(0, REPO_ROOT)
        from airlock import log
        log.append({"guard": "disclosure", "action": "claudemd_inject",
                    "cwd": cwd, "chars": chars})
    except Exception:
        pass


def _home():
    override = os.environ.get("AIRLOCK_HOME_OVERRIDE")
    return Path(override) if override else Path.home()


def _is_fw_dir(d):
    try:
        manifest = d / "manifest.json"
        if manifest.is_file() and "platform-version" in manifest.read_text(
                encoding="utf-8", errors="replace"):
            return True
        if (d / ".fdk").is_dir():
            return True
        if (d / "config" / "iparams.json").is_file():
            return True
    except Exception:
        pass
    return False


def main():
    payload = json.loads(sys.stdin.read())
    if not isinstance(payload, dict):
        return 0
    full_md = _home() / ".claude" / "CLAUDE-full.md"
    if not full_md.is_file():
        return 0
    cwd = Path(payload.get("cwd") or os.getcwd())
    dirs = [cwd]
    for _ in range(ANCESTORS):
        if dirs[-1].parent == dirs[-1]:
            break
        dirs.append(dirs[-1].parent)
    home = _home()
    if not any(_is_fw_dir(d) for d in dirs if d != home):
        return 0
    text = full_md.read_text(encoding="utf-8")
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "<fw-dev-toolkit-instructions>\n%s\n"
                                 "</fw-dev-toolkit-instructions>" % text,
        }
    }))
    _log_inject(str(cwd), len(text))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
