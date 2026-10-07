import tests  # noqa: F401, I001 -- MUST be the first import; see tests/__init__.py.

import io
import json
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from airlock import compaction_scrape as cs
from airlock import metrics, paths


def sysrec(text, ts="2026-10-07T15:00:00.000Z"):
    return {"type": "system", "subtype": "informational", "content": "fast-jev-compaction: " + text,
            "timestamp": ts, "sessionId": "s1", "cwd": "/w"}


class TestParse(unittest.TestCase):
    def test_tool(self):
        r = cs.parse("Bash output 66% smaller (14891 -> 5059 chars); 10/15 chunks omitted")
        self.assertEqual((r["tool"], r["chars_before"], r["chars_after"], r["chunks_omitted"], r["chunks"]),
                         ("Bash", 14891, 5059, 10, 15))

    def test_session_kept(self):
        r = cs.parse("kept 121/435 messages, no summary (74% reduction; 157 call_dropped, 3 pinned; "
                     "state ~19910 tokens (old messages collapsed) in 2 request(s))")
        self.assertEqual((r["msgs_kept"], r["msgs_total"], r["call_dropped"], r["pinned"], r["state_tokens"],
                          r["requests"]), (121, 435, 157, 3, 19910, 2))

    def test_fallback_and_passthrough_and_decisions(self):
        self.assertEqual(cs.parse("fallback to built-in summary (below 25% minimum: 24% reduction; "
                                  "41 call_dropped, 2 pinned; state ~19993 tokens (x) in 1 request(s))")["outcome"],
                         "fallback")
        self.assertEqual(cs.parse("Read result passed through (HTTP 500)")["outcome"], "passed_through")
        self.assertIsNone(cs.parse("Bash decisions: 1-20:keep/1.00"))


class TestScrape(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        p = mock.patch.object(paths, "state_dir", return_value=self.tmp)
        p.start()
        self.addCleanup(p.stop)
        self.proj = self.tmp / "projects"
        (self.proj / "a").mkdir(parents=True)
        (self.proj / "b").mkdir(parents=True)

    def write(self, name, recs):
        (self.proj / name).write_text("\n".join(json.dumps(r) for r in recs) + "\n")

    def test_only_system_records_and_fork_dedup(self):
        ev = sysrec("Bash output 50% smaller (1000 -> 500 chars); 1/2 chunks omitted")
        quoted = {"type": "user", "message": {"content": "fast-jev-compaction: Bash output 99% smaller "
                                              "(1000 -> 10 chars); 1/2 chunks omitted"}, "timestamp": "t"}
        self.write("a/1.jsonl", [ev, quoted])
        self.write("b/2.jsonl", [ev])  # forked copy
        self.assertEqual(cs.scrape(self.proj), (2, 1))
        self.assertEqual(cs.scrape(self.proj), (2, 0))
        c = sqlite3.connect(str(metrics.db_path()))
        self.assertEqual(c.execute("SELECT chars_before, chars_after, session_id FROM compaction").fetchall(),
                         [(1000, 500, "s1")])
        c.close()

    def test_report_section(self):
        self.write("a/1.jsonl", [sysrec("Bash output 50% smaller (1000 -> 500 chars); 1/2 chunks omitted")])
        cs.scrape(self.proj)
        out = io.StringIO()
        metrics.report(out=out)
        self.assertIn("saved 500", out.getvalue())


if __name__ == "__main__":
    unittest.main()
