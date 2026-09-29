"""harness/: the Jev-decided gate loop. Fully mocked: no key, no network, no
claude CLI, no make."""
import tests  # noqa: F401, I001 -- first import: isolates HOME and AIRLOCK_* (see tests/__init__.py)

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import bench, decide, gates, loop


def _report(*failed):
    return {"ok": not failed, "failed": list(failed),
            "gates": [{"name": n, "ok": False, "output": "%s broke" % n} for n in failed]}


def _ask(per_gate, same=0.0, tokens=500):
    """per_gate: {gate: (fix_kind, model, confidence)}."""
    def ask(body, timeout_s=None):
        answers = {}
        for key in body["questions"]:
            kind, _, gate = key.partition("::")
            fix_kind, model, conf = per_gate.get(gate.replace("_", "-"), per_gate.get(gate))
            if kind == "fix_kind":
                answers[key] = {"type": "choice", "choice": fix_kind, "confidence": conf}
            elif kind == "model":
                answers[key] = {"type": "choice", "choice": model, "confidence": conf}
            else:
                answers[key] = {"type": "noul", "noul": same}
        return {"answers": answers, "usage": {"input_tokens": tokens}}, 30
    return ask


class TestGates(unittest.TestCase):
    def test_reads_the_report(self):
        repo = Path(tempfile.mkdtemp())
        (repo / ".agent").mkdir()
        (repo / ".agent" / "report.json").write_text(json.dumps(_report("lint")))
        with mock.patch("harness.gates.subprocess.run"):
            report, _ = gates.run(repo)
        self.assertEqual(gates.failed_gates(report), [("lint", "lint broke")])

    def test_a_missing_report_is_a_failure(self):
        with mock.patch("harness.gates.subprocess.run"):
            report, _ = gates.run(Path(tempfile.mkdtemp()))
        self.assertFalse(report["ok"])


class TestDecide(unittest.TestCase):
    def test_one_request_three_questions_per_gate_from_round_two(self):
        failed = [("format-check", "x"), ("lint", "y")]
        first = decide.questions(failed, {})
        self.assertEqual(sorted(first), ["fix_kind::format_check", "fix_kind::lint",
                                         "model::format_check", "model::lint"])
        again = decide.questions(failed, {"lint": "old"})
        self.assertIn("same::lint", again)
        self.assertNotIn("same::format_check", again)

    def test_answers_map_back_to_gate_names_with_a_cost(self):
        out = decide.decide([("format-check", "x")], {},
                            _ask({"format_check": ("autofix", "haiku", 0.9)}, tokens=1000))
        self.assertEqual(out["format-check"]["fix_kind"], "autofix")
        self.assertAlmostEqual(out["_usage"]["cost_usd"], 1000 * decide.PRICE_PER_MTOK_USD / 1e6)


class LoopBase(unittest.TestCase):
    def _run(self, reports, ask, claude_cost=0.02):
        self.claude_calls = []

        def fake_claude(prompt, cwd, model, tools, budget, disallowed_tools=()):
            self.claude_calls.append({"model": model, "prompt": prompt, "tools": tools,
                                      "disallowed": disallowed_tools})
            return {"ok": True, "cost_usd": claude_cost}
        seq = iter(reports)
        with mock.patch("harness.gates.run", side_effect=lambda repo: (next(seq), 1.0)), \
             mock.patch("harness.claude_cli.run", side_effect=fake_claude), \
             mock.patch("harness.loop._run_fixers", return_value=["ruff"]) as fixers:
            result = loop.run("/repo", ask, max_rounds=5, log=lambda *_: None)
        self.fixers = fixers
        return result


class TestLoop(LoopBase):
    def test_fixers_first_then_one_cheap_edit_then_green(self):
        ask = _ask({"format-check": ("autofix", "haiku", 0.9),
                    "typecheck": ("code_edit", "haiku", 0.9)})
        result = self._run([_report("format-check", "typecheck"), _report("typecheck"),
                            _report()], ask)
        self.assertEqual(result["outcome"], "green")
        self.assertEqual(self.fixers.call_count, 1)
        self.assertEqual(len(self.claude_calls), 1)
        call = self.claude_calls[0]
        self.assertEqual(call["model"], "haiku")
        self.assertIn("typecheck", call["prompt"])
        self.assertEqual(call["disallowed"], ("Bash",))
        self.assertNotIn("Bash", call["tools"])

    def test_sonnet_when_any_gate_needs_it(self):
        ask = _ask({"typecheck": ("code_edit", "haiku", 0.9),
                    "dup": ("code_edit", "sonnet", 0.9)})
        self._run([_report("typecheck", "dup"), _report()], ask)
        self.assertEqual(self.claude_calls[0]["model"], "sonnet")

    def test_a_person_blocker_stops_without_claude(self):
        ask = _ask({"coverage-ratchet": ("needs_person", "sonnet", 0.9)})
        result = self._run([_report("coverage-ratchet")], ask)
        self.assertTrue(result["outcome"].startswith("needs_person"))
        self.assertEqual(self.claude_calls, [])

    def test_a_weak_blocker_does_not_stop(self):
        ask = _ask({"typecheck": ("environment", "haiku", 0.5)})
        self._run([_report("typecheck"), _report()], ask)
        self.assertEqual(len(self.claude_calls), 1)

    def test_the_same_failure_twice_more_is_stuck(self):
        ask = _ask({"typecheck": ("code_edit", "haiku", 0.9)}, same=0.95)
        result = self._run([_report("typecheck")] * 5, ask)
        self.assertTrue(result["outcome"].startswith("stuck"))
        self.assertEqual(len(self.claude_calls), 2)

    def test_budget_stops_the_loop(self):
        ask = _ask({"typecheck": ("code_edit", "haiku", 0.9)})
        result = self._run([_report("typecheck")] * 5, ask, claude_cost=1.99)
        self.assertEqual(result["outcome"], "budget")


class TestSeed(unittest.TestCase):
    def test_a_seed_that_does_not_apply_fails_loudly(self):
        source = Path(tempfile.mkdtemp())
        (source / "a.py").write_text("x = 1\n")
        with mock.patch("harness.bench.subprocess.run") as run:
            run.return_value.stdout = b""
            with mock.patch("harness.bench.Path.read_text", return_value="y = 2\n"):
                with self.assertRaises(SystemExit):
                    bench.prepare(source, [{"file": "a.py", "find": "x = 1", "replace": "x=1"}],
                                  tempfile.mkdtemp())


if __name__ == "__main__":
    unittest.main()
