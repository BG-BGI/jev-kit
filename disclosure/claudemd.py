#!/usr/bin/env python3
"""Slim the global CLAUDE.md down to a pointer, and wire the hook that
re-injects the full text where it belongs.

WHY THIS EXISTS
===============

~/.claude/CLAUDE.md goes into the system prompt of EVERY session in EVERY
repo. On this machine most of it is a Freshworks developer-toolkit section
(skill paths, publish rules, MCP deprecations) that only matters inside a
Freshworks app directory, yet it costs two to four thousand tokens per
session everywhere. A hook can add context but never remove it, so the fix
is at the file layer: `slim` swaps CLAUDE.md for a short pointer (the full
text backed up to CLAUDE-full.md, byte for byte), and the SessionStart hook
hooks/airlock_claudemd_inject.py injects the full text back -- only in
directories that look like Freshworks apps.

The user's own standing sections (output style, Jev decisions) apply to all
repos, so `slim` keeps everything from the first `# Output style` heading
down and drops only the toolkit block above it. No such heading means the
whole file is toolkit text, and only the pointer remains.

Everything is reversible (`restore`), refuses rather than clobbers an older
backup, and writes atomically (tmp + os.replace).

USAGE
=====

    python3 -m disclosure.claudemd <slim|restore|status|wire> [--home PATH]

  slim     back up CLAUDE.md to CLAUDE-full.md, replace it with the pointer
  restore  copy CLAUDE-full.md back over the slim CLAUDE.md
  status   say which state the file is in, with sizes and token estimates
  wire     add the SessionStart inject hook to ~/.claude/settings.json
"""
import argparse
import json
import os
import sys
from pathlib import Path

MARKER = "<!-- jev-kit:claudemd-slim -->"
TAIL_HEADING = "# Output style"
FULL_NAME = "CLAUDE-full.md"
RESTORE_CMD = "python3 -m disclosure.claudemd restore"
HOOK_TIMEOUT = 5

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "hooks" / "airlock_claudemd_inject.py"


def _claude_dir(home):
    return Path(home).expanduser() / ".claude"


def _atomic_write(path, text):
    """tmp + os.replace in the same directory, so a crash mid-write can
    never leave a half-written CLAUDE.md or settings.json behind."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _slim_text(original):
    lines = [
        MARKER,
        "",
        "The Freshworks developer-toolkit instructions that lived here were",
        "moved to ~/.claude/%s by jev-kit. The jev-kit SessionStart hook" % FULL_NAME,
        "injects the full text automatically in Freshworks app directories;",
        "everywhere else it stays out of the context.",
        "",
        "Restore the full file with: %s" % RESTORE_CMD,
        "",
    ]
    # Keep the user's standing sections: everything from the first
    # `# Output style` heading to the end of the file.
    tail = None
    for i, line in enumerate(original.splitlines()):
        if line.startswith(TAIL_HEADING):
            tail = "\n".join(original.splitlines()[i:])
            break
    text = "\n".join(lines)
    if tail:
        text += "\n" + tail
    if not text.endswith("\n"):
        text += "\n"
    return text


def cmd_slim(home):
    claude_dir = _claude_dir(home)
    claude_md = claude_dir / "CLAUDE.md"
    full_md = claude_dir / FULL_NAME
    if not claude_md.is_file():
        print("no CLAUDE.md at %s" % claude_md, file=sys.stderr)
        return 1
    original = claude_md.read_text(encoding="utf-8")
    if MARKER in original:
        print("already slim")
        return 0
    if full_md.is_file() and full_md.read_text(encoding="utf-8") != original:
        print("%s exists and differs from CLAUDE.md; refusing to overwrite "
              "an older backup. Reconcile or remove it first." % full_md,
              file=sys.stderr)
        return 1
    _atomic_write(full_md, original)
    _atomic_write(claude_md, _slim_text(original))
    print("slimmed %s (full text in %s)" % (claude_md, full_md))
    return 0


def cmd_restore(home):
    claude_dir = _claude_dir(home)
    claude_md = claude_dir / "CLAUDE.md"
    full_md = claude_dir / FULL_NAME
    if not full_md.is_file():
        print("no backup at %s; nothing to restore" % full_md, file=sys.stderr)
        return 1
    if not claude_md.is_file() or MARKER not in claude_md.read_text(encoding="utf-8"):
        print("CLAUDE.md does not carry the slim marker; refusing to "
              "overwrite a file this tool did not write", file=sys.stderr)
        return 1
    _atomic_write(claude_md, full_md.read_text(encoding="utf-8"))
    print("restored %s from %s (backup kept)" % (claude_md, full_md))
    return 0


def _describe(path):
    if not path.is_file():
        return "missing"
    n = len(path.read_text(encoding="utf-8"))
    return "%d bytes, ~%d tokens" % (n, n // 4)


def cmd_status(home):
    claude_dir = _claude_dir(home)
    claude_md = claude_dir / "CLAUDE.md"
    full_md = claude_dir / FULL_NAME
    if not claude_md.is_file():
        state = "missing"
    elif MARKER in claude_md.read_text(encoding="utf-8"):
        state = "slim"
    else:
        state = "full"
    print("CLAUDE.md: %s (%s)" % (state, _describe(claude_md)))
    print("%s: %s" % (FULL_NAME, _describe(full_md)))
    return 0


def _has_command(entries, needle):
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for h in entry.get("hooks") or []:
            cmd = h.get("command") if isinstance(h, dict) else None
            if isinstance(cmd, str) and needle in cmd:
                return True
    return False


def _matcher_block(entries, matcher):
    for entry in entries:
        if isinstance(entry, dict) and entry.get("matcher") == matcher:
            return entry
    return None


def cmd_wire(home):
    claude_dir = _claude_dir(home)
    settings = claude_dir / "settings.json"
    if not HOOK_PATH.is_file():
        print("no hook at %s; refusing to wire an entry that would fail at "
              "every session start" % HOOK_PATH, file=sys.stderr)
        return 1
    command = "python3 %s" % HOOK_PATH

    data = {}
    if settings.is_file():
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except Exception as exc:
            print("%s: not valid JSON (%s), refusing to touch it"
                  % (settings, exc), file=sys.stderr)
            return 1
    if not isinstance(data, dict):
        print("%s: top level is not an object, refusing to touch it"
              % settings, file=sys.stderr)
        return 1

    entries = data.setdefault("hooks", {}).setdefault("SessionStart", [])
    # Idempotence is keyed on the script path, not the whole command, so a
    # rewire with a different interpreter prefix still counts as present.
    if _has_command(entries, str(HOOK_PATH)):
        print("%s: already wired" % settings)
        return 0

    hook = {"type": "command", "command": command, "timeout": HOOK_TIMEOUT}
    existing = _matcher_block(entries, "*")
    if existing is not None:
        existing.setdefault("hooks", []).append(hook)
    else:
        entries.append({"matcher": "*", "hooks": [hook]})

    claude_dir.mkdir(parents=True, exist_ok=True)
    bak = settings.with_name(settings.name + ".bak")
    if settings.is_file() and not bak.exists():
        bak.write_text(settings.read_text(encoding="utf-8"), encoding="utf-8")
    _atomic_write(settings, json.dumps(data, indent=2) + "\n")
    print('%s: added SessionStart hook "%s"' % (settings, command))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="disclosure.claudemd")
    parser.add_argument("action", choices=("slim", "restore", "status", "wire"))
    parser.add_argument("--home", default=None, help="override $HOME (tests)")
    args = parser.parse_args(argv)
    home = args.home or str(Path.home())
    return {"slim": cmd_slim, "restore": cmd_restore,
            "status": cmd_status, "wire": cmd_wire}[args.action](home)


if __name__ == "__main__":
    sys.exit(main())
