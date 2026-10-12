#!/usr/bin/env python3
"""Bump a mod's version when its runtime files changed: python3 tools/bump_mod.py [mod-dir]."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from jevkit import modversion  # noqa: E402


def main(argv):
    mod_dir = argv[1] if len(argv) > 1 else os.path.join(ROOT, "mods", "jev-stats")
    result = modversion.bump(mod_dir)
    if result is None:
        print("unchanged: %s is already at the stored hash" % mod_dir)
    else:
        print("bumped %s: %s -> %s" % (mod_dir, result[0], result[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
