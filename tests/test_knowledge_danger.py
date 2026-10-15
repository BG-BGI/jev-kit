"""knowledge/danger.py and its two ends: airlock/enforce.py spools, the capture hook drains."""
import tests  # noqa: F401, I001 -- first import: isolates HOME and AIRLOCK_* (see tests/__init__.py)

import importlib.util
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from airlock import enforce, paths
from knowledge import config, danger

HOOKS = Path(__file__).resolve().parent.parent / "hooks"


def _load_capture():
    spec = importlib.util.spec_from_file_location("knowledge_capture_danger_t",
                                                  str(HOOKS / "knowledge_capture.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capture = _load_capture()

PAYLOAD = {"tool_name": "Bash", "session_id": "s1", "cwd": "/tmp/repo",
           "tool_input": {"command": "git push upstream main"}}
REASON = "BLOCKED (airlock enforce, R4-no-upstream-push): never push to upstream"


class Isolated(unittest.TestCase):
    def setUp(self):
        self.spool_path = paths.state_dir() / danger.SPOOL_NAME
        for p in self.spool_path.parent.glob(danger.SPOOL_NAME + "*"):
            p.unlink()
        state = self.spool_path.parent / "knowledge-capture.json"
        if state.exists():
            state.unlink()
        config.set_enabled(True)
        danger.mark_active()

    def tearDown(self):
        for p in self.spool_path.parent.glob(danger.SPOOL_NAME + "*"):
            p.unlink()
        self.marker.unlink(missing_ok=True)
        config.set_enabled(True)

    @property
    def marker(self):
        return self.spool_path.parent / danger.ACTIVE_NAME


class SpoolDrain(Isolated):
    def test_roundtrip(self):
        self.assertTrue(danger.spool("deny", REASON, PAYLOAD))
        rows = danger.drain()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["action"], "deny")
        self.assertEqual(row["rule"], "R4-no-upstream-push")
        self.assertEqual(row["tool"], "Bash")
        self.assertEqual(row["session_id"], "s1")
        self.assertIn("git push upstream main", row["summary"])

    def test_drain_clears(self):
        danger.spool("ask", REASON, PAYLOAD)
        self.assertEqual(len(danger.drain()), 1)
        self.assertEqual(danger.drain(), [])
        self.assertFalse(self.spool_path.exists())

    def test_drain_limit_and_order(self):
        for i in range(5):
            danger.spool("warn", "WARN (airlock R%d)" % i, PAYLOAD)
        rows = danger.drain(limit=3)
        self.assertEqual([r["rule"] for r in rows], ["R0", "R1", "R2"])

    def test_off_switch_writes_nothing(self):
        config.set_enabled(False)
        self.assertFalse(danger.spool("deny", REASON, PAYLOAD))
        self.assertFalse(self.spool_path.exists())

    def test_not_installed_writes_nothing(self):
        self.marker.unlink()
        self.assertFalse(danger.spool("deny", REASON, PAYLOAD))
        self.assertFalse(self.spool_path.exists())

    def test_stale_marker_writes_nothing(self):
        old = danger.time.time() - danger.ACTIVE_MAX_AGE_S - 60
        os.utime(str(self.marker), (old, old))
        self.assertFalse(danger.spool("deny", REASON, PAYLOAD))

    def test_mark_active_makes_spooling_possible_again(self):
        self.marker.unlink()
        self.assertTrue(danger.mark_active())
        self.assertTrue(danger.spool("deny", REASON, PAYLOAD))

    def test_unknown_action_refused(self):
        self.assertFalse(danger.spool("allow", REASON, PAYLOAD))

    def test_size_cap(self):
        self.spool_path.parent.mkdir(parents=True, exist_ok=True)
        self.spool_path.write_text("x" * (danger.MAX_BYTES + 1))
        self.assertFalse(danger.spool("deny", REASON, PAYLOAD))

    def test_secret_redacted_before_disk(self):
        secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
        danger.spool("deny", "BLOCKED (airlock R1) token %s" % secret,
                     {"tool_name": "Bash", "tool_input": {"command": "curl -H %s x" % secret}})
        self.assertNotIn(secret, self.spool_path.read_text())

    def test_corrupt_lines_skipped(self):
        self.spool_path.parent.mkdir(parents=True, exist_ok=True)
        self.spool_path.write_text('not json\n{"action": "deny", "rule": "R9"}\n[1]\n')
        rows = danger.drain()
        self.assertEqual([r["rule"] for r in rows], ["R9"])

    def test_missing_payload_still_spools(self):
        self.assertTrue(danger.spool("deny", REASON, None))
        self.assertEqual(danger.drain()[0]["tool"], "")

    def test_dedup_key_groups_same_rule(self):
        a = {"rule": "R4", "tool": "Bash", "action": "deny", "reason": "x"}
        b = dict(a, reason="y")
        self.assertEqual(danger.dedup_key(a), danger.dedup_key(b))
        self.assertNotEqual(danger.dedup_key(a), danger.dedup_key(dict(a, action="ask")))


class EnforceEnds(Isolated):
    def _emit(self, fn, arg):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            enforce._CURRENT["data"] = PAYLOAD
            try:
                fn(arg)
            finally:
                enforce._CURRENT.clear()
        return out.getvalue()

    def test_deny_prints_verdict_and_spools(self):
        out = self._emit(enforce.emit_deny, REASON)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        rows = danger.drain()
        self.assertEqual([r["action"] for r in rows], ["deny"])
        self.assertEqual(rows[0]["tool"], "Bash")

    def test_ask_and_warn_spool(self):
        self._emit(enforce.emit_ask, REASON)
        self._emit(enforce.emit_warn, ["WARN (airlock R8): use plocate"])
        self.assertEqual([r["action"] for r in danger.drain()], ["ask", "warn"])

    def test_spool_failure_never_changes_the_verdict(self):
        with mock.patch("knowledge.danger.spool", side_effect=RuntimeError("disk full")):
            out = self._emit(enforce.emit_deny, REASON)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_off_switch_leaves_no_spool(self):
        config.set_enabled(False)
        self._emit(enforce.emit_deny, REASON)
        self.assertFalse(self.spool_path.exists())


class CaptureDrain(Isolated):
    def test_rows_become_one_danger_candidate_each_and_dedup(self):
        for _ in range(3):
            danger.spool("deny", REASON, PAYLOAD)
        danger.spool("ask", REASON, PAYLOAD)
        got = []
        capture.drain_danger(ingest=lambda c: got.append(c) or {"status": "stored"})
        self.assertEqual(len(got), 2)
        self.assertTrue(all(c.bucket_hint == "danger" and c.source == "hook" for c in got))
        self.assertIn("R4-no-upstream-push", got[0].text)
        self.assertIsNone(got[0].signature)

    def test_second_drain_inside_window_is_silent(self):
        danger.spool("deny", REASON, PAYLOAD)
        capture.drain_danger(ingest=lambda c: {"status": "stored"})
        danger.spool("deny", REASON, PAYLOAD)
        got = []
        capture.drain_danger(ingest=lambda c: got.append(c) or {})
        self.assertEqual(got, [])

    def test_ingest_error_is_swallowed(self):
        danger.spool("deny", REASON, PAYLOAD)

        def boom(c):
            raise RuntimeError("gate down")
        self.assertEqual(capture.drain_danger(ingest=boom), [])

    def test_empty_spool_makes_no_ingest_call(self):
        got = []
        self.assertEqual(capture.drain_danger(ingest=lambda c: got.append(c)), [])
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
