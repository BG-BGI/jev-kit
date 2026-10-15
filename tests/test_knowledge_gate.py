"""knowledge/gate.py: the Jev gate, with a fake `ask`. No network."""
import unittest
from unittest import mock

from knowledge import config, gate
from knowledge.types import Candidate, Node


def node(i, text="t", bucket="failure", evidence=1):
    return Node(i, bucket, text, "machine", None, "", (), evidence, 0.0, 0.0, None, "")


def choice(pick, conf=0.95, other=0.02):
    return {"choice": pick, "confidence": conf,
            "probabilities": {pick: conf, "x": other}}


def noul(p):
    return {"noul": p}


def answers(store=0.97, bucket="failure", novelty="new", target=None, promote=0.02,
            dur=2, scope="machine"):
    a = {"store": noul(store), "bucket": choice(bucket), "novelty": choice(novelty),
         "durability": {"score": dur, "confidence": 0.95, "probabilities": {"2": 0.95, "1": 0.02}},
         "scope": choice(scope), "promote": noul(promote)}
    if target:
        a["target"] = choice(target)
    return {"answers": a, "usage": {"input_tokens": 40}}


def fake(response=None, exc=None):
    calls = []

    def ask(body, timeout_s=None):
        calls.append((body, timeout_s))
        if exc:
            raise exc
        return response, 120
    ask.calls = calls
    return ask


class JudgeTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(gate, "log_event")
        self.log = p.start()
        self.addCleanup(p.stop)
        self.c = Candidate("failure", "KeyError: 'x'", tool="Bash", scope="repo:/r")

    def actions(self):
        return [call.args[0] for call in self.log.call_args_list]

    def test_confident_new(self):
        ask = fake(answers())
        v = gate.judge(self.c, [], ask=ask)
        self.assertTrue(v.store)
        self.assertEqual((v.bucket, v.novelty, v.confident), ("failure", "new", True))
        self.assertEqual(v.durability, 1.0)
        self.assertEqual(ask.calls[0][1], config.GATE_TIMEOUT_S)
        self.assertEqual(self.actions(), ["gate"])
        self.assertEqual(self.log.call_args.kwargs["latency_ms"], 120)

    def test_one_call_has_all_questions(self):
        ask = fake(answers())
        gate.judge(self.c, [node(1), node(2)], ask=ask)
        qs = ask.calls[0][0]["questions"]
        self.assertEqual(set(qs), {"store", "bucket", "novelty", "target", "durability",
                                   "scope", "promote"})
        self.assertEqual(set(qs["bucket"]["criteria"]), set(config.BUCKETS))

    def test_redacts_candidate_and_neighbours(self):
        secret = "ghp_" + "a" * 30
        c = Candidate("failure", "auth failed with %s" % secret)
        ask = fake(answers())
        gate.judge(c, [node(1, "old %s" % secret)], ask=ask)
        ctx = ask.calls[0][0]["state"]["context"]
        self.assertNotIn(secret, ctx)
        self.assertIn("[REDACTED]", ctx)

    def test_timeout_returns_none_and_logs(self):
        v = gate.judge(self.c, [], ask=fake(exc=TimeoutError("timed out")))
        self.assertIsNone(v)
        self.assertEqual(self.actions(), ["gate_timeout"])

    def test_error_returns_none_and_logs(self):
        v = gate.judge(self.c, [], ask=fake(exc=RuntimeError("boom")))
        self.assertIsNone(v)
        self.assertEqual(self.actions(), ["gate_error"])

    def test_below_bar_store_is_none(self):
        v = gate.judge(self.c, [], ask=fake(answers(store=0.6)))
        self.assertIsNone(v)
        self.assertEqual(self.actions(), ["gate_below_bar"])

    def test_confident_no_is_a_verdict(self):
        v = gate.judge(self.c, [], ask=fake(answers(store=0.01)))
        self.assertFalse(v.store)

    def test_low_confidence_bucket_uses_hint(self):
        r = answers()
        r["answers"]["bucket"] = choice("fix", conf=0.5)
        v = gate.judge(self.c, [], ask=fake(r))
        self.assertEqual(v.bucket, "failure")

    def test_low_confidence_bucket_without_hint_is_none(self):
        r = answers()
        r["answers"]["bucket"] = choice("fix", conf=0.5)
        c = Candidate(None, "x")
        self.assertIsNone(gate.judge(c, [], ask=fake(r)))

    def test_low_margin_novelty_is_none(self):
        r = answers()
        r["answers"]["novelty"] = choice("duplicate", conf=0.85, other=0.6)
        self.assertIsNone(gate.judge(self.c, [node(1)], ask=fake(r)))

    def test_duplicate_single_neighbour_targets_it(self):
        v = gate.judge(self.c, [node(7)], ask=fake(answers(novelty="duplicate")))
        self.assertEqual((v.novelty, v.target), ("duplicate", 7))

    def test_target_picks_neighbour(self):
        v = gate.judge(self.c, [node(7), node(9)],
                       ask=fake(answers(novelty="refines", target="n2")))
        self.assertEqual(v.target, 9)

    def test_unsure_target_is_none(self):
        r = answers(novelty="refines", target="n2")
        r["answers"]["target"] = choice("n2", conf=0.4)
        self.assertIsNone(gate.judge(self.c, [node(7), node(9)], ask=fake(r)))

    def test_duplicate_without_neighbours_is_new(self):
        v = gate.judge(self.c, [], ask=fake(answers(novelty="duplicate")))
        self.assertEqual((v.novelty, v.target), ("new", None))

    def test_promote_needs_confidence(self):
        self.assertTrue(gate.judge(self.c, [], ask=fake(answers(promote=0.95))).promote)
        self.assertFalse(gate.judge(self.c, [], ask=fake(answers(promote=0.7))).promote)

    def test_scope_mapping(self):
        v = gate.judge(self.c, [], ask=fake(answers(scope="repo")))
        self.assertEqual(v.scope, "repo:/r")
        c = Candidate("failure", "x", scope="machine")
        self.assertEqual(gate.judge(c, [], ask=fake(answers(scope="repo"))).scope, "machine")
        self.assertEqual(gate.judge(c, [], ask=fake(answers(scope="global"))).scope, "global")

    def test_session_only_durability_zero(self):
        v = gate.judge(self.c, [], ask=fake(answers(dur=0)))
        self.assertEqual(v.durability, 0.0)

    def test_garbage_response_fails_open(self):
        self.assertIsNone(gate.judge(self.c, [], ask=fake({"answers": {"store": "junk"}})))
        self.assertIsNone(gate.judge(self.c, [], ask=fake(None)))


class PickTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(gate, "log_event")
        self.log = p.start()
        self.addCleanup(p.stop)
        self.nodes = [node(1, "a"), node(2, "b"), node(3, "c")]

    def resp(self, fits, winner="n1"):
        ans = {"which": {"choice": winner}}
        for k, v in fits.items():
            ans["fits::" + k] = noul(v)
        return {"answers": ans}

    def test_silence_default(self):
        ask = fake(self.resp({"n1": 0.2, "n2": 0.1, "n3": 0.3}))
        self.assertEqual(gate.pick_for_prompt("fix the build", self.nodes, ask=ask), [])
        self.assertEqual(self.log.call_args.args[0], "prompt_silent")

    def test_caps_at_prompt_nodes_winner_first(self):
        ask = fake(self.resp({"n1": 0.6, "n2": 0.9, "n3": 0.8}, winner="n3"))
        got = gate.pick_for_prompt("p", self.nodes, ask=ask)
        self.assertEqual(len(got), config.PROMPT_NODES)
        self.assertEqual(got[0], 3)

    def test_error_and_timeout_are_empty(self):
        self.assertEqual(gate.pick_for_prompt("p", self.nodes, ask=fake(exc=OSError("x"))), [])
        self.assertEqual(gate.pick_for_prompt(
            "p", self.nodes, ask=fake(exc=TimeoutError("timed out"))), [])
        self.assertEqual(self.log.call_args.args[0], "prompt_timeout")

    def test_no_candidates_no_call(self):
        ask = fake({})
        self.assertEqual(gate.pick_for_prompt("p", [], ask=ask), [])
        self.assertEqual(ask.calls, [])

    def test_prompt_redacted(self):
        ask = fake(self.resp({}))
        gate.pick_for_prompt("use ghp_" + "b" * 30, self.nodes, ask=ask)
        self.assertNotIn("ghp_", ask.calls[0][0]["state"]["request"])


class LogEventTests(unittest.TestCase):
    def test_log_event_writes_knowledge_row(self):
        with mock.patch.object(gate.log, "append") as app:
            gate.log_event("gate", latency_ms=5)
        entry = app.call_args.args[0]
        self.assertEqual((entry["guard"], entry["kind"], entry["action"]),
                         ("knowledge", "knowledge", "gate"))

    def test_log_event_never_raises(self):
        with mock.patch.object(gate.log, "append", side_effect=OSError):
            gate.log_event("gate")


if __name__ == "__main__":
    unittest.main()
