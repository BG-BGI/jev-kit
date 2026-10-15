"""Constants and the on/off switch for the experience graph.

The switch is the file `~/.config/airlock/knowledge` (via `airlock.paths`).
Its text `off` turns the graph off. A missing file, or any other text, means
on. There is no shadow state: the component is on or it is off.
"""
from airlock import paths

SESSION_NODES = 5
PROMPT_NODES = 2
FAIL_LINES = 2
FAIL_DEDUP_SECONDS = 600
PREVIEW_CHARS = 70
CONSTRAINT_WARN = 10
MIN_PROMPT_CHARS = 40
TTL_DAYS = 14
MAX_MB = 50
GATE_TIMEOUT_S = 1.5

BUCKETS = ("failure", "fix", "pattern", "danger", "decision", "constraint", "doc", "env")
EDGE_KINDS = ("fixes", "caused-by", "supersedes", "contradicts", "evidence-for", "applies-to")

# A scope is "repo:<abs path>", "machine" or "global".
SCOPE_MACHINE = "machine"
SCOPE_GLOBAL = "global"

SWITCH_NAME = "knowledge"


def switch_path():
    return paths.config_file(SWITCH_NAME)


def enabled():
    """True unless the switch file says `off`. Never raises."""
    try:
        return switch_path().read_text(encoding="utf-8").strip().lower() != "off"
    except Exception:
        return True


def set_enabled(on):
    """Write the switch file. `on` removes the `off` marker by writing `on`."""
    p = switch_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("on\n" if on else "off\n", encoding="utf-8")
