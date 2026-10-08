import tests  # noqa: F401, I001 -- MUST be the first import; see tests/__init__.py.

import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from airlock import client, log, metrics, paths


class TestMetrics(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        p = mock.patch.object(paths, "state_dir", return_value=self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def rows(self, sql):
        import sqlite3
        c = sqlite3.connect(str(self.tmp / metrics.DB_NAME))
        try:
            return c.execute(sql).fetchall()
        finally:
            c.close()

    def test_log_append_lands_in_events(self):
        log.append({"ts": "2026-10-07T00:00:00+00:00", "guard": "rules", "action": "deny",
                    "rule_id": "R5-sudo", "tool_name": "Bash"}, log_file=self.tmp / "x.jsonl")
        log.append({"guard": "g", "latency_ms": 12, "usage": {"input_tokens": 5, "output_tokens": 2}},
                   log_file=self.tmp / "x.jsonl")
        self.assertEqual(self.rows("SELECT component, action, jev_called FROM events ORDER BY id"),
                         [("rules", "deny", 0), ("g", None, 1)])
        self.assertEqual(self.rows("SELECT input_tokens, output_tokens FROM events WHERE jev_called=1"),
                         [(5, 2)])

    def test_denied_uses_would_deny(self):
        f = self.tmp / "x.jsonl"
        log.append({"guard": "g", "action": "deny", "would_deny": False}, log_file=f)
        log.append({"guard": "g", "action": "deny", "would_deny": True}, log_file=f)
        log.append({"guard": "rules", "action": "deny"}, log_file=f)
        log.append({"guard": "rules", "action": "allow"}, log_file=f)
        self.assertEqual(self.rows("SELECT denied FROM events ORDER BY id"), [(0,), (1,), (1,), (0,)])

    def test_failure_verdict_last_error_is_not_its_own_error(self):
        f = self.tmp / "x.jsonl"
        log.append({"guard": "failure_verdict", "verdict": "retry", "last_error": "Exit code 1",
                    "latency_ms": 9}, log_file=f)
        log.append({"guard": "other", "last_error": "boom"}, log_file=f)
        self.assertEqual(self.rows("SELECT error FROM events ORDER BY id"), [(None,), ("boom",)])

    def test_backfill_idempotent(self):
        (self.tmp / "decide.jsonl").write_text(
            json.dumps({"ts": "2026-10-07T00:00:00+00:00", "guard": "decide", "latency_ms": 9,
                        "jev_cost_usd": 0.5}) + "\nnot json\n")
        self.assertEqual(metrics.backfill(self.tmp), (1, 1))
        self.assertEqual(metrics.backfill(self.tmp), (1, 0))
        self.assertEqual(self.rows("SELECT cost_usd FROM events"), [(0.5,)])

    def test_ask_records_direct_success_and_failure(self):
        body = {"state": "s", "model": "m", "questions": [1, 2]}
        resp = {"usage": {"input_tokens": 7, "output_tokens": 1}}
        with mock.patch.object(client, "_ask_via_daemon", return_value=None), \
                mock.patch.object(client.keyfile, "get_api_key", return_value="k"), \
                mock.patch.object(client, "call_jev", return_value=(resp, 33)):
            client.ask(body)
        with mock.patch.object(client, "_ask_via_daemon", return_value=None), \
                mock.patch.object(client.keyfile, "get_api_key", return_value="k"), \
                mock.patch.object(client, "call_jev", side_effect=client.TypeSafeError("boom")):
            with self.assertRaises(client.TypeSafeError):
                client.ask(body)
        self.assertEqual(self.rows("SELECT transport, ok, latency_ms, input_tokens, n_questions, error"
                                   " FROM jev_calls ORDER BY id"),
                         [("direct", 1, 33, 7, 2, None), ("direct", 0, None, None, 2, "boom")])

    def test_report_runs(self):
        log.append({"guard": "rules", "action": "deny"}, log_file=self.tmp / "x.jsonl")
        out = io.StringIO()
        metrics.report(out=out)
        self.assertIn("rules", out.getvalue())

    def test_record_never_raises(self):
        with mock.patch.object(metrics, "_connect", side_effect=OSError("disk")):
            metrics.record_event({"a": 1}, "s")
            metrics.record_call("direct", True)


if __name__ == "__main__":
    unittest.main()
