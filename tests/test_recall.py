import tests  # noqa: F401, I001 -- MUST be the first import; see tests/__init__.py.

import datetime
import io
import json
import os
import sqlite3
import stat
import sys
import unittest
from unittest import mock

from airlock import paths, recall


def payload(text="a\nb\nc", start=21, tool="Bash", session="s1"):
    end = start + len(text.split("\n")) - 1
    return {"session": session, "tool": tool, "ranges": [{"start": start, "end": end, "text": text}]}


class RecallCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(self._wipe)
        self._wipe()

    @staticmethod
    def _wipe():
        try:
            os.remove(recall.db_path())
        except OSError:
            pass


class TestPutGet(RecallCase):
    def test_roundtrip_numbers_lines(self):
        r = recall.put(payload())
        self.assertTrue(r["ok"])
        self.assertTrue(r["ref"].startswith("r_"))
        got = recall.get(r["ref"])
        self.assertEqual(got["text"], "21: a\n22: b\n23: c")
        self.assertFalse(got["truncated"])

    def test_idempotent_ref(self):
        self.assertEqual(recall.put(payload())["ref"], recall.put(payload())["ref"])
        conn = sqlite3.connect(str(recall.db_path()))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM recall").fetchone()[0], 1)

    def test_different_text_different_ref(self):
        self.assertNotEqual(recall.put(payload("x"))["ref"], recall.put(payload("y"))["ref"])

    def test_range_filter(self):
        ref = recall.put(payload())["ref"]
        self.assertEqual(recall.get(ref, "22-22")["text"], "22: b")

    def test_range_outside_lists_stored(self):
        ref = recall.put(payload())["ref"]
        got = recall.get(ref, "1-5")
        self.assertFalse(got["ok"])
        self.assertIn("21-23", got["error"])

    def test_truncates_with_hint(self):
        ref = recall.put(payload("\n".join("line %d" % i for i in range(500))))["ref"]
        got = recall.get(ref, max_chars=100)
        self.assertTrue(got["truncated"])
        self.assertIn("pass range", got["text"])

    def test_multiple_ranges_ordered(self):
        p = {"session": "s", "tool": "Read", "ranges": [
            {"start": 50, "end": 50, "text": "z"}, {"start": 1, "end": 2, "text": "a\nb"}]}
        got = recall.get(recall.put(p)["ref"])
        self.assertEqual(got["text"], "1: a\n2: b\n50: z")

    def test_unknown_ref(self):
        got = recall.get("r_deadbeef")
        self.assertFalse(got["ok"])
        self.assertIn("re-run", got["error"])

    def test_bad_payload_fails_open(self):
        for bad in ({}, {"ranges": [{"start": 0, "end": 1, "text": "x"}]},
                    {"ranges": [{"start": 1, "end": 1, "text": 5}]}, {"ranges": "x"}):
            self.assertFalse(recall.put(bad)["ok"])

    def test_store_error_fails_open(self):
        with mock.patch.object(recall, "_connect", side_effect=sqlite3.OperationalError("locked")):
            self.assertFalse(recall.put(payload())["ok"])
            self.assertFalse(recall.get("r_x")["ok"])

    @unittest.skipIf(sys.platform == "win32", "POSIX modes")
    def test_modes(self):
        recall.put(payload())
        self.assertEqual(stat.S_IMODE(os.stat(recall.db_path()).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(recall.db_path().parent).st_mode), 0o700)


class TestRetention(RecallCase):
    def test_ttl_expires_on_put_and_get(self):
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=8)
        ref = recall.put(payload("old"), now=old)["ref"]
        self.assertFalse(recall.get(ref)["ok"])  # hidden even before pruned
        recall.put(payload("new"))  # prune on write removes it
        conn = sqlite3.connect(str(recall.db_path()))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM recall").fetchone()[0], 1)

    def test_size_cap_drops_oldest_first(self):
        t0 = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)
        refs = []
        for i in range(3):
            when = t0 + datetime.timedelta(hours=i)
            refs.append(recall.put(payload("x" * 400_000 + str(i)), max_mb=1, now=when)["ref"])
        self.assertFalse(recall.get(refs[0])["ok"])
        self.assertTrue(recall.get(refs[2], max_chars=10)["ok"])


class TestCli(RecallCase):
    def run_cli(self, argv, stdin=""):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)), mock.patch("sys.stdout", out):
            self.assertEqual(recall.main(argv), 0)
        return out.getvalue()

    def test_put_get_list_purge(self):
        ref = json.loads(self.run_cli(["put"], json.dumps(payload())))["ref"]
        got = json.loads(self.run_cli(["get", ref, "--range", "21-21"]))
        self.assertEqual(got["text"], "21: a")
        self.assertIn(ref, self.run_cli(["list"]))
        self.assertIn("21: a", self.run_cli(["show", ref]))
        self.assertIn("purged 1 rows", self.run_cli(["purge"]))

    def test_put_garbage_stdin_fails_open(self):
        self.assertFalse(json.loads(self.run_cli(["put"], "not json"))["ok"])

    def test_db_lives_in_state_dir(self):
        self.assertEqual(recall.db_path(), paths.state_dir() / "recall.db")


class TestRecent(RecallCase):
    NOW = datetime.datetime(2026, 10, 14, 12, 0, tzinfo=datetime.timezone.utc)

    def _put(self, text, minutes_ago, **kw):
        when = self.NOW - datetime.timedelta(minutes=minutes_ago)
        return recall.put(payload(text, **kw), now=when)["ref"]

    def test_newest_first_with_age_and_range(self):
        old = self._put("older line here", 90)
        new = self._put("newer line here\nsecond", 5, start=40)
        got = recall.recent(now=self.NOW)
        self.assertEqual([r["ref"] for r in got], [new, old])
        self.assertEqual((got[0]["start"], got[0]["end"], got[0]["age_min"]), (40, 41, 5))
        self.assertEqual(got[1]["age_min"], 90)

    def test_horizon_and_limit(self):
        for i in range(5):
            self._put("line number %d here" % i, 10 + i)
        stale = self._put("stale line content", 60 * 20)
        refs = [r["ref"] for r in recall.recent(hours=12, limit=3, now=self.NOW)]
        self.assertEqual(len(refs), 3)
        self.assertNotIn(stale, [r["ref"] for r in recall.recent(hours=12, limit=50, now=self.NOW)])

    def test_preview_skips_trivia_and_is_redacted_and_cut(self):
        self._put("41:\n\nexport TYPESAFE_API_KEY=apikey_abcdef1234567890abcdef " + "x" * 200, 1)
        (r,) = recall.recent(now=self.NOW)
        self.assertNotIn("apikey_abcdef1234567890abcdef", r["preview"])
        self.assertLessEqual(len(r["preview"]), recall.PREVIEW_CHARS)
        self.assertTrue(r["preview"].startswith("export"))

    def test_empty_store_and_errors_fail_open(self):
        self.assertEqual(recall.recent(now=self.NOW), [])
        with mock.patch.object(recall, "_connect", side_effect=sqlite3.Error("boom")):
            self.assertEqual(recall.recent(now=self.NOW), [])


if __name__ == "__main__":
    unittest.main()
