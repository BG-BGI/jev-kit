"""airlock/overhead_scrape.py: per-session context overhead from transcripts.

Hermetic: a temp projects root with synthetic transcripts, a temp state dir
for metrics.db. The cases that matter: streamed rows repeating a message id
are counted once, first/avg/max context math, subagent transcripts keyed
apart from the parent session, re-scraping a grown session refreshes its row,
and est_cost_usd is NULL -- never a guess -- when the model or its cache
rates are unknown.
"""
import tests  # noqa: F401, I001 -- MUST be the first import; see tests/__init__.py.

import io
import json
import os
import pathlib
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from airlock import metrics, paths, pricing
from airlock import overhead_scrape as ovh

RATES = {"id": "anthropic/claude-sonnet-5.5", "input": 2e-6, "output": 1e-5,
         "cache_read": 2e-7, "cache_write": 2.5e-6}


def arec(mid, ts, tin=0, cread=0, cwrite=0, tout=0, model="claude-sonnet-5-5",
         sid="s1", uuid=None, with_id=True):
    message = {"role": "assistant", "model": model, "content": [],
               "usage": {"input_tokens": tin, "cache_read_input_tokens": cread,
                         "cache_creation_input_tokens": cwrite, "output_tokens": tout}}
    if with_id:
        message["id"] = mid
    return {"type": "assistant", "timestamp": ts, "sessionId": sid,
            "uuid": uuid or (mid + "-u"), "message": message}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        p = mock.patch.object(paths, "state_dir", return_value=self.tmp)
        p.start()
        self.addCleanup(p.stop)
        self.root = self.tmp / "projects"
        (self.root / "proj").mkdir(parents=True)

    def write(self, name, recs):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")

    def rows(self, sql="SELECT * FROM session_overhead"):
        c = sqlite3.connect(str(self.tmp / metrics.DB_NAME))
        c.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in c.execute(sql).fetchall()]
        finally:
            c.close()


class TestScrape(Base):
    def test_dedup_first_avg_max_and_totals(self):
        self.write("proj/t1.jsonl", [
            arec("m1", "2026-10-07T15:00:00.000Z", tin=10, cread=100, cwrite=20, tout=5),
            # Streamed continuation: same message id, same usage. Counted once.
            arec("m1", "2026-10-07T15:00:01.000Z", tin=10, cread=100, cwrite=20, tout=5,
                 uuid="m1-u2"),
            arec("m2", "2026-10-07T15:01:00.000Z", tin=10, cread=400, cwrite=0, tout=5),
            arec("m3", "2026-10-07T15:02:00.000Z", tin=20, cread=800, cwrite=50, tout=5),
        ])
        sessions, written = ovh.scrape(self.root)
        self.assertEqual((sessions, written), (1, 1))
        row = self.rows()[0]
        self.assertEqual(row["session_id"], "s1")
        self.assertEqual(row["project"], "proj")
        self.assertEqual(row["ts"], "2026-10-07T15:00:00.000Z")
        self.assertEqual(row["model"], "claude-sonnet-5-5")
        self.assertEqual(row["requests"], 3)
        self.assertEqual(row["first_ctx_tokens"], 130)   # 10 + 100 + 20
        self.assertEqual(row["avg_ctx_tokens"], 470)     # (130 + 410 + 870) / 3
        self.assertEqual(row["max_ctx_tokens"], 870)
        self.assertEqual(row["input_tokens"], 40)
        self.assertEqual(row["cache_read_tokens"], 1300)
        self.assertEqual(row["cache_write_tokens"], 70)
        self.assertEqual(row["output_tokens"], 15)
        self.assertIsNone(row["est_cost_usd"])           # no pricing cache

    def test_uuid_fallback_when_message_id_missing(self):
        self.write("proj/t1.jsonl", [
            arec("x", "2026-10-07T15:00:00.000Z", tin=5, with_id=False, uuid="u1"),
            arec("x", "2026-10-07T15:00:01.000Z", tin=5, with_id=False, uuid="u1"),
            arec("x", "2026-10-07T15:01:00.000Z", tin=5, with_id=False, uuid="u2"),
        ])
        ovh.scrape(self.root)
        row = self.rows()[0]
        self.assertEqual(row["requests"], 2)
        self.assertEqual(row["input_tokens"], 10)

    def test_subagent_transcript_is_its_own_session(self):
        self.write("proj/t1.jsonl", [arec("m1", "2026-10-07T15:00:00.000Z", tin=100)])
        self.write("proj/subagents/agent1.jsonl",
                   [arec("m2", "2026-10-07T15:05:00.000Z", tin=7, sid="s1")])
        sessions, written = ovh.scrape(self.root)
        self.assertEqual((sessions, written), (2, 2))
        by_id = {r["session_id"]: r for r in self.rows()}
        self.assertEqual(sorted(by_id), ["s1", "sub:s1"])
        self.assertEqual(by_id["s1"]["input_tokens"], 100)
        self.assertEqual(by_id["sub:s1"]["input_tokens"], 7)

    def test_rescrape_of_grown_session_refreshes_row(self):
        self.write("proj/t1.jsonl", [arec("m1", "2026-10-07T15:00:00.000Z", tin=10, cread=100)])
        ovh.scrape(self.root)
        self.write("proj/t1.jsonl", [
            arec("m1", "2026-10-07T15:00:00.000Z", tin=10, cread=100),
            arec("m2", "2026-10-07T15:01:00.000Z", tin=10, cread=300),
        ])
        ovh.scrape(self.root)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["requests"], 2)
        self.assertEqual(rows[0]["cache_read_tokens"], 400)
        self.assertEqual(rows[0]["first_ctx_tokens"], 110)
        self.assertEqual(rows[0]["max_ctx_tokens"], 310)

    def test_est_cost_from_rates_and_none_fallbacks(self):
        self.write("proj/t1.jsonl", [
            arec("m1", "2026-10-07T15:00:00.000Z", tin=100, cread=1000, cwrite=200, tout=50),
            arec("m2", "2026-10-07T15:01:00.000Z", tin=10, cread=500, cwrite=0, tout=5,
                 model="mystery-model", sid="s2"),
        ])
        with mock.patch.object(pricing, "rates", return_value=dict(RATES)):
            ovh.scrape(self.root)
        by_id = {r["session_id"]: r for r in self.rows()}
        want = 100 * 2e-6 + 1000 * 2e-7 + 200 * 2.5e-6 + 50 * 1e-5
        self.assertAlmostEqual(by_id["s1"]["est_cost_usd"], want)
        self.assertIsNone(by_id["s2"]["est_cost_usd"])  # unknown model family

        with mock.patch.object(pricing, "rates", return_value=None):
            ovh.scrape(self.root)
        self.assertIsNone(self.rows()[0]["est_cost_usd"])

        no_cache = dict(RATES, cache_read=None, cache_write=None)
        with mock.patch.object(pricing, "rates", return_value=no_cache):
            ovh.scrape(self.root)
        self.assertIsNone(self.rows()[0]["est_cost_usd"])

    def test_days_window_skips_old_files(self):
        self.write("proj/old.jsonl", [arec("m1", "2026-09-01T15:00:00.000Z", tin=10)])
        old = time.time() - 10 * 86400
        os.utime(self.root / "proj" / "old.jsonl", (old, old))
        self.assertEqual(ovh.scrape(self.root, days=1), (0, 0))
        self.assertEqual(ovh.scrape(self.root), (1, 1))

    def test_non_assistant_and_garbled_lines_are_skipped(self):
        path = self.root / "proj" / "t1.jsonl"
        path.write_text("\n".join([
            json.dumps({"type": "user", "message": {"content": "hi"}}),
            json.dumps({"type": "assistant", "sessionId": "s1",
                        "message": {"role": "assistant", "content": []}}),  # no usage
            'not json but says "assistant"',
            json.dumps(arec("m1", "2026-10-07T15:00:00.000Z", tin=1)),
        ]) + "\n")
        self.assertEqual(ovh.scrape(self.root), (1, 1))

    def test_report_section(self):
        self.write("proj/t1.jsonl", [
            arec("m1", "2026-10-07T15:00:00.000Z", tin=10, cread=1000, cwrite=20, tout=5),
            arec("m2", "2026-10-07T15:01:00.000Z", tin=10, cread=2000, cwrite=0, tout=5),
        ])
        ovh.scrape(self.root)
        out = io.StringIO()
        metrics.report(out=out)
        text = out.getvalue()
        self.assertIn("session overhead (from transcripts)", text)
        self.assertIn("sessions 1, requests 2, cache-read 3000 tok", text)
        self.assertIn("mean 1030 tok, median 1030 tok", text)
        self.assertIn("over 0 priced session(s) of 1", text)

    def test_report_silent_without_rows(self):
        out = io.StringIO()
        metrics.report(out=out)
        self.assertNotIn("session overhead", out.getvalue())

    def test_cli(self):
        from contextlib import redirect_stdout
        self.write("proj/t1.jsonl", [arec("m1", "2026-10-07T15:00:00.000Z", tin=10)])
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ovh._main(["--root", str(self.root), "--days", "365"]), 0)
            self.assertEqual(ovh._main(["--bogus"]), 2)
            self.assertEqual(metrics._main(["scrape-overhead", "--root", str(self.root)]), 0)
        self.assertEqual(len(self.rows()), 1)
        self.assertIn("wrote 1 row(s)", out.getvalue())
        self.assertIn("usage:", out.getvalue())


if __name__ == "__main__":
    unittest.main()
