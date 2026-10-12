"""jev-kit plugins: list, and update the jev-kit marketplace plugins. Dry run unless --yes."""
import subprocess

MARKETPLACE = "jev-kit"
PLUGINS = ("jev-stats@jev-kit",)


def commands():
    cmds = [["claude", "plugin", "marketplace", "update", MARKETPLACE]]
    cmds += [["claude", "plugin", "update", p] for p in PLUGINS]
    return cmds


def update_plugins(yes, out, err, runner=subprocess.call):
    ok = True
    for cmd in commands():
        out.write("run        %s\n" % " ".join(cmd))
        if not yes:
            continue
        try:
            rc = runner(cmd)
        except OSError as e:
            err.write("jev-kit: cannot run claude: %s\n" % e)
            return 1
        ok = ok and rc == 0
    if not yes:
        out.write("dry run: nothing run. Add --yes to apply.\n")
    elif ok:
        out.write("done. Run /reload-plugins in Claude Code (session-only; the CLI cannot do it).\n")
    return 0 if ok else 1


def run(args, out, err, runner=subprocess.call):
    if args.action == "list":
        try:
            return runner(["claude", "plugin", "list"])
        except OSError as e:
            err.write("jev-kit: cannot run claude: %s\n" % e)
            return 1
    return update_plugins(args.yes, out, err, runner)
