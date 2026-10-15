#!/usr/bin/env python3
"""SessionStart hook: tell the session what the experience graph already knows.

WHY THIS EXISTS
===============

A fresh session knows nothing about the standing rules the human has laid
down or the failures this repo keeps producing. This hook injects them once,
at the start, under a pinned budget (docs/experience-graph.md, Retrieval).

WHAT IT INJECTS
===============

  - Every `constraint` node. They are law and there should be few; past
    CONSTRAINT_WARN the block says the list smells.
  - Then at most SESSION_NODES other nodes that match this session's scopes
    (this repo, this machine, global), best first.
  - One line each: `- [bucket #id] preview`. Previews are redacted and cut to
    PREVIEW_CHARS; `python3 -m knowledge.store show ID` prints the whole node.

WHAT IT PROMISES
================

  - Local reads only: one SQLite query set. No network, no Jev call.
  - Silent when the graph is empty, off, or missing.
  - Fails open: any error, or running out of BUDGET_S, exits 0 with no output.

SWITCHES
========

  - The kit-wide kill switch wins: AIRLOCK_DISABLE=1 or ~/.config/airlock/disabled.
  - knowledge.config.enabled(): the state file ~/.config/airlock/knowledge.
"""
import json
import os
import sys
import threading

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HOOK_DIR)

# Same sys.path surgery as hooks/airlock.py.
for _p in (HOOK_DIR, ""):
    while _p in sys.path:
        sys.path.remove(_p)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DISABLE_VARS = ("AIRLOCK_DISABLE", "PLUMBLINE_DISABLE", "JEV_GUARD_DISABLE")
BUDGET_S = 2.0


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


def line(node, n):
    """One line for a node, preview redacted and cut to n characters."""
    from airlock import redact
    text = redact.redact(" ".join(str(node.text or "").split()))
    if len(text) > n:
        text = text[:n - 1] + "…"
    return "- [%s #%s] %s" % (node.bucket, node.id, text)


def build(constraints, nodes, config):
    """The context block, or '' when there is nothing to say."""
    n = int(config.PREVIEW_CHARS)
    cons = list(constraints)
    ids = {c.id for c in cons}
    rest = [x for x in nodes if x.id not in ids and x.bucket != "constraint"][:int(config.SESSION_NODES)]
    if not cons and not rest:
        return ""
    out = ["Experience graph (local, advice only):"]
    if cons:
        out.append("Standing rules from the human:")
        out.extend(line(c, n) for c in cons)
        if len(cons) > int(config.CONSTRAINT_WARN):
            out.append("Warning: %d standing rules is too many; review with "
                       "`python3 -m knowledge.store constraints` and drop stale ones." % len(cons))
    if rest:
        out.append("Seen before here:")
        out.extend(line(x, n) for x in rest)
    return "<experience-graph>\n%s\n</experience-graph>" % "\n".join(out)


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
        from knowledge import config, store
        if not config.enabled():
            return 0
        _arm_watchdog(BUDGET_S)
        scopes = store.scopes_for(payload.get("cwd") or os.getcwd())
        cons = list(store.constraints())
        # Ask for extra: constraints can sit in the ranking and are filtered out of it.
        nodes = store.top_for_scopes(scopes, int(config.SESSION_NODES) + len(cons))
        text = build(cons, nodes, config)
        if text:
            sys.stdout.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart", "additionalContext": text}}))
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
