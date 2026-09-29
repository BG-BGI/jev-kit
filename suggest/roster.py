"""Find the skills a Claude Code session can load.

The authority is the session's own transcript: Claude Code records the skill
list it shows the model as a `skill_listing` attachment (`- name: description`
lines, plus a `names` array), and later listings add to it. That list holds
skills no file on disk describes -- built-ins like `init`, and org-served
ones like `anthropic-skills:pptx` -- so it is the roster whenever the hook
payload's `transcript_path` has one.

The files on disk fill in what the listing truncates or leaves out: the
full description and the opening of each SKILL.md, which the rerank reads.
With no listing, the disk alone is the roster. Three places, in this order,
first name wins:

  1. project skills:  <cwd>/.claude/skills/<dir>/SKILL.md
  2. user skills:     ~/.claude/skills/<dir>/SKILL.md
  3. plugin skills:   every installPath in ~/.claude/plugins/installed_plugins.json,
                      both <installPath>/skills/<dir>/SKILL.md and a SKILL.md at
                      the plugin root. A plugin skill is named "<plugin>:<skill>",
                      which is the name the Skill tool takes.

A skill whose frontmatter says `disable-model-invocation: true` is left out:
the model cannot load it, so suggesting it would be advice nobody can take.

Only the standard library. A SKILL.md that cannot be read or parsed is
skipped, never fatal: the roster is best-effort, and a hook built on it fails
open.
"""
import json
import os

BODY_CHARS = 700
DESCRIPTION_CHARS = 1000
PLUGINS_FILE = os.path.join(".claude", "plugins", "installed_plugins.json")


def _unquote(value):
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def parse_frontmatter(text):
    """(fields, body) from a SKILL.md. Handles the shapes skills actually
    use: `key: value`, quoted values, and `|` / `>` block scalars with an
    indented continuation. Not a YAML parser, and does not pretend to be."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n---", 1)
    if len(parts) < 2:
        return {}, text
    head = parts[0][3:]
    body = parts[1].split("\n", 1)[1] if "\n" in parts[1] else ""
    fields = {}
    lines = head.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line[0] in " \t#" or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if value[:1] in ("|", ">"):
            block = []
            while i < len(lines) and (not lines[i].strip() or lines[i][0] in " \t"):
                block.append(lines[i].strip())
                i += 1
            joiner = "\n" if value[:1] == "|" else " "
            fields[key.strip()] = joiner.join(b for b in block if b).strip()
        else:
            fields[key.strip()] = _unquote(value)
    return fields, body


def _read_skill(path, name):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:
        return None
    fields, body = parse_frontmatter(text)
    if str(fields.get("disable-model-invocation", "")).lower() == "true":
        return None
    return {
        "name": name,
        "description": " ".join(str(fields.get("description") or "").split())[:DESCRIPTION_CHARS],
        "body": " ".join(body.split())[:BODY_CHARS],
    }


def _skills_dir(root, prefix=""):
    out = []
    try:
        entries = sorted(os.listdir(root))
    except Exception:
        return out
    for entry in entries:
        path = os.path.join(root, entry, "SKILL.md")
        if os.path.isfile(path):
            out.append((path, prefix + entry))
    return out


def _plugin_skills(home):
    try:
        with open(os.path.join(home, PLUGINS_FILE), "r") as f:
            plugins = (json.load(f) or {}).get("plugins") or {}
    except Exception:
        return []
    out = []
    for key in sorted(plugins):
        plugin = key.split("@", 1)[0]
        installs = plugins[key] if isinstance(plugins[key], list) else []
        for install in installs[:1]:
            root = (install or {}).get("installPath") if isinstance(install, dict) else None
            if not root:
                continue
            top = os.path.join(root, "SKILL.md")
            if os.path.isfile(top):
                out.append((top, "%s:%s" % (plugin, plugin)))
            out.extend(_skills_dir(os.path.join(root, "skills"), plugin + ":"))
    return out


LISTING_TYPE = "skill_listing"
TRANSCRIPT_MAX_BYTES = 64 * 1024 * 1024


def _listing_entries(content):
    out = {}
    for chunk in ("\n" + (content or "")).split("\n- "):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, sep, desc = chunk.partition(": ")
        name = name.strip().lstrip("- ").strip()
        if name and " " not in name:
            out[name] = " ".join(desc.split()) if sep else ""
    return out


def from_transcript(path):
    """{name: listing description} from every skill_listing attachment in a
    transcript, later listings adding to earlier ones. {} when there is none,
    or the file is missing, unreadable or implausibly large. Never raises."""
    listing = {}
    try:
        if not path or os.path.getsize(path) > TRANSCRIPT_MAX_BYTES:
            return listing
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if LISTING_TYPE not in line:
                    continue
                try:
                    attachment = (json.loads(line) or {}).get("attachment") or {}
                except Exception:
                    continue
                if attachment.get("type") != LISTING_TYPE:
                    continue
                entries = _listing_entries(attachment.get("content"))
                for name in attachment.get("names") or []:
                    if isinstance(name, str) and name:
                        entries.setdefault(name, "")
                for name, desc in entries.items():
                    if desc or name not in listing:
                        listing[name] = desc
    except Exception:
        return {}
    return listing


def merge(listing, disk):
    """The listing's names, described by disk where disk knows the skill and
    by the listing otherwise. A disk skill the listing does not name is left
    out: the session cannot load it."""
    by_name = {s["name"]: s for s in disk}
    out = []
    for name in sorted(listing):
        known = by_name.get(name)
        if known:
            out.append(dict(known, description=known["description"] or listing[name][:DESCRIPTION_CHARS]))
        else:
            out.append({"name": name, "description": listing[name][:DESCRIPTION_CHARS], "body": ""})
    return out


def discover(cwd=None, home=None, transcript_path=None):
    """Every model-invocable skill, as [{name, description, body}], sorted by
    name. Never raises."""
    disk = discover_disk(cwd, home)
    listing = from_transcript(transcript_path)
    return merge(listing, disk) if listing else disk


def discover_disk(cwd=None, home=None):
    """The on-disk roster alone."""
    home = home or os.path.expanduser("~")
    candidates = []
    if cwd:
        candidates.extend(_skills_dir(os.path.join(cwd, ".claude", "skills")))
    candidates.extend(_skills_dir(os.path.join(home, ".claude", "skills")))
    candidates.extend(_plugin_skills(home))
    seen = {}
    for path, name in candidates:
        if name in seen:
            continue
        skill = _read_skill(path, name)
        if skill:
            seen[name] = skill
    return [seen[n] for n in sorted(seen)]
