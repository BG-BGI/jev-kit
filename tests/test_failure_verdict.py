"""The failure-verdict hook: per-session failure state, the verdict request and
the PostToolUseFailure entry point. Fully mocked: no key, no network."""
import tests  # noqa: F401, I001 -- first import: isolates HOME and AIRLOCK_* (see tests/__init__.py)

import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from airlock import failure_state, failure_verdict

HOOK_PATH = Path(__file__).resolve().parent.parent / "hooks" / "airlock_failure_verdict.py"


def _load_hook():
    spec = importlib.util.spec_from_file_location("airlock_failure_verdict", str(HOOK_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ask(choice, confidence=0.95, runner_up=0.02, calls=None):
    def ask(body, timeout_s=None):
        if calls is not None:
            calls.append(body)
        return ({"answers": {"verdict": {
            "type": "choice", "choice": choice, "confidence": confidence,
            "probabilities": {choice: confidence, "retry_with_fix": runner_up}}},
            "usage": {"input_tokens": 300, "output_tokens": 5}}, 40)
    return ask


class StateBase(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        for p in (mock.patch.object(failure_state, "STATE_DIR", d),
                  mock.patch.object(failure_state, "STATE_FILE", d / "failures.json")):
            p.start()
            self.addCleanup(p.stop)


class TestFailureState(StateBase):
    def test_failures_accumulate_per_session(self):
        self.assertEqual(len(failure_state.record_failure("a", "Bash", "x", "e1")), 1)
        self.assertEqual(len(failure_state.record_failure("a", "Bash", "x", "e2")), 2)
        self.assertEqual(len(failure_state.record_failure("b", "Bash", "x", "e1")), 1)

    def test_old_failures_leave_the_window(self):
        with mock.patch("airlock.failure_state.time.time", return_value=1000.0):
            failure_state.record_failure("a", "Bash", "x", "old")
        later = 1000.0 + failure_state.WINDOW_S + 1
        with mock.patch("airlock.failure_state.time.time", return_value=later):
            rows = failure_state.record_failure("a", "Bash", "x", "new")
        self.assertEqual([r["error"] for r in rows], ["new"])

    def test_only_the_last_few_are_kept(self):
        for i in range(failure_state.MAX_KEPT + 3):
            rows = failure_state.record_failure("a", "Bash", "x", str(i))
        self.assertEqual(len(rows), failure_state.MAX_KEPT)
        self.assertEqual(rows[-1]["error"], str(failure_state.MAX_KEPT + 2))

    def test_a_verdict_is_claimed_once_per_window(self):
        self.assertTrue(failure_state.claim_announcement("a", "change_approach"))
        self.assertFalse(failure_state.claim_announcement("a", "change_approach"))
        self.assertTrue(failure_state.claim_announcement("a", "ask_user"))
        self.assertTrue(failure_state.claim_announcement("b", "change_approach"))


class TestVerdict(unittest.TestCase):
    FAILURES = [{"tool": "Bash", "summary": "command: make", "error": "Exit code 2"}] * 2

    def test_spoken_verdicts_need_the_deny_bar(self):
        self.assertTrue(failure_verdict.decide(self.FAILURES, _ask("change_approach"))["speak"])
        self.assertFalse(failure_verdict.decide(
            self.FAILURES, _ask("change_approach", confidence=0.6))["speak"])
        self.assertFalse(failure_verdict.decide(
            self.FAILURES, _ask("change_approach", runner_up=0.7))["speak"])

    def test_silent_verdicts_never_speak(self):
        for verdict in ("retry_with_fix", "expected"):
            self.assertFalse(failure_verdict.decide(self.FAILURES, _ask(verdict))["speak"])

    def test_what_is_sent_is_redacted(self):
        calls = []
        secret = "sk-ant-api03-" + "b" * 40
        failures = [{"tool": "Bash", "summary": failure_verdict.call_summary(
            "Bash", {"command": "curl -H 'x-api-key: %s' https://api" % secret}),
            "error": "401 for key %s" % secret}] * 2
        failure_verdict.decide(failures, _ask("ask_user", calls=calls))
        self.assertNotIn("b" * 40, json.dumps(calls[0]))

    def test_call_summary_picks_the_telling_field(self):
        self.assertEqual(failure_verdict.call_summary("Bash", {"command": "make  -j4"}),
                         "command: make -j4")
        self.assertEqual(failure_verdict.call_summary("Read", {"file_path": "/a/b.py"}),
                         "file_path: /a/b.py")

    def test_advice_names_the_count(self):
        self.assertIn("failed 3 times", failure_verdict.advice("change_approach", 3))
        self.assertIn("nothing was blocked", failure_verdict.advice("ask_user", 2))


class TestHook(StateBase):
    def setUp(self):
        super().setUp()
        self.hook = _load_hook()
        self.tmp = Path(tempfile.mkdtemp())
        env = {"AIRLOCK_CONFIG_DIR": str(self.tmp / "config"),
               "AIRLOCK_STATE_DIR": str(self.tmp / "state")}
        for var in self.hook.DISABLE_VARS + (self.hook.MODE_ENV,):
            env[var] = ""
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)

    def _fail(self, ask, session="s", interrupt=False, key="key"):
        payload = {"session_id": session, "cwd": "/tmp", "hook_event_name": "PostToolUseFailure",
                   "tool_name": "Bash", "tool_input": {"command": "make"},
                   "error": "Exit code 2", "is_interrupt": interrupt}
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             mock.patch("sys.stdout", out), \
             mock.patch("airlock.keyfile.get_api_key", return_value=key), \
             mock.patch("airlock.client.ask", ask):
            self.assertEqual(self.hook.main(), 0)
        raw = out.getvalue()
        return json.loads(raw) if raw.strip() else None

    def _rows(self):
        path = self.tmp / "state" / self.hook.LOG_NAME
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def test_first_failure_never_asks(self):
        calls = []
        self.assertIsNone(self._fail(_ask("change_approach", calls=calls)))
        self.assertEqual(calls, [])

    def test_second_failure_asks_and_speaks_once(self):
        ask = _ask("change_approach")
        self._fail(ask)
        out = self._fail(ask)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PostToolUseFailure")
        self.assertIn("failed 2 times", hso["additionalContext"])
        self.assertIsNone(self._fail(ask))
        self.assertEqual(self._rows()[-1]["suppressed"], "said_recently")

    def test_the_row_carries_the_redacted_error_for_audit(self):
        ask = _ask("change_approach")
        self._fail(ask)
        self._fail(ask)
        self.assertEqual(self._rows()[-1]["last_error"], "Exit code 2")

    def test_the_stored_error_is_redacted(self):
        secret = "sk-ant-api03-" + "d" * 40
        failure_state.record_failure("r", "Bash", "curl", "401 for %s" % secret)
        self.assertNotIn("d" * 40, failure_state.STATE_FILE.read_text())

    def test_silent_verdict_is_logged_not_said(self):
        ask = _ask("retry_with_fix")
        self._fail(ask)
        self.assertIsNone(self._fail(ask))
        self.assertEqual(self._rows()[-1]["verdict"], "retry_with_fix")
        self.assertFalse(self._rows()[-1]["emitted"])

    def test_interrupt_is_ignored_entirely(self):
        calls = []
        for _ in range(3):
            self.assertIsNone(self._fail(_ask("change_approach", calls=calls), interrupt=True))
        self.assertEqual(calls, [])

    def test_shadow_judges_and_says_nothing(self):
        (self.tmp / "config").mkdir(parents=True)
        (self.tmp / "config" / "failure-verdict").write_text("shadow\n")
        ask = _ask("change_approach")
        self._fail(ask)
        self.assertIsNone(self._fail(ask))
        row = self._rows()[-1]
        self.assertEqual((row["mode"], row["verdict"], row["emitted"]),
                         ("shadow", "change_approach", False))

    def test_error_and_no_key_are_silent(self):
        def boom(body, timeout_s=None):
            raise OSError("down")
        self._fail(boom)
        self.assertIsNone(self._fail(boom))
        self.assertIn("down", self._rows()[-1]["error"])
        self.assertIsNone(self._fail(_ask("change_approach"), session="k", key=None))
        self.assertIsNone(self._fail(_ask("change_approach"), session="k", key=None))

    def test_kill_switch_and_off(self):
        calls = []
        with mock.patch.dict(os.environ, {"AIRLOCK_DISABLE": "1"}):
            self._fail(_ask("change_approach", calls=calls))
            self._fail(_ask("change_approach", calls=calls))
        with mock.patch.dict(os.environ, {self.hook.MODE_ENV: "off"}):
            self._fail(_ask("change_approach", calls=calls))
            self._fail(_ask("change_approach", calls=calls))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
