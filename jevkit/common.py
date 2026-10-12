"""Shared helpers: dry-run-by-default writes with a timestamped backup."""
import datetime
import difflib
import json
import os
import shutil
import tempfile
from pathlib import Path

from airlock import paths

PROFILES = ("owner", "shared")


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def read_json(path, default=None):
    text = read_text(path)
    if text is None:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def profile():
    """'owner', 'shared' or None when no profile was recorded."""
    p = (read_json(paths.kit_config_dir() / "profile.json", {}) or {}).get("profile")
    return p if p in PROFILES else None


def _backup(path):
    backup = "%s.bak.%s" % (path, datetime.datetime.now().strftime("%Y%m%dT%H%M%S"))
    shutil.copy2(path, backup)
    return backup


def write_text(path, new, yes, out, mode=0o644):
    """Show the diff for `path`; write it (backed up, atomically) only with yes.
    Returns True when the file changed or would change."""
    path = str(path)
    old = read_text(path)
    if old == new:
        out.write("unchanged  %s\n" % path)
        return False
    diff = difflib.unified_diff((old or "").splitlines(True), new.splitlines(True),
                                "%s (now)" % path, "%s (new)" % path)
    out.write("".join(diff) or "(empty file)\n")
    if not new.endswith("\n"):
        out.write("\n")
    if not yes:
        out.write("dry run: nothing written. Add --yes to apply.\n")
        return True
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if old is not None:
        mode = os.stat(path).st_mode & 0o777
        out.write("backup     %s\n" % _backup(path))
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".jevkit.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    out.write("wrote      %s\n" % path)
    return True


def remove_file(path, yes, out):
    path = str(path)
    if not os.path.exists(path):
        out.write("unchanged  %s (absent)\n" % path)
        return False
    out.write("remove     %s\n" % path)
    if not yes:
        out.write("dry run: nothing removed. Add --yes to apply.\n")
        return True
    os.unlink(path)
    return True
