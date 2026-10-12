"""jev-kit profile: record whether this machine is yours ('owner') or someone else's ('shared')."""
import datetime
import json

from airlock import paths

from . import common


def path():
    return str(paths.kit_config_dir() / "profile.json")


def run(args, out, err):
    if args.action == "get":
        out.write("%s\n" % (common.profile() or "not set"))
        return 0
    data = common.read_json(path(), {}) or {}
    data["profile"] = args.value
    data["set_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    common.write_text(path(), json.dumps(data, indent=2) + "\n", args.yes, out)
    return 0
