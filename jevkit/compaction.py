"""jev-kit compaction: install, configure, update and remove fast-jev-compaction.

Everything that writes is a dry run unless --yes. The plugin's options live in
settings.json under pluginConfigs; they are checked against the userConfig
schema in the installed release, so a typo or a wrong type is rejected before
it is written. The API key is never stored here.
"""
import json
import os
import re
import shutil
import subprocess
import types

from airlock import paths

from . import common

PLUGIN_ID = "fast-jev-compaction@fast-jev-compaction"
MARKETPLACE = "fast-jev-compaction"
ALLOW_ENTRY = "mcp__fast-jev-compaction__recall"
# SessionStart hook that lists stored recall refs after /compact or a resume,
# when the omission notes that named them are gone from context.
RESURFACE_HOOK = "airlock_recall_resurface.py"
RESURFACE_MATCHER = "compact|resume"
RESURFACE_TIMEOUT = 5
HOOKS_FLAG = "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS"
MODES = ("tool", "session", "both")
EGRESS = (
    "Compaction sends data to TypeSafe: tool output (tool mode) or up to ~25,000 tokens of\n"
    "conversation state per request (session mode), after a pattern redactor. Pattern\n"
    "matching is not a guarantee: prose, names and paths still go out. Read compaction/README.md."
)


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_pin(root=None):
    pin = {}
    text = common.read_text(os.path.join(root or repo_root(), "compaction", "PIN")) or ""
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            pin[k.strip()] = v.strip()
    return pin


def compaction_home():
    return os.environ.get("COMPACTION_HOME") or os.path.expanduser("~/.local/share/jev-compaction")


def claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def settings_path(arg=None):
    return os.path.expanduser(arg) if arg else os.path.join(claude_dir(), "settings.json")


def release_dir(pin):
    return os.path.join(compaction_home(), "releases", pin["commit"][:12])


def schema(pin):
    """userConfig of the installed release, or None when it is not installed."""
    manifest = common.read_json(os.path.join(release_dir(pin), ".claude-plugin", "plugin.json"), None)
    return (manifest or {}).get("userConfig") if manifest else None


def installed_sha12():
    """The release the marketplace currently points at, from known_marketplaces.json."""
    known = common.read_json(os.path.join(claude_dir(), "plugins", "known_marketplaces.json"), {}) or {}
    m = re.search(r"releases/([0-9a-f]{12})", json.dumps(known.get(MARKETPLACE) or {}))
    return m.group(1) if m else None


def coerce(spec_type, raw):
    if spec_type == "boolean":
        low = str(raw).lower()
        if low in ("true", "on", "yes", "1"):
            return True
        if low in ("false", "off", "no", "0"):
            return False
        raise ValueError("expected true or false, got %r" % raw)
    if spec_type == "number":
        try:
            f = float(raw)
        except ValueError:
            raise ValueError("expected a number, got %r" % raw)
        return int(f) if f == int(f) else f
    return str(raw)


def validate(key, raw, sch):
    if sch is None:
        raise ValueError("compaction is not installed (no release found); run: jev-kit compaction install")
    if key not in sch:
        raise ValueError("unknown option %r; known: %s" % (key, ", ".join(sorted(sch))))
    spec = sch[key]
    if spec.get("sensitive"):
        raise ValueError("%s is a secret; it is never stored by this command. Put the key in the key file" % key)
    value = coerce(spec.get("type", "string"), raw)
    if key == "mode" and value not in MODES:
        raise ValueError("mode must be one of %s" % ", ".join(MODES))
    return value


def _load(path):
    return common.read_json(path, {}) or {}


def _dump(data):
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _options(data):
    return ((data.get("pluginConfigs") or {}).get(PLUGIN_ID) or {}).get("options") or {}


def resurface_command():
    """The deployed release's copy when there is one (it survives checkout
    moves and updates atomically), else this checkout's."""
    live = os.path.join(str(paths.install_home()), "current", "hooks", RESURFACE_HOOK)
    script = live if os.path.isfile(live) else os.path.join(repo_root(), "hooks", RESURFACE_HOOK)
    return "python3 %s" % script


def _resurface_entries(data):
    return [h for block in ((data.get("hooks") or {}).get("SessionStart") or [])
            for h in (block.get("hooks") or []) if RESURFACE_HOOK in (h.get("command") or "")]


def ensure_settings(data):
    """Function hooks flag, the recall tool permission and the ref-resurface
    SessionStart hook, each added if missing."""
    data.setdefault("env", {})[HOOKS_FLAG] = "1"
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    if ALLOW_ENTRY not in allow:
        allow.append(ALLOW_ENTRY)
    if not _resurface_entries(data):
        data.setdefault("hooks", {}).setdefault("SessionStart", []).append({
            "matcher": RESURFACE_MATCHER,
            "hooks": [{"type": "command", "command": resurface_command(), "timeout": RESURFACE_TIMEOUT}]})
    return data


def cmd_get(args, out, err):
    pin = read_pin()
    sch = schema(pin) or {}
    opts = _options(_load(settings_path(args.settings_file)))
    keys = [args.key] if args.key else sorted(k for k in set(sch) | set(opts) if not (sch.get(k) or {}).get("sensitive"))
    for k in keys:
        if sch and k not in sch:
            err.write("jev-kit: unknown option %r\n" % k)
            return 1
        if k in opts:
            out.write("%-18s %s\n" % (k, json.dumps(opts[k])))
        else:
            d = (sch.get(k) or {}).get("default")
            out.write("%-18s %s (default)\n" % (k, "unset" if d is None else json.dumps(d)))
    return 0


def cmd_set(args, out, err):
    path = settings_path(args.settings_file)
    try:
        value = validate(args.key, args.value, schema(read_pin()))
    except ValueError as e:
        err.write("jev-kit: %s\n" % e)
        return 1
    data = _load(path)
    data.setdefault("pluginConfigs", {}).setdefault(PLUGIN_ID, {}).setdefault("options", {})[args.key] = value
    common.write_text(path, _dump(data), args.yes, out)
    if args.yes:
        out.write("takes effect in the next session (or after /reload-plugins).\n")
    return 0


def cmd_unset(args, out, err):
    path = settings_path(args.settings_file)
    data = _load(path)
    opts = _options(data)
    if args.key not in opts:
        out.write("unchanged  %s is already unset\n" % args.key)
        return 0
    del opts[args.key]
    common.write_text(path, _dump(data), args.yes, out)
    return 0


def cmd_status(args, out, err):
    pin = read_pin()
    inst = installed_sha12()
    out.write("pin         %s\n" % pin.get("commit", "missing")[:12])
    out.write("installed   %s%s\n" % (inst or "no", "" if not inst or inst == pin.get("commit", "")[:12] else "  (behind the pin: jev-kit compaction update)"))
    data = _load(settings_path(args.settings_file))
    out.write("hooks flag  %s\n" % ("set" if (data.get("env") or {}).get(HOOKS_FLAG) == "1" else "missing"))
    out.write("resurface   %s\n" % ("wired" if _resurface_entries(_load(settings_path(args.settings_file))) else "not wired"))
    out.write("recall perm %s\n" % ("allowed" if ALLOW_ENTRY in ((data.get("permissions") or {}).get("allow") or []) else "not allowed"))
    return cmd_get(types.SimpleNamespace(key=None, settings_file=args.settings_file), out, err)


def cmd_install(args, out, err, runner=subprocess.call):
    pin = read_pin()
    if not pin.get("commit"):
        err.write("jev-kit: compaction/PIN is missing\n")
        return 1
    path = settings_path(args.settings_file)
    mode = args.mode
    out.write(EGRESS + "\n")
    out.write("pin         %s\n" % pin["commit"][:12])
    out.write("mode        %s\n" % mode)
    out.write("settings    %s: set %s=1, allow %s, and list stored refs after /compact or a resume (SessionStart hook)\n"
              % (path, HOOKS_FLAG, ALLOW_ENTRY))
    out.write("run        compaction/install.sh (clone the pin, add the marketplace, install the plugin)\n")
    if not args.yes:
        out.write("dry run: nothing changed. Add --yes to apply.\n")
        return 0
    common.write_text(path, _dump(ensure_settings(_load(path))), True, out)
    env = dict(os.environ, COMPACTION_MODE=mode)
    rc = runner(["bash", os.path.join(repo_root(), "compaction", "install.sh")], env=env)
    if rc == 0:
        out.write("done. Run /reload-plugins in Claude Code.\n")
    return rc


def _prune(keep, out, yes):
    root = os.path.join(compaction_home(), "releases")
    try:
        names = sorted(n for n in os.listdir(root) if n != keep and os.path.isdir(os.path.join(root, n)))
    except OSError:
        return
    for n in names:
        out.write("remove     %s\n" % os.path.join(root, n))
        if yes:
            shutil.rmtree(os.path.join(root, n), ignore_errors=True)


def cmd_update(args, out, err, runner=subprocess.call):
    pin = read_pin()
    want = pin.get("commit", "")[:12]
    have = installed_sha12()
    if not have:
        err.write("jev-kit: compaction is not installed; run: jev-kit compaction install\n")
        return 1
    if have == want:
        out.write("up to date  installed release %s matches the pin\n" % have)
        return 0
    path = settings_path(args.settings_file)
    mode = _options(_load(path)).get("mode", "tool")
    out.write("update      %s -> %s (options and mode %s are kept)\n" % (have, want, mode))
    out.write("run        claude plugin marketplace remove %s\n" % MARKETPLACE)
    out.write("run        compaction/install.sh\n")
    out.write("remove     old releases other than %s\n" % want)
    if not args.yes:
        out.write("dry run: nothing changed. Add --yes to apply.\n")
        return 0
    runner(["claude", "plugin", "marketplace", "remove", MARKETPLACE])
    rc = runner(["bash", os.path.join(repo_root(), "compaction", "install.sh")], env=dict(os.environ, COMPACTION_MODE=mode))
    if rc != 0:
        err.write("jev-kit: install failed; the old release directory was left in place.\n")
        return rc
    _prune(want, out, True)
    out.write("done. Run /reload-plugins in Claude Code.\n")
    return 0


def _drop_resurface(data):
    """Remove the resurface hook, and any SessionStart block or key it empties."""
    session = (data.get("hooks") or {}).get("SessionStart")
    if not session:
        return
    for block in session:
        block["hooks"] = [h for h in (block.get("hooks") or []) if RESURFACE_HOOK not in (h.get("command") or "")]
    data["hooks"]["SessionStart"] = [b for b in session if b.get("hooks")]
    if not data["hooks"]["SessionStart"]:
        del data["hooks"]["SessionStart"]
    if not data["hooks"]:
        del data["hooks"]


def cmd_remove(args, out, err, runner=subprocess.call):
    path = settings_path(args.settings_file)
    out.write("run        claude plugin uninstall %s\n" % PLUGIN_ID)
    out.write("run        claude plugin marketplace remove %s\n" % MARKETPLACE)
    out.write("settings    %s: drop pluginConfigs for the plugin, the recall permission and the ref-resurface hook\n" % path)
    out.write("kept        env %s (other mods may use function hooks)\n" % HOOKS_FLAG)
    if args.purge:
        out.write("remove     %s (all downloaded releases)\n" % os.path.join(compaction_home(), "releases"))
        out.write("note        stored cuts: run `jev-kit recall purge` to delete them\n")
    if not args.yes:
        out.write("dry run: nothing changed. Add --yes to apply.\n")
        return 0
    runner(["claude", "plugin", "uninstall", PLUGIN_ID])
    runner(["claude", "plugin", "marketplace", "remove", MARKETPLACE])
    data = _load(path)
    (data.get("pluginConfigs") or {}).pop(PLUGIN_ID, None)
    if not data.get("pluginConfigs") and "pluginConfigs" in data:
        del data["pluginConfigs"]
    allow = (data.get("permissions") or {}).get("allow")
    if allow and ALLOW_ENTRY in allow:
        allow.remove(ALLOW_ENTRY)
    _drop_resurface(data)
    common.write_text(path, _dump(data), True, out)
    if args.purge:
        shutil.rmtree(os.path.join(compaction_home(), "releases"), ignore_errors=True)
    return 0


def run(args, out, err):
    return {"get": cmd_get, "set": cmd_set, "unset": cmd_unset, "status": cmd_status, "install": cmd_install,
            "update": cmd_update, "remove": cmd_remove}[args.action](args, out, err)
