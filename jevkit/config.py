"""jev-kit config list|get|set|unset.

Keys: mode, kill-switch, r6, r11, rule.<ID>. Each is validated before anything
is written, and every write goes through common.write_text (diff, backup,
--yes).
"""
import json
import os

from airlock import mode as airlock_mode
from airlock import paths, rules

from . import common

SWITCH_RULES = {"r6": "R6-gui-or-browser", "r11": "R11-browse-via-jev"}
ON_ACTION = "deny"


class ConfigError(Exception):
    pass


def _rules_path():
    return rules.CONFIG_FILE  # the file load_action_overrides() reads


def _load_rules():
    data = common.read_json(_rules_path(), {})
    return data if isinstance(data, dict) else {}


def _rules_view(data):
    """The dict that holds the {rule id: action} pairs (flat or under "rules")."""
    return data["rules"] if isinstance(data.get("rules"), dict) else data


def _rule_id(key):
    if key in SWITCH_RULES:
        return SWITCH_RULES[key]
    if key.startswith("rule."):
        rid = key[len("rule."):]
        if rid in rules.RULES_BY_ID:
            return rid
        raise ConfigError("unknown rule %r; known: %s" % (rid, ", ".join(sorted(rules.RULES_BY_ID))))
    return None


def keys():
    return ["mode", "kill-switch", "r6", "r11"] + ["rule." + r for r in sorted(rules.RULES_BY_ID)]


def get(key):
    """(value, source) for a key."""
    if key == "mode":
        env = paths.env(*airlock_mode.MODE_ENV)
        if env in airlock_mode.VALID_MODES:
            return env, "env"
        return airlock_mode.resolve_mode(), "file" if os.path.exists(airlock_mode.MODE_FILE) else "default"
    if key == "kill-switch":
        if paths.env("AIRLOCK_DISABLE") == "1":
            return "on", "env"
        on = os.path.exists(str(paths.config_file("disabled")))
        return ("on" if on else "off"), "file"
    rid = _rule_id(key)
    if rid:
        overrides = rules.load_action_overrides()
        value = rules.effective_action(rules.RULES_BY_ID[rid], overrides)
        value = ("on" if value != "off" else "off") if key in SWITCH_RULES else value
        return value, "rules.json" if rid in overrides else "default"
    raise ConfigError("unknown key %r; known: %s" % (key, ", ".join(keys())))


def set_value(key, value, yes, out, force=False):
    if key == "mode":
        if value not in airlock_mode.VALID_MODES:
            raise ConfigError("mode must be one of: %s" % ", ".join(airlock_mode.VALID_MODES))
        if value == "enforce" and common.profile() == "shared" and not force:
            raise ConfigError("this machine is marked 'shared': enforce needs --i-own-this-machine")
        env = paths.env(*airlock_mode.MODE_ENV)
        if env in airlock_mode.VALID_MODES and env != value:
            out.write("warning: env override %s wins over the mode file\n" % env)
        return common.write_text(airlock_mode.MODE_FILE, value + "\n", yes, out, 0o600)
    if key == "kill-switch":
        if value not in ("on", "off"):
            raise ConfigError("kill-switch must be on or off")
        path = paths.config_file("disabled")
        return (common.write_text(path, "disabled by jev-kit\n", yes, out, 0o600) if value == "on"
                else common.remove_file(path, yes, out))
    rid = _rule_id(key)
    if rid:
        if key in SWITCH_RULES:
            if value not in ("on", "off"):
                raise ConfigError("%s must be on or off" % key)
            value = ON_ACTION if value == "on" else "off"
        elif value not in rules.VALID_ACTIONS:
            raise ConfigError("action must be one of: %s" % ", ".join(rules.VALID_ACTIONS))
        data = _load_rules()
        _rules_view(data)[rid] = value
        return common.write_text(_rules_path(), json.dumps(data, indent=2, sort_keys=True) + "\n", yes, out, 0o600)
    raise ConfigError("unknown key %r; known: %s" % (key, ", ".join(keys())))


def unset(key, yes, out):
    rid = _rule_id(key)
    if rid:
        data = _load_rules()
        _rules_view(data).pop(rid, None)
        return common.write_text(_rules_path(), json.dumps(data, indent=2, sort_keys=True) + "\n", yes, out, 0o600)
    if key == "kill-switch":
        return set_value(key, "off", yes, out)
    if key == "mode":
        return common.remove_file(airlock_mode.MODE_FILE, yes, out)
    raise ConfigError("unknown key %r; known: %s" % (key, ", ".join(keys())))


def run(args, out, err):
    try:
        if args.action == "list":
            for k in keys():
                value, source = get(k)
                out.write("%-34s %-8s (%s)\n" % (k, value, source))
        elif args.action == "get":
            value, source = get(args.key)
            out.write("%s\n" % value if not args.verbose else "%s (%s)\n" % (value, source))
        elif args.action == "set":
            set_value(args.key, args.value, args.yes, out, force=args.i_own_this_machine)
        elif args.action == "unset":
            unset(args.key, args.yes, out)
    except ConfigError as exc:
        err.write("jev-kit config: %s\n" % exc)
        return 2
    return 0
