"""hooks/airlock_recall_resurface.py and its wiring in `jev-kit compaction`."""
import tests  # noqa: F401, I001 -- first import: isolates HOME and AIRLOCK_* (see tests/__init__.py)

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from airlock import recall
from jevkit import compaction

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "airlock_recall_resurface.py"


def _load():
    spec = importlib.util.spec_from_file_location("recall_resurface", str(HOOK))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load()


def _run(payload):
    return subprocess.run([sys.executable, str(HOOK)], input=payload, capture_output=True, text=True,
                          env=dict(os.environ))


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(self._wipe)
        self._wipe()

    @staticmethod
    def _wipe():
        try:
            os.remove(recall.db_path())
        except OSError:
            pass


class TestHook(StoreCase):
    def _seed(self):
        return recall.put({"session": "main", "tool": "Bash", "ranges": [
            {"start": 21, "end": 22, "text": "ping response received\nsecond"}]})["ref"]

    def test_lists_refs_after_compact_and_resume(self):
        ref = self._seed()
        for source in ("compact", "resume"):
            r = _run(json.dumps({"source": source}))
            self.assertEqual(r.returncode, 0, r.stderr)
            text = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertIn(ref, text)
            self.assertIn("mcp__fast-jev-compaction__recall", text)
            self.assertIn("lines 21-22", text)

    def test_silent_when_nothing_was_lost_from_context(self):
        self._seed()
        for source in ("startup", "clear", None):
            self.assertEqual(_run(json.dumps({"source": source})).stdout, "")

    def test_silent_on_an_empty_store_and_on_garbage(self):
        self.assertEqual(_run(json.dumps({"source": "compact"})).stdout, "")
        r = _run("not json")
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_block_is_capped(self):
        refs = [{"ref": "r_%08x" % i, "tool": "Bash", "start": 1, "end": 9, "age_min": 3,
                 "preview": "p" * 70} for i in range(200)]
        text = hook.build(refs)
        self.assertLessEqual(len(text), hook.MAX_CHARS + 60)
        self.assertTrue(text.startswith("<recall-refs>") and text.endswith("</recall-refs>"))
        self.assertEqual(hook.build([]), "")


class TestWiring(unittest.TestCase):
    def test_ensure_settings_adds_the_hook_once(self):
        data = compaction.ensure_settings({})
        data = compaction.ensure_settings(data)
        (block,) = data["hooks"]["SessionStart"]
        self.assertEqual(block["matcher"], "compact|resume")
        (h,) = block["hooks"]
        self.assertIn("airlock_recall_resurface.py", h["command"])
        self.assertEqual(h["timeout"], 5)

    def test_existing_session_start_hooks_are_kept(self):
        mine = {"matcher": "*", "hooks": [{"type": "command", "command": "python3 /x/other.py"}]}
        data = compaction.ensure_settings({"hooks": {"SessionStart": [mine]}})
        self.assertEqual(len(data["hooks"]["SessionStart"]), 2)
        self.assertEqual(data["hooks"]["SessionStart"][0], mine)

    def test_drop_removes_only_the_resurface_hook_and_tidies(self):
        data = compaction.ensure_settings({})
        compaction._drop_resurface(data)
        self.assertNotIn("hooks", data)
        mine = {"matcher": "*", "hooks": [{"type": "command", "command": "python3 /x/other.py"}]}
        data = compaction.ensure_settings({"hooks": {"SessionStart": [mine]}})
        compaction._drop_resurface(data)
        self.assertEqual(data["hooks"]["SessionStart"], [mine])


if __name__ == "__main__":
    unittest.main()
