"""jev-kit status: one read-only picture of the install. Never prints the key."""
import json
import os
import sqlite3
import time

from airlock import keyfile, paths, pricing

from . import common, config

COMPACTION_ID = "fast-jev-compaction@fast-jev-compaction"
HOOK_MARK = "airlock/current/hooks/airlock.py"


def _release(home):
    link = os.path.join(home, "current")
    if not os.path.lexists(link):
        return {"current": None}
    real = os.path.realpath(link)
    return {"current": os.path.basename(real), "path": real,
            "releases": len([n for n in os.listdir(os.path.join(home, "releases"))
                             if not n.startswith(".")]) if os.path.isdir(os.path.join(home, "releases")) else 0}


def _wired(settings_files):
    out = {}
    for f in settings_files:
        text = common.read_text(f)
        out[f] = None if text is None else HOOK_MARK in text.replace("\\", "/")
    return out


def _plugins(claude_dir):
    installed = (common.read_json(os.path.join(claude_dir, "plugins", "installed_plugins.json"), {}) or {})
    out = {}
    for name, entries in (installed.get("plugins", installed) or {}).items():
        if isinstance(entries, list) and entries and isinstance(entries[0], dict):
            if "jev" in name:
                out[name] = entries[0].get("version")
    return out


def _compaction(settings):
    data = common.read_json(settings, {}) or {}
    cfg = ((data.get("pluginConfigs") or {}).get(COMPACTION_ID) or {}).get("options")
    return {"configured": cfg is not None, "options": cfg or {},
            "function_hooks": (data.get("env") or {}).get("CLAUDE_CODE_ENABLE_FUNCTION_HOOKS") == "1"}


def _metrics():
    db = str(paths.state_file("metrics.db"))
    if not os.path.exists(db):
        return None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=1)
        try:
            counts = {}
            for t in ("events", "compaction", "compaction_req", "jev_calls"):
                try:
                    counts[t] = conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                except sqlite3.Error:
                    counts[t] = None
            return counts
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def collect(claude_dir=None, settings_files=None):
    claude_dir = claude_dir or os.path.expanduser("~/.claude")
    settings = os.path.join(claude_dir, "settings.json")
    settings_files = settings_files or [settings]
    mode, mode_src = config.get("mode")
    cache = pricing.load()
    return {
        "release": _release(str(paths.install_home())),
        "mode": {"value": mode, "source": mode_src},
        "kill_switch": config.get("kill-switch")[0],
        "profile": common.profile(),
        "key_present": bool(keyfile.get_api_key()),
        "wired": _wired(settings_files),
        "plugins": _plugins(claude_dir),
        "compaction": _compaction(settings),
        "pricing_age_h": None if not cache else round((time.time() - cache["fetched_at"]) / 3600, 1),
        "metrics": _metrics(),
    }


def warnings(s):
    w = []
    if s["release"]["current"] is None:
        w.append("nothing deployed: run install/install.sh --guard")
    if s["kill_switch"] == "on":
        w.append("kill switch is ON: the guard judges nothing")
    if not s["key_present"]:
        w.append("no TYPESAFE_API_KEY found")
    if s["wired"] and not any(v for v in s["wired"].values()):
        w.append("the guard hook is not wired into settings.json")
    if s["profile"] == "owner" and not s["compaction"]["configured"]:
        w.append("owner profile but compaction is not installed")
    return w


def run(args, out, err):
    s = collect()
    s["warnings"] = warnings(s)
    if args.json:
        out.write(json.dumps(s, indent=2, sort_keys=True) + "\n")
        return 0
    rel = s["release"]
    out.write("release     %s%s\n" % (rel["current"] or "none", "" if not rel["current"] else "  (%d kept)" % rel["releases"]))
    out.write("mode        %s (%s)\n" % (s["mode"]["value"], s["mode"]["source"]))
    out.write("kill switch %s\n" % s["kill_switch"])
    out.write("profile     %s\n" % (s["profile"] or "not set"))
    out.write("api key     %s\n" % ("present" if s["key_present"] else "missing"))
    for f, v in s["wired"].items():
        out.write("wired       %s: %s\n" % (f, {True: "yes", False: "no", None: "file missing"}[v]))
    for name, ver in sorted(s["plugins"].items()):
        out.write("plugin      %s %s\n" % (name, ver))
    c = s["compaction"]
    out.write("compaction  %s\n" % ("configured: " + json.dumps(c["options"], sort_keys=True) if c["configured"] else "not configured"))
    out.write("pricing     %s\n" % ("no cache" if s["pricing_age_h"] is None else "cache %.1fh old" % s["pricing_age_h"]))
    out.write("metrics     %s\n" % ("none" if s["metrics"] is None else ", ".join("%s=%s" % kv for kv in s["metrics"].items())))
    for w in s["warnings"]:
        out.write("warning     %s\n" % w)
    return 0
