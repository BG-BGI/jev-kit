"""jev-kit install: a front door over install/install.sh.

Without --yes it runs `install.sh --check-only`, which prints the plan and
installs nothing. With --yes it runs the installer, then records the machine
profile and the checkout path so `jev-kit update` finds it. It never turns
compaction on: that stays an explicit `jev-kit compaction install`.
"""
import datetime
import json
import os
import subprocess
import sys

from . import common, profile, update


def choose_profile(given, current, interactive, ask=input):
    """--profile wins, then the recorded one, then a prompt; None when it cannot be asked."""
    if given:
        return given
    if current:
        return current
    if not interactive:
        return None
    while True:
        answer = ask("Is this machine yours (owner) or someone else's (shared)? [owner/shared] ").strip().lower()
        if answer in common.PROFILES:
            return answer


def run(args, out, err, runner=subprocess.call, interactive=None):
    repo = update.find_repo(args.repo)
    if not repo:
        err.write("jev-kit: no jev-kit checkout found. Run from the checkout or pass --repo PATH.\n")
        return 1
    installer = os.path.join(repo, "install", "install.sh")
    extra = [a for a in args.installer_args if a != "--"]
    interactive = sys.stdin.isatty() if interactive is None else interactive
    chosen = choose_profile(args.profile, common.profile(), interactive and args.yes)
    if args.yes and chosen is None:
        err.write("jev-kit: pass --profile owner or --profile shared (no terminal to ask on).\n")
        return 1
    out.write("repo       %s\n" % repo)
    out.write("profile    %s\n" % (chosen or "not set (asked with --yes)"))
    if not args.yes:
        out.write("run        install/install.sh --check-only %s\n" % " ".join(extra))
        rc = runner(["bash", installer, "--check-only"] + extra, cwd=repo)
        out.write("dry run: nothing installed. Add --yes to apply.\n")
        return rc
    out.write("run        install/install.sh %s\n" % " ".join(extra))
    rc = runner(["bash", installer] + extra, cwd=repo)
    if rc != 0:
        err.write("jev-kit: the installer failed; nothing else was changed.\n")
        return rc
    if chosen and chosen != common.profile():
        data = common.read_json(profile.path(), {}) or {}
        data["profile"] = chosen
        data["set_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        common.write_text(profile.path(), json.dumps(data, indent=2) + "\n", True, out)
    update._record_repo(repo, True, out)
    out.write("done. Compaction is opt-in: jev-kit compaction install (read compaction/README.md first).\n")
    return 0
