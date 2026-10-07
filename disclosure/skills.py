"""Move long-unused user skills out of Claude Code's auto-discovery.

Claude Code reads every ~/.claude/skills/<dir>/SKILL.md description into each
session's system prompt, roughly a hundred tokens per skill, whether or not
the skill is ever invoked. The transcripts under ~/.claude/projects say which
skills actually get loaded: each load is a Skill tool_use record naming the
skill. A user skill with no load in the last --days days is cold, and `apply`
moves its directory to ~/.claude/skills-cold/<dir>, out of auto-discovery,
recording the move in skills-cold/manifest.json so `restore` can put it back.

A cold skill is not lost: suggest/roster.py reads skills-cold/ as an extra
source and the suggest hook injects the path to its SKILL.md when it fits a
turn, so the skill costs nothing per session yet stays one read away.

Only user skills move. Plugin skills (names with ":") are reported as an FYI
and left alone: their discovery goes through installed_plugins.json, not this
directory. A skill whose frontmatter says `disable-model-invocation: true`
never enters a system prompt, so moving it saves nothing and it is skipped.

Only the standard library. The scan tolerates unreadable files and
unparseable lines silently, the manifest is written to a temp file and
renamed, and a destination that already exists is skipped, never overwritten.

Usage:
  python3 -m disclosure.skills <report|plan|apply|restore> [name...]
                               [--days N] [--home PATH] [--projects PATH]
"""
import argparse
import datetime
import json
import os
import shutil
import sys

DEFAULT_DAYS = 30
MANIFEST = "manifest.json"


def _log_event(action, **fields):
    """One best-effort metrics breadcrumb, written only after a real state
    change (never on a no-op or refusal). The import is lazy and everything
    is swallowed: disclosure stays stdlib-only in spirit -- it keeps working
    on a machine without airlock -- and a log/metrics failure must never
    fail the command (metrics.record_event's own philosophy)."""
    try:
        from airlock import log
        entry = {"ts": _utcnow().isoformat(), "guard": "disclosure", "action": action}
        entry.update(fields)
        log.append(entry)
    except Exception:
        pass


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_ts(value):
    try:
        ts = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return ts


def scan_usage(projects_dir):
    """{skill name: {"count": n, "last": datetime or None}} from every Skill
    tool_use in every *.jsonl transcript under projects_dir. Never raises."""
    usage = {}
    try:
        for root, _dirs, files in os.walk(projects_dir):
            for fname in sorted(files):
                if fname.endswith(".jsonl"):
                    _scan_file(os.path.join(root, fname), usage)
    except Exception:
        pass
    return usage


def _scan_file(path, usage):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"Skill"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if not isinstance(record, dict):
                    continue
                message = record.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, list):
                    continue
                ts = _parse_ts(record.get("timestamp"))
                for item in content:
                    if not isinstance(item, dict) or item.get("type") != "tool_use":
                        continue
                    if item.get("name") != "Skill":
                        continue
                    inp = item.get("input")
                    name = inp.get("skill") if isinstance(inp, dict) else None
                    if not isinstance(name, str) or not name:
                        continue
                    entry = usage.setdefault(name, {"count": 0, "last": None})
                    entry["count"] += 1
                    if ts and (entry["last"] is None or ts > entry["last"]):
                        entry["last"] = ts
    except Exception:
        return


def user_skills(home):
    """[(name, directory)] for every model-invocable user skill, sorted."""
    out = []
    root = os.path.join(home, ".claude", "skills")
    try:
        entries = sorted(os.listdir(root))
    except Exception:
        return out
    for entry in entries:
        path = os.path.join(root, entry, "SKILL.md")
        if os.path.isfile(path) and not _model_invisible(path):
            out.append((entry, os.path.join(root, entry)))
    return out


def _model_invisible(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(8192)
    except Exception:
        return False
    if not text.startswith("---"):
        return False
    for line in text.split("\n---", 1)[0].splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "disable-model-invocation" and value.strip().lower() == "true":
            return True
    return False


def split(skills, usage, days, now=None):
    """(hot, cold), each [(name, last datetime or None)]. Cold means no Skill
    invocation within the last `days` days."""
    now = now or _utcnow()
    cutoff = now - datetime.timedelta(days=days)
    hot, cold = [], []
    for name, _directory in skills:
        last = (usage.get(name) or {}).get("last")
        (hot if last and last >= cutoff else cold).append((name, last))
    return hot, cold


def _cold_dir(home):
    return os.path.join(home, ".claude", "skills-cold")


def read_manifest(cold_dir):
    try:
        with open(os.path.join(cold_dir, MANIFEST), "r") as f:
            data = json.load(f)
        moved = data.get("moved") if isinstance(data, dict) else None
        return moved if isinstance(moved, dict) else {}
    except Exception:
        return {}


def write_manifest(cold_dir, moved):
    path = os.path.join(cold_dir, MANIFEST)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump({"moved": moved}, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _fmt(last):
    return last.isoformat() if last else "never"


def cmd_report(home, projects, days, out=None):
    out = out or sys.stdout
    usage = scan_usage(projects)
    hot, cold = split(user_skills(home), usage, days)
    cold_names = {n for n, _ in cold}
    print("user skills (hot = Skill call within %d days):" % days, file=out)
    rows = sorted(hot + cold)
    if not rows:
        print("  none", file=out)
    for name, last in rows:
        print("  %-40s %-32s %s" % (name, _fmt(last),
                                    "cold" if name in cold_names else "hot"), file=out)
    plugins = {n: u for n, u in usage.items() if ":" in n}
    if plugins:
        print("plugins (report-only, not moved):", file=out)
        for name in sorted(plugins):
            print("  %-40s %-32s %d calls" % (name, _fmt(plugins[name]["last"]),
                                              plugins[name]["count"]), file=out)


def cmd_plan(home, projects, days, out=None):
    out = out or sys.stdout
    _hot, cold = split(user_skills(home), scan_usage(projects), days)
    if not cold:
        print("nothing to move", file=out)
        return
    for name, last in cold:
        print("would move %s -> %s (last used %s)" % (
            os.path.join(home, ".claude", "skills", name),
            os.path.join(_cold_dir(home), name), _fmt(last)), file=out)


def cmd_apply(home, projects, days, out=None):
    out = out or sys.stdout
    _hot, cold = split(user_skills(home), scan_usage(projects), days)
    if not cold:
        print("nothing to move", file=out)
        return
    cold_dir = _cold_dir(home)
    os.makedirs(cold_dir, exist_ok=True)
    moved = read_manifest(cold_dir)
    done, moved_names = 0, []
    for name, _last in cold:
        src = os.path.join(home, ".claude", "skills", name)
        dst = os.path.join(cold_dir, name)
        if os.path.exists(dst):
            print("skip %s: %s already exists" % (name, dst), file=out)
            continue
        try:
            shutil.move(src, dst)
        except Exception as exc:
            print("skip %s: %s" % (name, exc), file=out)
            continue
        moved[name] = {"from": src, "ts": _utcnow().isoformat()}
        done += 1
        moved_names.append(name)
        print("moved %s -> %s" % (src, dst), file=out)
    write_manifest(cold_dir, moved)
    print("moved %d of %d cold skills" % (done, len(cold)), file=out)
    if done:
        _log_event("skills_apply", moved=done, skills=moved_names)


def cmd_restore(home, names=None, out=None):
    out = out or sys.stdout
    cold_dir = _cold_dir(home)
    moved = read_manifest(cold_dir)
    targets = [n for n in sorted(moved) if not names or n in names]
    if not targets:
        print("nothing to restore", file=out)
        return
    restored = []
    for name in targets:
        src = os.path.join(cold_dir, name)
        dst = (moved.get(name) or {}).get("from") or os.path.join(home, ".claude", "skills", name)
        if not os.path.isdir(src):
            print("prune %s: not in %s" % (name, cold_dir), file=out)
            moved.pop(name, None)
            continue
        if os.path.exists(dst):
            print("skip %s: %s already exists" % (name, dst), file=out)
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
        except Exception as exc:
            print("skip %s: %s" % (name, exc), file=out)
            continue
        moved.pop(name, None)
        restored.append(name)
        print("restored %s -> %s" % (name, dst), file=out)
    write_manifest(cold_dir, moved)
    if restored:
        _log_event("skills_restore", restored=len(restored), skills=restored)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python3 -m disclosure.skills",
        description="Move cold (long-unused) user skills out of Claude Code's "
                    "auto-discovery, and back.")
    parser.add_argument("command", choices=("report", "plan", "apply", "restore"))
    parser.add_argument("names", nargs="*",
                        help="restore only these skills (restore only)")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help="a skill unused this many days is cold (default %d)" % DEFAULT_DAYS)
    parser.add_argument("--home", default=None, help="home directory (default ~)")
    parser.add_argument("--projects", default=None,
                        help="transcripts root (default <home>/.claude/projects)")
    args = parser.parse_args(argv)
    home = args.home or os.path.expanduser("~")
    projects = args.projects or os.path.join(home, ".claude", "projects")
    if args.command == "report":
        cmd_report(home, projects, args.days)
    elif args.command == "plan":
        cmd_plan(home, projects, args.days)
    elif args.command == "apply":
        cmd_apply(home, projects, args.days)
    else:
        cmd_restore(home, args.names)
    return 0


if __name__ == "__main__":
    sys.exit(main())
