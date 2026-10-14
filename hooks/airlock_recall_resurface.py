#!/usr/bin/env python3
"""SessionStart hook: after /compact or a resume, say which recall refs exist.

WHY THIS EXISTS
===============

fast-jev-compaction cuts the middle out of a long tool result and leaves an
omission note naming a ref (`recall ref=r_1853eade range=21-100`). The model
fetches the text back with the plugin's `recall` tool. The store
(airlock/recall.py) keeps rows for days, but the NOTE lives only in the
context window. /compact summarises the window away and a resume starts from a
summary, so afterwards the text is still on disk and nothing says it is there.

This hook is the missing pointer. On `compact` and `resume` only, it adds one
short block listing the refs stored in the last few hours, each with its tool,
line range, age and a redacted one-line preview, and says how to fetch one.

WHAT IT PROMISES
================

  - Local reads only: one SQLite query. No network, stdlib plus airlock.
  - Silent on `startup` and `clear`, when nothing was lost from context, and
    when the store is empty or missing.
  - Previews go through airlock/redact.py and are cut to 70 characters; the
    whole block is capped, so it costs a few hundred tokens at most.
  - Fails open: any error exits 0 with no output.

The store does not record which Claude session wrote a row, so the list is
"recent on this machine", not "this session's". Two sessions running at once
can see each other's refs.
"""
import json
import os
import sys

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HOOK_DIR)

# Same sys.path surgery as hooks/airlock.py: this file sits beside
# `airlock.py`, so a bare `import airlock` would find that file, not the package.
for _p in (HOOK_DIR, ""):
    while _p in sys.path:
        sys.path.remove(_p)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

SOURCES = ("compact", "resume")
MAX_CHARS = 1800


def _age(minutes):
    if minutes < 60:
        return "%d min ago" % minutes
    return "%dh ago" % (minutes // 60)


def build(refs):
    """The context block for `refs` (dicts from recall.recent), or ''."""
    if not refs:
        return ""
    lines = [
        "Earlier tool output was cut from context and stored locally. Fetch any of it with the "
        "recall tool (mcp__fast-jev-compaction__recall: ref, optional range like 21-60):",
    ]
    for r in refs:
        lines.append('%s  %s  lines %d-%d  %s  "%s"' % (
            r["ref"], r["tool"], r["start"], r["end"], _age(r["age_min"]), r["preview"]))
    body = "\n".join(lines)
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS].rsplit("\n", 1)[0]
    return "<recall-refs>\n%s\n</recall-refs>" % body


def main():
    payload = json.loads(sys.stdin.read())
    if not isinstance(payload, dict) or payload.get("source") not in SOURCES:
        return 0
    from airlock import recall
    text = build(recall.recent())
    if text:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": text}}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
