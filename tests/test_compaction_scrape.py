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


def asstrec(req_id, ts, model=None):
    msg = {"role": "assistant", "content": []}
    if model:
        msg["model"] = model
    return {"type": "assistant", "requestId": req_id, "timestamp": ts, "sessionId": "s1", "message": msg}


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

    def test_requests_after_counts_distinct_later_requests(self):
        ev = sysrec("Bash output 50% smaller (1000 -> 500 chars); 1/2 chunks omitted",
                    ts="2026-10-07T15:00:00.000Z")
        self.write("a/1.jsonl", [
            asstrec("req_1", "2026-10-07T14:59:00.000Z"),   # before the trim: not counted
            ev,
            asstrec("req_2", "2026-10-07T15:01:00.000Z"),
            asstrec("req_2", "2026-10-07T15:01:01.000Z"),   # same request, streamed lines
            asstrec("req_3", "2026-10-07T15:02:00.000Z"),
        ])
        cs.scrape(self.proj)
        c = sqlite3.connect(str(metrics.db_path()))
        self.assertEqual(c.execute("SELECT requests_after FROM compaction").fetchall(), [(2,)])
        c.close()

    def test_requests_after_grows_on_rescrape_of_resumed_session(self):
        ev = sysrec("Bash output 50% smaller (1000 -> 500 chars); 1/2 chunks omitted",
                    ts="2026-10-07T15:00:00.000Z")
        self.write("a/1.jsonl", [ev, asstrec("req_1", "2026-10-07T15:01:00.000Z")])
        cs.scrape(self.proj)
        # Resumed session copies history into a new file and keeps going.
        self.write("b/2.jsonl", [ev, asstrec("req_1", "2026-10-07T15:01:00.000Z"),
                                 asstrec("req_2", "2026-10-07T15:02:00.000Z")])
        cs.scrape(self.proj)
        c = sqlite3.connect(str(metrics.db_path()))
        self.assertEqual(c.execute("SELECT requests_after FROM compaction").fetchall(), [(2,)])
        c.close()

    def test_report_effective_tokens(self):
        self.write("a/1.jsonl", [
            sysrec("Bash output 50% smaller (1000 -> 500 chars); 1/2 chunks omitted",
                   ts="2026-10-07T15:00:00.000Z"),
            asstrec("req_1", "2026-10-07T15:01:00.000Z"),
            asstrec("req_2", "2026-10-07T15:02:00.000Z"),
        ])
        cs.scrape(self.proj)
        out = io.StringIO()
        metrics.report(out=out)
        # 500 chars saved * 2 later requests / 4 chars per token
        self.assertIn("effective ~250 tok", out.getvalue())

    def test_model_is_the_last_request_at_or_before_the_trim(self):
        ev = sysrec("Bash output 50% smaller (1000 -> 500 chars); 1/2 chunks omitted",
                    ts="2026-10-07T15:00:00.000Z")
        self.write("a/1.jsonl", [
            asstrec("req_1", "2026-10-07T14:58:00.000Z", "claude-opus-5-5"),
            asstrec("req_2", "2026-10-07T14:59:00.000Z", "claude-sonnet-5-5"),
            asstrec("req_s", "2026-10-07T14:59:30.000Z", "<synthetic>"),   # not a real model
            ev,
            asstrec("req_3", "2026-10-07T15:01:00.000Z", "claude-haiku-5-5"),
        ])
        cs.scrape(self.proj)
        c = sqlite3.connect(str(metrics.db_path()))
        self.assertEqual(c.execute("SELECT model FROM compaction").fetchall(), [("claude-sonnet-5-5",)])
        c.close()

    def test_model_falls_back_to_first_later_request_and_backfills(self):
        ev = sysrec("Bash output 50% smaller (1000 -> 500 chars); 1/2 chunks omitted",
                    ts="2026-10-07T15:00:00.000Z")
        self.write("a/1.jsonl", [ev])
        cs.scrape(self.proj)
        c = sqlite3.connect(str(metrics.db_path()))
        self.assertEqual(c.execute("SELECT model FROM compaction").fetchall(), [(None,)])
        self.write("a/1.jsonl", [ev, asstrec("req_1", "2026-10-07T15:01:00.000Z", "claude-haiku-5-5")])
        cs.scrape(self.proj)
        self.assertEqual(c.execute("SELECT model FROM compaction").fetchall(), [("claude-haiku-5-5",)])
        c.close()

    def test_later_requests_are_credited_to_their_own_model(self):
        ev = sysrec("Bash output 50% smaller (1000 -> 500 chars); 1/2 chunks omitted",
                    ts="2026-10-07T15:00:00.000Z")
        self.write("a/1.jsonl", [
            asstrec("req_0", "2026-10-07T14:59:00.000Z", "claude-opus-5-5"),   # before the trim: not credited
            ev,
            asstrec("req_1", "2026-10-07T15:01:00.000Z", "claude-opus-5-5"),
            asstrec("req_1", "2026-10-07T15:01:01.000Z", "claude-opus-5-5"),   # streamed lines of one request
            asstrec("req_2", "2026-10-07T15:02:00.000Z", "claude-haiku-5-5"),  # after a /model switch
            asstrec("req_3", "2026-10-07T15:03:00.000Z", "claude-haiku-5-5"),
        ])
        cs.scrape(self.proj)
        cs.scrape(self.proj)  # rescrape keeps the same counts
        c = sqlite3.connect(str(metrics.db_path()))
        self.assertEqual(c.execute("SELECT model, requests FROM compaction_req ORDER BY model").fetchall(),
                         [("claude-haiku-5-5", 2), ("claude-opus-5-5", 1)])
        c.close()

    def test_ensure_schema_migrates_old_table(self):
        conn = sqlite3.connect(str(metrics.db_path()))
        conn.execute("CREATE TABLE compaction (id INTEGER PRIMARY KEY, ts TEXT NOT NULL,"
                     " row_hash TEXT NOT NULL UNIQUE)")
        cs.ensure_schema(conn)
        cs.ensure_schema(conn)  # idempotent
        cols = [r[1] for r in conn.execute("PRAGMA table_info(compaction)")]
        self.assertIn("requests_after", cols)
        self.assertIn("model", cols)
        conn.close()


if __name__ == "__main__":
    unittest.main()
