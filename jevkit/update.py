"""jev-kit update: fast-forward the checkout from origin, redeploy, update plugins.

Only `origin` is ever fetched. Dry run unless --yes.
"""
import os
import subprocess

from airlock import paths

from . import common, plugins


def _git(repo, *argv):
    p = subprocess.run(["git", "-C", repo] + list(argv), capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def state_path():
    return paths.kit_config_dir() / "state.json"


def find_repo(arg):
    """--repo, then the recorded checkout, then the current directory."""
    candidates = [arg, (common.read_json(state_path(), {}) or {}).get("repo"), os.getcwd()]
    for c in candidates:
        if c and os.path.isfile(os.path.join(os.path.expanduser(c), "install", "install.sh")):
            return os.path.realpath(os.path.expanduser(c))
    return None


def _record_repo(repo, yes, out):
    state = common.read_json(state_path(), {}) or {}
    if state.get("repo") == repo:
        return
    state["repo"] = repo
    import json
    common.write_text(str(state_path()), json.dumps(state, indent=2) + "\n", yes, out)


def run(args, out, err):
    repo = find_repo(args.repo)
    if not repo:
        err.write("jev-kit: no jev-kit checkout found. Run from the checkout or pass --repo PATH.\n")
        return 1
    rc, branch, _ = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        err.write("jev-kit: checkout is on '%s', not main. Switch to main first.\n" % branch)
        return 1
    rc, dirty, _ = _git(repo, "status", "--porcelain")
    if dirty:
        err.write("jev-kit: checkout has uncommitted changes. Commit or stash them first.\n")
        return 1
    out.write("repo       %s\n" % repo)
    out.write("run        git fetch origin\n")
    if args.yes:
        rc, _, e = _git(repo, "fetch", "origin")
        if rc:
            err.write("jev-kit: git fetch failed: %s\n" % e)
            return 1
    _, head, _ = _git(repo, "rev-parse", "--short", "HEAD")
    rc, behind, _ = _git(repo, "rev-list", "--count", "HEAD..origin/main")
    behind = int(behind) if rc == 0 and behind.isdigit() else 0
    out.write("checkout   %s, %d commit(s) behind origin/main%s\n"
              % (head, behind, "" if args.yes else " (as of last fetch)"))
    if behind:
        out.write("run        git merge --ff-only origin/main\n")
    out.write("run        install/install.sh --guard   (tests, deploy, shim)\n")
    out.write("run        jev-kit plugins update\n")
    if not args.yes:
        out.write("dry run: nothing changed. Add --yes to apply.\n")
        return 0
    if behind:
        rc, _, e = _git(repo, "merge", "--ff-only", "origin/main")
        if rc:
            err.write("jev-kit: fast-forward failed: %s\n" % e)
            return 1
    if subprocess.call(["bash", os.path.join(repo, "install", "install.sh"), "--guard"], cwd=repo) != 0:
        err.write("jev-kit: deploy failed; the running release is unchanged.\n")
        return 1
    _record_repo(repo, True, out)
    return plugins.update_plugins(True, out, err)
