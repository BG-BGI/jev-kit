"""Content hash and version bump for a Claude Code mod.

Claude Code caches a marketplace plugin by name and version, so an edit that
keeps the version never reaches `/plugin update`. The hash of a mod's runtime
files is stored beside it; a test fails when the files change without a bump.
"""
import hashlib
import json
import os

HASH_FILE = ".content-hash"
MANIFEST = os.path.join(".claude-plugin", "plugin.json")


def _runtime_files(mod_dir):
    for root, dirs, files in os.walk(mod_dir):
        dirs[:] = sorted(d for d in dirs if d != "node_modules")
        for name in sorted(files):
            rel = os.path.relpath(os.path.join(root, name), mod_dir)
            if rel in (HASH_FILE, MANIFEST) or name.endswith(".test.ts"):
                continue
            yield rel


def content_hash(mod_dir):
    h = hashlib.sha256()
    for rel in _runtime_files(mod_dir):
        h.update(rel.encode() + b"\0")
        with open(os.path.join(mod_dir, rel), "rb") as f:
            h.update(f.read())
        h.update(b"\0")
    return h.hexdigest()


def stored_hash(mod_dir):
    try:
        with open(os.path.join(mod_dir, HASH_FILE), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def bump_patch(version):
    major, minor, patch = (int(p) for p in version.split("."))
    return "%d.%d.%d" % (major, minor, patch + 1)


def bump(mod_dir):
    """Bump the patch version and store the new hash. Returns (old, new) or None if current."""
    digest = content_hash(mod_dir)
    if stored_hash(mod_dir) == digest:
        return None
    manifest = os.path.join(mod_dir, MANIFEST)
    with open(manifest, encoding="utf-8") as f:
        data = json.load(f)
    old = data["version"]
    data["version"] = bump_patch(old)
    with open(manifest, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    with open(os.path.join(mod_dir, HASH_FILE), "w", encoding="utf-8") as f:
        f.write(digest + "\n")
    return old, data["version"]
