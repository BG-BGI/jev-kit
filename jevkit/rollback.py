"""jev-kit rollback: point `current` at the previous release; optionally restore
the newest settings.json backup. Dry run unless --yes."""
import glob
import os

from airlock import paths

from . import common


def _releases(home):
    root = os.path.join(home, "releases")
    try:
        names = [n for n in os.listdir(root) if not n.startswith(".export-") and os.path.isdir(os.path.join(root, n))]
    except OSError:
        return []
    return sorted(names, key=lambda n: os.path.getmtime(os.path.join(root, n)), reverse=True)


def previous_release(home):
    """(current name, previous name or None); current is None when nothing is deployed."""
    link = os.path.join(home, "current")
    if not os.path.lexists(link):
        return None, None
    current = os.path.basename(os.path.realpath(link))
    names = _releases(home)
    if current not in names:
        return current, None
    i = names.index(current)
    return current, names[i + 1] if i + 1 < len(names) else None


def newest_backup(settings):
    found = sorted(glob.glob(glob.escape(str(settings)) + ".bak.*"))
    return found[-1] if found else None


def run(args, out, err):
    home = str(paths.install_home())
    current, previous = previous_release(home)
    if current is None:
        err.write("jev-kit rollback: nothing deployed under %s\n" % home)
        return 1
    if previous is None:
        err.write("jev-kit rollback: no release older than %s under %s/releases\n" % (current, home))
        return 1
    out.write("current %s -> %s\n" % (current, previous))
    if args.yes:
        link = os.path.join(home, "current")
        tmp = os.path.join(home, ".current.tmp.%d" % os.getpid())
        os.symlink(os.path.join(home, "releases", previous), tmp)
        os.replace(tmp, link)
        out.write("current -> %s\n" % os.path.realpath(link))
    else:
        out.write("dry run: nothing changed. Add --yes to apply.\n")
    if args.settings:
        settings = os.path.expanduser(args.settings_file)
        backup = newest_backup(settings)
        if backup is None:
            err.write("jev-kit rollback: no %s.bak.* to restore\n" % settings)
            return 1
        common.write_text(settings, common.read_text(backup) or "", args.yes, out)
    return 0
