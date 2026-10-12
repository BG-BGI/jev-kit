"""jev-kit command router."""
import argparse
import os
import subprocess
import sys

from airlock import paths

PASSTHROUGH = ("metrics", "recall", "pricing", "headless", "report")


def _passthrough(module, rest):
    """Run `python3 -m airlock.<module>` with the deployed release on PYTHONPATH."""
    env = dict(os.environ)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = here + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return subprocess.call([sys.executable, "-m", "airlock." + module] + rest, env=env, cwd=here)


def build_parser():
    p = argparse.ArgumentParser(prog="jev-kit", description="Install, configure and inspect jev-kit.")
    sub = p.add_subparsers(dest="cmd", metavar="command")

    s = sub.add_parser("status", help="show release, mode, wiring, plugins, compaction")
    s.add_argument("--json", action="store_true")

    c = sub.add_parser("config", help="list, get, set or unset a setting")
    csub = c.add_subparsers(dest="action", required=True)
    csub.add_parser("list")
    g = csub.add_parser("get")
    g.add_argument("key")
    g.add_argument("-v", "--verbose", action="store_true")
    st = csub.add_parser("set")
    st.add_argument("key")
    st.add_argument("value")
    st.add_argument("--i-own-this-machine", action="store_true", help="allow enforce on a 'shared' machine")
    us = csub.add_parser("unset")
    us.add_argument("key")
    for sp in (st, us):
        sp.add_argument("--yes", action="store_true", help="apply (default is a dry run)")

    r = sub.add_parser("rollback", help="point `current` at the previous release")
    r.add_argument("--settings", action="store_true", help="also restore the newest settings.json backup")
    r.add_argument("--settings-file", default="~/.claude/settings.json")
    r.add_argument("--yes", action="store_true", help="apply (default is a dry run)")

    for name in PASSTHROUGH:
        sub.add_parser(name, help="run python3 -m airlock.%s from anywhere" % name, add_help=False)
    return p


def main(argv=None, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in PASSTHROUGH:
        return _passthrough(argv[0], argv[1:])
    args = build_parser().parse_args(argv)
    if args.cmd == "status":
        from . import status
        return status.run(args, out, err)
    if args.cmd == "config":
        from . import config
        return config.run(args, out, err)
    if args.cmd == "rollback":
        from . import rollback
        return rollback.run(args, out, err)
    build_parser().print_help(out)
    return 0
