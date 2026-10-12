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

    u = sub.add_parser("update", help="fast-forward from origin, redeploy, update plugins")
    u.add_argument("--repo", help="path to the jev-kit checkout (default: recorded, then cwd)")
    u.add_argument("--yes", action="store_true", help="apply (default is a dry run)")

    pl = sub.add_parser("plugins", help="list or update the jev-kit marketplace plugins")
    plsub = pl.add_subparsers(dest="action", required=True)
    plsub.add_parser("list")
    plu = plsub.add_parser("update")
    plu.add_argument("--yes", action="store_true", help="apply (default is a dry run)")

    pr = sub.add_parser("profile", help="record whether this machine is yours (owner) or shared")
    prsub = pr.add_subparsers(dest="action", required=True)
    prsub.add_parser("get")
    prs = prsub.add_parser("set")
    prs.add_argument("value", choices=("owner", "shared"))
    prs.add_argument("--yes", action="store_true", help="apply (default is a dry run)")

    co = sub.add_parser("compaction", help="install, configure, update or remove fast-jev-compaction")
    co.add_argument("--settings-file", help="settings.json to edit (default: $CLAUDE_CONFIG_DIR or ~/.claude)")
    cosub = co.add_subparsers(dest="action", required=True)
    cosub.add_parser("status")
    cg = cosub.add_parser("get")
    cg.add_argument("key", nargs="?")
    cs = cosub.add_parser("set")
    cs.add_argument("key")
    cs.add_argument("value")
    cu = cosub.add_parser("unset")
    cu.add_argument("key")
    ci = cosub.add_parser("install")
    ci.add_argument("--mode", choices=("tool", "session", "both"), default="tool")
    cosub.add_parser("update")
    cr = cosub.add_parser("remove")
    cr.add_argument("--purge", action="store_true", help="also delete downloaded releases")
    for sp in (cs, cu, ci, cosub.choices["update"], cr):
        sp.add_argument("--yes", action="store_true", help="apply (default is a dry run)")

    ins = sub.add_parser("install", help="run install/install.sh (dry run unless --yes); extra flags pass through")
    ins.add_argument("--repo", help="path to the jev-kit checkout (default: recorded, then cwd)")
    ins.add_argument("--profile", choices=("owner", "shared"), help="record whether this machine is yours or shared")
    ins.add_argument("--yes", action="store_true", help="apply (default is a dry run)")
    ins.add_argument("installer_args", nargs=argparse.REMAINDER, help="after --: flags for install.sh, e.g. --guard --wire FILE")

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
    if args.cmd == "update":
        from . import update
        return update.run(args, out, err)
    if args.cmd == "plugins":
        from . import plugins
        return plugins.run(args, out, err)
    if args.cmd == "install":
        from . import install
        return install.run(args, out, err)
    if args.cmd == "profile":
        from . import profile
        return profile.run(args, out, err)
    if args.cmd == "compaction":
        from . import compaction
        return compaction.run(args, out, err)
    build_parser().print_help(out)
    return 0
