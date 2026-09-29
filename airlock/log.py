"""Append-only shadow log writer.

One JSON line per judged call at ~/.local/state/airlock/shadow.jsonl
(%LOCALAPPDATA%\\airlock\\state\\shadow.jsonl on Windows). Directory mode 700,
file mode 600 on POSIX; on Windows the mode is meaningless and the per-user
ACL on %LOCALAPPDATA% is what keeps the file private -- see
airlock/platform_compat.py, which is also where the locking lives (flock on
POSIX, a byte-range lock on Windows) so concurrent hook processes -- one per
tool call -- never interleave a partial line.
"""
import json
import os

from . import paths, platform_compat

LOG_DIR = paths.state_dir()
LOG_FILE = LOG_DIR / "shadow.jsonl"


def append(entry, log_file=None):
    """Append one entry as a JSON line. Never raises -- a logging failure must
    never surface anywhere a caller could turn into session-visible output.
    `log_file` sends the row to another file in the same state directory
    (the skill-suggest hook keeps its rows out of the guard's log)."""
    try:
        target = log_file or LOG_FILE
        log_dir = target.parent if log_file else LOG_DIR
        log_dir.mkdir(parents=True, exist_ok=True)
        platform_compat.restrict_path(log_dir, 0o700)

        line = json.dumps(entry, default=str, ensure_ascii=False) + "\n"
        # O_BINARY exists only on Windows, where the default is TEXT mode and
        # every "\n" written would silently become "\r\n" -- the rows would
        # still parse, but the file would stop being byte-identical to the
        # Linux one and any offset arithmetic would drift. getattr() keeps the
        # flag a no-op (0) on POSIX, so the Linux call is unchanged.
        fd = os.open(str(target),
                     os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0),
                     0o600)
        try:
            platform_compat.lock_file(fd, platform_compat.LOCK_EXCLUSIVE)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                platform_compat.unlock_file(fd)
        finally:
            os.close(fd)
        platform_compat.restrict_path(str(target), 0o600)
    except Exception:
        return
