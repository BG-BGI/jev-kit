"""Per-project MCP disable lists, derived from what the project actually used.

Claude Code loads every configured MCP server's tool names and instructions
into the system prompt of every session in a project, whether or not any
session ever calls that server: 5-15k tokens of fixed overhead that no hook
can strip once the session has started. The transcripts under
~/.claude/projects already record every tool call each project made, so the
honest signal is sitting right there: a server no session in this project
has touched within the window pays its schema cost on every turn for
nothing.

This module reads those transcripts (never writes them), reports per
project which configured servers earned their keep, and on `apply` disables
the unused PROJECT-scope ones through the supported knob: the
`disabledMcpjsonServers` array in `<project>/.claude/settings.local.json`.
User-scope servers in ~/.claude.json apply to every project, so one is
never disabled automatically; `apply` only prints the suggestion and no
code path here ever edits ~/.claude.json. Everything we add is recorded in
a manifest (airlock's state dir) so `restore` removes exactly our
additions and nothing the user wrote by hand.

Like the rest of the kit, every failure fails open: a garbled transcript
line is skipped, and an unreadable settings file, .mcp.json or manifest
means "change nothing", never a crash.

    python3 -m disclosure.mcp report  [--days N] [--home PATH] [--project PATH]
    python3 -m disclosure.mcp apply   --project PATH [--days N] [--home PATH]
    python3 -m disclosure.mcp restore --project PATH [--home PATH]
"""
import argparse
import datetime
import json
import os
import shutil
import sys

from airlock import paths

MANIFEST_NAME = "mcp-disclosure.json"
DEFAULT_DAYS = 30


def _norm(p):
    return os.path.abspath(os.path.expanduser(p))


def _log_event(action, **fields):
    """One best-effort metrics breadcrumb, written only after a real state
    change (never on a no-op or refusal). Lazy import, everything swallowed:
    this module must keep working without airlock's metrics machinery, and a
    log/metrics failure must never fail the command (metrics.record_event's
    own philosophy)."""
    try:
        from airlock import log
        entry = {"ts": _now().isoformat(), "guard": "disclosure", "action": action}
        entry.update(fields)
        log.append(entry)
    except Exception:
        pass


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_ts(s):
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except Exception:
        return None


def _read_json(path):
    """(data, state) where state is 'ok', 'missing' or 'corrupt'."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh), "ok"
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return None, "missing"
    except Exception:
        return None, "corrupt"


def _write_json_atomic(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- usage scan

def scan_usage(home):
    """{normalized cwd: {server: newest use}} from every transcript line.

    A use is an assistant record whose message.content holds a tool_use named
    mcp__<server>__<tool>. The newest use is a tz-aware datetime, or None when
    the record carried no parseable timestamp -- an unknown time counts as
    recent, because the fail-open answer is "do not disable"."""
    usage = {}
    root = os.path.join(_norm(home), ".claude", "projects")
    try:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if name.endswith(".jsonl"):
                    _scan_file(os.path.join(dirpath, name), usage)
    except Exception:
        pass
    return usage


def _scan_file(path, usage):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    _scan_record(json.loads(line), usage)
                except Exception:
                    continue
    except Exception:
        pass


def _scan_record(rec, usage):
    if not isinstance(rec, dict) or rec.get("type") != "assistant":
        return
    cwd = rec.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return
    message = rec.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    ts = _parse_ts(rec.get("timestamp") or "")
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_use":
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.startswith("mcp__"):
            continue
        parts = name.split("__")
        if len(parts) < 2 or not parts[1]:
            continue
        seen = usage.setdefault(_norm(cwd), {})
        server = parts[1]
        if server not in seen:
            seen[server] = ts
        elif seen[server] is not None and (ts is None or ts > seen[server]):
            seen[server] = ts


def _within(last, cutoff):
    return True if last is None else last >= cutoff


# ----------------------------------------------------------- config discovery

def project_scope_servers(project):
    data, state = _read_json(os.path.join(_norm(project), ".mcp.json"))
    if state != "ok" or not isinstance(data, dict):
        return []
    servers = data.get("mcpServers")
    return sorted(servers) if isinstance(servers, dict) else []


def user_scope_servers(home):
    data, state = _read_json(os.path.join(_norm(home), ".claude.json"))
    if state != "ok" or not isinstance(data, dict):
        return []
    servers = data.get("mcpServers")
    return sorted(servers) if isinstance(servers, dict) else []


# ------------------------------------------------------------------- report

def _status(server, seen, cutoff):
    last = seen.get(server, "never")
    if last == "never":
        return False, "unused (never)"
    if _within(last, cutoff):
        when = "unknown" if last is None else last.date().isoformat()
        return True, "used-within-window (last %s)" % when
    return False, "unused (last used %s)" % last.date().isoformat()


def report(home, days, project=None, out=None):
    out = out or sys.stdout
    usage = scan_usage(home)
    cutoff = _now() - datetime.timedelta(days=days)
    projects = [_norm(project)] if project else sorted(usage)
    if not projects:
        print("no transcripts found under %s" % os.path.join(_norm(home), ".claude", "projects"), file=out)
        return 0
    user_servers = user_scope_servers(home)
    for p in projects:
        seen = usage.get(p, {})
        print("project: %s" % p, file=out)
        to_disable = []
        proj_servers = project_scope_servers(p)
        print("  project-scope (.mcp.json):%s" % ("" if proj_servers else " none"), file=out)
        for s in proj_servers:
            used, text = _status(s, seen, cutoff)
            print("    %-24s %s" % (s, text), file=out)
            if not used:
                to_disable.append(s)
        print("  user-scope (~/.claude.json):%s" % ("" if user_servers else " none"), file=out)
        for s in user_servers:
            _used, text = _status(s, seen, cutoff)
            print("    %-24s %s" % (s, text), file=out)
        if to_disable:
            print("  recommend: python3 -m disclosure.mcp apply --project %s  (disables: %s)"
                  % (p, ", ".join(to_disable)), file=out)
        else:
            print("  recommend: nothing to disable", file=out)
    return 0


# -------------------------------------------------------------------- apply

def apply(home, project, days, out=None):
    out = out or sys.stdout
    home, project = _norm(home), _norm(project)
    seen = scan_usage(home).get(project, {})
    cutoff = _now() - datetime.timedelta(days=days)

    # User-scope servers are global, so never written here: guidance only.
    for s in user_scope_servers(home):
        if not _status(s, seen, cutoff)[0]:
            print('user-scope server "%s" unused here within the last %d days: '
                  "consider `claude mcp disable %s` in this project or removing "
                  "it from ~/.claude.json" % (s, days, s), file=out)

    unused = [s for s in project_scope_servers(project)
              if not _status(s, seen, cutoff)[0]]

    settings_path = os.path.join(project, ".claude", "settings.local.json")
    settings, state = _read_json(settings_path)
    if state == "corrupt" or (state == "ok" and not isinstance(settings, dict)):
        print("%s is not a JSON object: changing nothing" % settings_path, file=out)
        return 0
    if state == "missing":
        settings = {}
    existing = settings.get("disabledMcpjsonServers")
    existing = [e for e in existing if isinstance(e, str)] if isinstance(existing, list) else []

    added = sorted(set(unused) - set(existing))
    if not added:
        print("nothing to disable", file=out)
        return 0

    manifest_path = str(paths.state_file(MANIFEST_NAME))
    manifest, mstate = _read_json(manifest_path)
    if mstate == "corrupt" or (mstate == "ok" and not isinstance(manifest, dict)):
        print("%s is not a JSON object: changing nothing" % manifest_path, file=out)
        return 0
    if mstate == "missing":
        manifest = {}

    if state == "ok" and not os.path.exists(settings_path + ".bak"):
        shutil.copy2(settings_path, settings_path + ".bak")
    settings["disabledMcpjsonServers"] = sorted(set(existing) | set(added))
    # A name in both lists is a conflict Claude Code should never be handed:
    # drop what we disable from enabledMcpjsonServers too, and remember it so
    # restore puts the explicit enable back.
    enabled = settings.get("enabledMcpjsonServers")
    dropped = []
    if isinstance(enabled, list):
        dropped = sorted(set(added) & {e for e in enabled if isinstance(e, str)})
        if dropped:
            settings["enabledMcpjsonServers"] = [e for e in enabled if e not in set(dropped)]
    _write_json_atomic(settings_path, settings)

    entry = manifest.get(project)
    prev = entry.get("added") if isinstance(entry, dict) and isinstance(entry.get("added"), list) else []
    prev_dropped = entry.get("dropped_enabled") if isinstance(entry, dict) and isinstance(entry.get("dropped_enabled"), list) else []
    manifest[project] = {"added": sorted(set(prev) | set(added)),
                         "dropped_enabled": sorted(set(prev_dropped) | set(dropped)),
                         "ts": _now().isoformat()}
    _write_json_atomic(manifest_path, manifest)
    print("disabled %d project-scope server(s) in %s: %s"
          % (len(added), settings_path, ", ".join(added)), file=out)
    _log_event("mcp_apply", project=project, disabled=len(added), servers=added)
    return 0


# ------------------------------------------------------------------ restore

def restore(home, project, out=None):
    out = out or sys.stdout
    project = _norm(project)
    manifest_path = str(paths.state_file(MANIFEST_NAME))
    manifest, mstate = _read_json(manifest_path)
    if mstate != "ok" or not isinstance(manifest, dict):
        print("no usable manifest at %s: nothing to restore" % manifest_path, file=out)
        return 0
    entry = manifest.get(project)
    added = entry.get("added") if isinstance(entry, dict) and isinstance(entry.get("added"), list) else []
    if not added:
        print("nothing to restore for %s" % project, file=out)
        return 0

    settings_path = os.path.join(project, ".claude", "settings.local.json")
    settings, state = _read_json(settings_path)
    if state == "corrupt" or (state == "ok" and not isinstance(settings, dict)):
        print("%s is not a JSON object: changing nothing" % settings_path, file=out)
        return 0
    if state == "ok":
        ours = set(added)
        existing = settings.get("disabledMcpjsonServers")
        changed = False
        if isinstance(existing, list):
            settings["disabledMcpjsonServers"] = [e for e in existing if e not in ours]
            changed = True
        dropped = entry.get("dropped_enabled") if isinstance(entry.get("dropped_enabled"), list) else []
        if dropped:
            enabled = settings.get("enabledMcpjsonServers")
            enabled = [e for e in enabled if isinstance(e, str)] if isinstance(enabled, list) else []
            settings["enabledMcpjsonServers"] = sorted(set(enabled) | set(dropped))
            changed = True
        if changed:
            _write_json_atomic(settings_path, settings)

    manifest.pop(project, None)
    _write_json_atomic(manifest_path, manifest)
    print("restored %s: removed %s" % (project, ", ".join(sorted(added))), file=out)
    _log_event("mcp_restore", project=project, servers=sorted(added))
    return 0


# ---------------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python3 -m disclosure.mcp",
        description="Report, disable and restore unused MCP servers per project.")
    ap.add_argument("command", choices=("report", "apply", "restore"))
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help="usage window in days (default %d)" % DEFAULT_DAYS)
    ap.add_argument("--home", default=None, help="override $HOME (default ~)")
    ap.add_argument("--project", default=None, help="project path")
    args = ap.parse_args(argv)
    home = args.home or os.path.expanduser("~")
    try:
        if args.command == "report":
            return report(home, args.days, project=args.project)
        if not args.project:
            print("--project is required for %s" % args.command, file=sys.stderr)
            return 2
        if args.command == "apply":
            return apply(home, args.project, args.days)
        return restore(home, args.project)
    except Exception as exc:  # fail open: a broken disclosure must never block
        print("mcp disclosure failed open (changed nothing): %s" % exc, file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
