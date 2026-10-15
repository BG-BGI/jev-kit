"""hooks/knowledge_capture.py, knowledge_session.py, knowledge_prompt.py.

The knowledge/ modules are replaced by in-memory fakes, so these tests pin the
hooks' own contract: budgets, dedup, rules, fail-open. They do not depend on
the real store, gate or ingest.
"""
import tests  # noqa: F401, I001 -- first import: isolates HOME and AIRLOCK_* (see tests/__init__.py)

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

from airlock import paths

HOOKS = Path(__file__).resolve().parent.parent / "hooks"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(HOOKS / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capture = _load("knowledge_capture_t", "knowledge_capture.py")
session = _load("knowledge_session_t", "knowledge_session.py")
prompt = _load("knowledge_prompt_t", "knowledge_prompt.py")


@dataclass
class Candidate:
    bucket_hint: object = None
    text: str = ""
    tool: str = ""
    signature: object = None
    exception_type: str = ""
    entities: tuple = ()
    scope: str = "machine"
    session_id: str = ""
    provenance: str = ""
    source: str = "hook"
    rejected_alternatives: tuple = ()
    fixes_signature: object = None


def node(i, bucket="failure", text="something", evidence=1):
    return types.SimpleNamespace(id=i, bucket=bucket, text=text, evidence=evidence)


class Fakes:
    """In-memory stand-ins for knowledge.config/signature/store/types/ingest/gate."""

    def __init__(self):
        self.fixes = {}
        self.constraints = []
        self.top = []
        self.near = []
        self.ingested = []
        self.picked = []
        self.pick_result = []
        self.ingest_result = {"status": "stored", "id": 7, "reason": ""}
        self.enabled = True

        cfg = types.ModuleType("knowledge.config")
        cfg.SESSION_NODES, cfg.PROMPT_NODES, cfg.FAIL_LINES = 5, 2, 2
        cfg.FAIL_DEDUP_SECONDS, cfg.PREVIEW_CHARS = 600, 70
        cfg.CONSTRAINT_WARN, cfg.MIN_PROMPT_CHARS = 10, 40
        cfg.enabled = lambda: self.enabled
        self.config = cfg

        sig = types.ModuleType("knowledge.signature")
        sig.signature = lambda tool, text: hashlib.sha1(
            (tool + "|" + "".join(c for c in text if not c.isdigit())).encode()).hexdigest()[:16]
        sig.exception_type = lambda text: text.split(":")[0] if ":" in text else ""
        self.signature = sig

        st = types.ModuleType("knowledge.store")
        st.scopes_for = lambda cwd: ["repo:" + str(cwd), "machine", "global"]
        st.fixes_for = lambda s, scopes: self.fixes.get(s, [])
        st.constraints = lambda: list(self.constraints)
        st.top_for_scopes = lambda scopes, k: list(self.top)[:k]
        st.nearest = lambda text, exc, ents, scopes, k=3: list(self.near)[:k]
        self.store = st

        ty = types.ModuleType("knowledge.types")
        ty.Candidate = Candidate
        self.types = ty

        ing = types.ModuleType("knowledge.ingest")

        def ingest(c):
            self.ingested.append(c)
            return self.ingest_result
        ing.ingest = ingest
        self.ingest_mod = ing

        gt = types.ModuleType("knowledge.gate")

        def pick(p, cands):
            self.picked.append((p, list(cands)))
            return list(self.pick_result)
        gt.pick_for_prompt = pick
        self.gate = gt

    def patch(self):
        pkg = types.ModuleType("knowledge")
        pkg.__path__ = []
        mods = {"knowledge": pkg, "knowledge.config": self.config, "knowledge.signature": self.signature,
                "knowledge.store": self.store, "knowledge.types": self.types,
                "knowledge.ingest": self.ingest_mod, "knowledge.gate": self.gate}
        for k, v in mods.items():
            if k != "knowledge":
                setattr(pkg, k.split(".")[1], v)
        return mock.patch.dict(sys.modules, mods)


class Base(unittest.TestCase):
    def setUp(self):
        self.fk = Fakes()
        p = self.fk.patch()
        p.start()
        self.addCleanup(p.stop)
        self._wipe()
        self.addCleanup(self._wipe)

    @staticmethod
    def _wipe():
        for name in (capture.STATE_NAME, prompt.STATE_NAME):
            try:
                os.remove(str(paths.state_file(name)))
            except OSError:
                pass


def fail(tool="Bash", command="ls /x", error="KeyError: 'a'", session_id="s1"):
    return {"hook_event_name": "PostToolUseFailure", "session_id": session_id, "cwd": "/repo",
            "tool_name": tool, "tool_input": {"command": command}, "error": error}


def ok(tool="Bash", command="ls", session_id="s1", response="fine", **extra):
    d = {"hook_event_name": "PostToolUse", "session_id": session_id, "cwd": "/repo",
         "tool_name": tool, "tool_input": {"command": command}, "tool_response": response}
    d.update(extra)
    return d


class TestFixRecall(Base):
    def _seed_fix(self, payload):
        sig = self.fk.signature.signature(payload["tool_name"], payload["error"])
        self.fk.fixes[sig] = [node(3, "fix", "check passed after failure | edited foo.py " + "x" * 200,
                                   evidence=4)]
        return sig

    def test_hit_prints_two_lines_with_a_capped_preview_and_makes_no_ingest_call(self):
        p = fail()
        self._seed_fix(p)
        lines, _ = capture.on_failure(p)
        self.assertEqual(len(lines), 2)
        self.assertIn("fix #3", lines[0])
        self.assertIn("4x", lines[0])
        self.assertLessEqual(len(lines[1]), len("Fix that worked: ") + 70)
        self.assertEqual(self.fk.ingested, [])

    def test_hit_is_said_once_per_signature_per_window(self):
        p = fail()
        self._seed_fix(p)
        self.assertEqual(len(capture.on_failure(p)[0]), 2)
        self.assertEqual(capture.on_failure(p)[0], [])
        self.assertEqual(self.fk.ingested, [])

    def test_a_different_signature_is_not_deduped(self):
        a, b = fail(error="KeyError: 'a'"), fail(error="ValueError: bad")
        self._seed_fix(a)
        self._seed_fix(b)
        self.assertEqual(len(capture.on_failure(a)[0]), 2)
        self.assertEqual(len(capture.on_failure(b)[0]), 2)

    def test_dedup_window_expires(self):
        p = fail()
        self._seed_fix(p)
        capture.on_failure(p)
        with mock.patch.object(capture.time, "time", return_value=capture.time.time() + 601):
            self.assertEqual(len(capture.on_failure(p)[0]), 2)

    def test_recall_text_is_redacted(self):
        p = fail()
        sig = self._seed_fix(p)
        self.fk.fixes[sig] = [node(3, "fix", "use key sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD now")]
        lines, _ = capture.on_failure(p)
        self.assertNotIn("sk-abcdefghijklmnop", "\n".join(lines))

    def test_main_emits_hook_json_for_the_failure_event(self):
        p = fail()
        self._seed_fix(p)
        out = self._run_main(capture, p)
        body = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(body["hookEventName"], "PostToolUseFailure")
        self.assertEqual(len(body["additionalContext"].splitlines()), 2)

    def _run_main(self, mod, payload):
        import io
        buf = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(sys, "stdout", buf), \
                mock.patch.object(mod, "_arm_watchdog", lambda s: None):
            self.assertEqual(mod.main(), 0)
        return buf.getvalue()


class TestCaptureRules(Base):
    def test_first_failure_nominates_nothing(self):
        capture.on_failure(fail())
        self.assertEqual(self.fk.ingested, [])

    def test_repeat_failure_ingests_a_failure_candidate(self):
        capture.on_failure(fail(command="ls /a"))
        capture.on_failure(fail(command="ls /b"))
        (c,) = self.fk.ingested
        self.assertEqual((c.bucket_hint, c.source, c.tool), ("failure", "hook", "Bash"))
        self.assertEqual(c.exception_type, "KeyError")
        self.assertTrue(c.signature)
        self.assertTrue(c.scope.startswith("repo:"))

    def test_repeat_in_another_session_does_not_count(self):
        capture.on_failure(fail(session_id="a"))
        capture.on_failure(fail(session_id="b"))
        self.assertEqual(self.fk.ingested, [])

    def test_repeat_outside_the_window_does_not_count(self):
        capture.on_failure(fail())
        with mock.patch.object(capture.time, "time", return_value=capture.time.time() + 1000):
            capture.on_failure(fail())
        self.assertEqual(self.fk.ingested, [])

    def test_interrupt_is_ignored(self):
        p = fail()
        p["is_interrupt"] = True
        capture.on_failure(p)
        capture.on_failure(p)
        self.assertEqual(self.fk.ingested, [])

    def test_candidate_text_is_redacted(self):
        secret = "sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD"
        capture.on_failure(fail(command="curl -H 'x: %s' u" % secret))
        capture.on_failure(fail(command="curl -H 'x: %s' u" % secret))
        (c,) = self.fk.ingested
        self.assertNotIn(secret, c.text + c.provenance + " ".join(c.entities))

    def test_airlock_marker_in_a_failure_is_a_danger_candidate_once(self):
        err = "BLOCKED (airlock R4): rm -rf is destructive"
        capture.on_failure(fail(error=err))
        (c,) = self.fk.ingested
        self.assertEqual(c.bucket_hint, "danger")
        self.assertIn("airlock", c.text)
        capture.on_failure(fail(error=err))
        self.assertEqual([x.bucket_hint for x in self.fk.ingested].count("danger"), 1)

    def test_airlock_marker_in_a_success_result_is_danger(self):
        capture.on_success(ok(response={"stdout": "WARN (airlock R2): force push"}))
        (c,) = self.fk.ingested
        self.assertEqual(c.bucket_hint, "danger")

    def test_ingest_errors_do_not_escape_main(self):
        self.fk.ingest_mod.ingest = mock.Mock(side_effect=RuntimeError("boom"))
        capture.on_failure(fail())
        import io
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(fail()))), \
                mock.patch.object(capture, "_arm_watchdog", lambda s: None):
            self.assertEqual(capture.main(), 0)


class TestRedToGreen(Base):
    CMD = "python3 -m pytest tests/test_a.py"

    def test_green_after_red_is_a_fix_candidate_linked_to_the_failure(self):
        p = fail(command=self.CMD, error="AssertionError: 1 != 2")
        sig = self.fk.signature.signature("Bash", p["error"])
        capture.on_failure(p)
        capture.on_success(ok("Edit", command="", tool_input={"file_path": "/repo/src/a.py"}))
        self.assertEqual(self.fk.ingested, [])
        capture.on_success(ok(command=self.CMD))
        (c,) = self.fk.ingested
        self.assertEqual((c.bucket_hint, c.fixes_signature, c.source), ("fix", sig, "hook"))
        self.assertIn("a.py", c.text)

    def test_edit_tool_input_is_read_from_the_payload(self):
        capture.on_failure(fail(command=self.CMD, error="AssertionError: x"))
        payload = ok("Edit")
        payload["tool_input"] = {"file_path": "/repo/src/b.py"}
        capture.on_success(payload)
        capture.on_success(ok(command=self.CMD))
        (c,) = self.fk.ingested
        self.assertIn("b.py", c.text)

    def test_green_without_a_prior_red_is_noise(self):
        capture.on_success(ok(command=self.CMD))
        self.assertEqual(self.fk.ingested, [])

    def test_a_flip_is_consumed_once(self):
        capture.on_failure(fail(command=self.CMD, error="AssertionError: x"))
        capture.on_success(ok(command=self.CMD))
        capture.on_success(ok(command=self.CMD))
        self.assertEqual(len(self.fk.ingested), 1)

    def test_a_different_command_does_not_flip(self):
        capture.on_failure(fail(command=self.CMD, error="AssertionError: x"))
        capture.on_success(ok(command="python3 -m pytest tests/test_other.py"))
        self.assertEqual(self.fk.ingested, [])

    def test_non_check_command_never_flips(self):
        capture.on_failure(fail(command="cat /nope", error="FileNotFoundError: x"))
        capture.on_success(ok(command="cat /nope"))
        self.assertEqual(self.fk.ingested, [])

    def test_red_expires(self):
        capture.on_failure(fail(command=self.CMD, error="AssertionError: x"))
        with mock.patch.object(capture.time, "time", return_value=capture.time.time() + 1000):
            capture.on_success(ok(command=self.CMD))
        self.assertEqual(self.fk.ingested, [])

    def test_unrelated_success_touches_nothing(self):
        self.assertIsNone(capture.on_success(ok(command="ls")))
        self.assertFalse(os.path.exists(str(paths.state_file(capture.STATE_NAME))))


class TestSession(Base):
    def test_block_is_empty_when_graph_is_empty(self):
        self.assertEqual(session.build([], [], self.fk.config), "")

    def test_five_nodes_at_most_with_seventy_char_previews(self):
        nodes = [node(i, "failure", "n%d " % i + "w" * 300) for i in range(1, 10)]
        text = session.build([], nodes, self.fk.config)
        node_lines = [x for x in text.splitlines() if x.startswith("- [")]
        self.assertEqual(len(node_lines), 5)
        for ln in node_lines:
            self.assertLessEqual(len(ln.split("] ", 1)[1]), 70)

    def test_all_constraints_are_listed_and_not_counted_against_the_node_cap(self):
        cons = [node(100 + i, "constraint", "never do thing %d" % i) for i in range(4)]
        nodes = [node(i) for i in range(1, 8)]
        text = session.build(cons, nodes, self.fk.config)
        lines = [x for x in text.splitlines() if x.startswith("- [")]
        self.assertEqual(len([x for x in lines if "constraint" in x]), 4)
        self.assertEqual(len(lines), 4 + 5)
        self.assertNotIn("too many", text)

    def test_warns_past_ten_constraints(self):
        cons = [node(100 + i, "constraint", "rule %d" % i) for i in range(11)]
        self.assertIn("too many", session.build(cons, [], self.fk.config))

    def test_constraints_in_the_node_ranking_are_not_repeated(self):
        c = node(9, "constraint", "never push")
        text = session.build([c], [c, node(1)], self.fk.config)
        self.assertEqual(text.count("#9"), 1)

    def test_previews_are_redacted(self):
        n = node(1, "env", "token sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD here")
        self.assertNotIn("sk-abcdefghijklmnop", session.build([], [n], self.fk.config))

    def test_main_emits_session_start_json(self):
        import io
        self.fk.constraints = [node(1, "constraint", "never push to upstream")]
        buf = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"cwd": "/repo", "source": "startup"}))), \
                mock.patch.object(sys, "stdout", buf), mock.patch.object(session, "_arm_watchdog", lambda s: None):
            self.assertEqual(session.main(), 0)
        body = json.loads(buf.getvalue())["hookSpecificOutput"]
        self.assertEqual(body["hookEventName"], "SessionStart")
        self.assertIn("never push to upstream", body["additionalContext"])

    def test_main_is_silent_when_off(self):
        import io
        self.fk.enabled = False
        self.fk.constraints = [node(1, "constraint", "x")]
        buf = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO("{}")), mock.patch.object(sys, "stdout", buf), \
                mock.patch.object(session, "_arm_watchdog", lambda s: None):
            self.assertEqual(session.main(), 0)
        self.assertEqual(buf.getvalue(), "")


class TestPrompt(Base):
    def _handle(self, text, session_id="s1"):
        return prompt.handle({"prompt": text, "session_id": session_id, "cwd": "/repo"})

    def test_remember_stores_an_explicit_constraint_with_no_gate_call(self):
        out = self._handle("remember: never push to upstream")
        (c,) = self.fk.ingested
        self.assertEqual((c.bucket_hint, c.source, c.text), ("constraint", "explicit",
                                                              "never push to upstream"))
        self.assertIn("remember: never push", c.provenance)
        self.assertIn("#7", out)
        self.assertEqual(self.fk.picked, [])

    def test_standing_rule_and_from_now_on_also_store(self):
        self._handle("Standing rule: use origin only")
        self._handle("ok, from now on, run the tests before committing please")
        self.assertEqual([c.text for c in self.fk.ingested],
                         ["use origin only", "run the tests before committing please"])

    def test_constraint_text_is_redacted_before_storing(self):
        self._handle("remember: key is sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD")
        self.assertNotIn("sk-abcdefghijklmnop", self.fk.ingested[0].text)

    def test_correction_offers_once_and_never_writes(self):
        first = self._handle("don't use mocks here")
        second = self._handle("never do that again")
        self.assertIn("remember:", first)
        self.assertEqual(second, "")
        self.assertEqual(self.fk.ingested, [])
        self.assertEqual(self.fk.picked, [])

    def test_offer_is_per_session(self):
        self.assertIn("remember:", self._handle("don't do that", "a"))
        self.assertIn("remember:", self._handle("don't do that", "b"))

    def test_retrieval_picks_at_most_two_lines(self):
        self.fk.top = [node(i, "fix", "fix number %d " % i + "z" * 200) for i in range(1, 6)]
        self.fk.pick_result = [1, 2, 3, 4]
        out = self._handle("please investigate why the build keeps failing on CI today")
        lines = [x for x in out.splitlines() if x.startswith("- [")]
        self.assertEqual(len(lines), 2)
        for ln in lines:
            self.assertLessEqual(len(ln.split("] ", 1)[1]), 70)
        self.assertEqual(len(self.fk.picked), 1)

    def test_silence_when_nothing_is_picked(self):
        self.fk.top = [node(1)]
        self.fk.pick_result = []
        self.assertEqual(self._handle("please investigate why the build keeps failing on CI"), "")

    def test_short_prompt_never_reaches_the_gate(self):
        self.fk.top = [node(1)]
        self.assertEqual(self._handle("fix it"), "")
        self.assertEqual(self.fk.picked, [])

    def test_empty_graph_never_reaches_the_gate(self):
        self.assertEqual(self._handle("please investigate why the build keeps failing on CI"), "")
        self.assertEqual(self.fk.picked, [])

    def test_constraints_are_not_offered_as_retrieval_candidates(self):
        self.fk.top = [node(1, "constraint", "never push"), node(2, "fix", "a fix")]
        self.fk.pick_result = [2]
        self._handle("please investigate why the build keeps failing on CI")
        (_, cands), = self.fk.picked
        self.assertEqual([c.id for c in cands], [2])

    def test_prompt_sent_to_the_gate_is_redacted(self):
        self.fk.top = [node(1)]
        self._handle("please investigate sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD in the build logs")
        self.assertNotIn("sk-abcdefghijklmnop", self.fk.picked[0][0])


class TestProcess(unittest.TestCase):
    """Real subprocess runs: fail-open, kill switches, hard time budget."""

    def _run(self, hook, stdin, env=None):
        e = dict(os.environ)
        e.update(env or {})
        return subprocess.run([sys.executable, str(HOOKS / hook)], input=stdin, capture_output=True,
                              text=True, env=e, timeout=20)

    def test_garbage_and_non_dict_input_exit_zero_silently(self):
        for hook in ("knowledge_capture.py", "knowledge_session.py", "knowledge_prompt.py"):
            for stdin in ("not json", "[]", "", "null"):
                r = self._run(hook, stdin)
                self.assertEqual((r.returncode, r.stdout), (0, ""), (hook, stdin, r.stderr))

    def test_kill_switch_env_makes_every_hook_silent(self):
        payload = json.dumps({"prompt": "remember: x", "tool_name": "Bash", "error": "boom",
                              "hook_event_name": "PostToolUseFailure"})
        for hook in ("knowledge_capture.py", "knowledge_session.py", "knowledge_prompt.py"):
            r = self._run(hook, payload, {"AIRLOCK_DISABLE": "1"})
            self.assertEqual((r.returncode, r.stdout), (0, ""), hook)

    def test_watchdog_exits_zero_when_the_budget_runs_out(self):
        runner = textwrap.dedent("""
            import importlib.util, sys, time, types
            from unittest import mock
            spec = importlib.util.spec_from_file_location("m", sys.argv[1])
            m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
            m.BUDGET_S = 0.3
            cfg = types.ModuleType("knowledge.config"); cfg.enabled = lambda: True
            pkg = types.ModuleType("knowledge"); pkg.__path__ = []
            sys.modules.update({"knowledge": pkg, "knowledge.config": cfg})
            m.handle = lambda payload: time.sleep(30)
            sys.stdin = __import__("io").StringIO('{"tool_name": "Bash"}')
            sys.exit(m.main())
        """)
        for hook in ("knowledge_capture.py", "knowledge_session.py", "knowledge_prompt.py"):
            if hook == "knowledge_session.py":
                continue  # its main() has no handle(); same watchdog code, covered by capture/prompt
            start = __import__("time").time()
            r = subprocess.run([sys.executable, "-c", runner, str(HOOKS / hook)], capture_output=True,
                               text=True, timeout=20, env=dict(os.environ))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertLess(__import__("time").time() - start, 10)

    def test_budgets_are_the_documented_ones(self):
        self.assertLessEqual(capture.BUDGET_S, 5)
        self.assertLessEqual(session.BUDGET_S, 3)
        self.assertLessEqual(prompt.BUDGET_S, 5)


if __name__ == "__main__":
    unittest.main()
