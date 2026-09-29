"""The model ladder of the Agent tier guard: a dispatch whose model is known
in code (an explicit `model`, a type with a fixed model, or a machine's
inherit_model) is judged by model, and the advice names a cheaper `model`
rather than an agent type the machine may not have.

Hook-level tests go through enforce.handle() with a faked Jev answer, the
same harness as test_tier_surface.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from airlock import policy, tiers
from test_tier_surface import RewriteOnBase, TierSurfaceBase, _agent_data, _answer, _hso


def _options(options):
    return mock.patch.object(tiers, "_options", side_effect=lambda path=None: options)


class TestModelName(unittest.TestCase):
    def test_aliases_and_full_ids_resolve(self):
        self.assertEqual(tiers.model_name("haiku"), "haiku")
        self.assertEqual(tiers.model_name("Sonnet"), "sonnet")
        self.assertEqual(tiers.model_name("claude-haiku-4-5-20251001"), "haiku")
        self.assertEqual(tiers.model_name("opus[1m]"), "opus")
        self.assertEqual(tiers.model_name("claude-fable-5-1"), "fable")

    def test_nothing_known_gives_none(self):
        for value in ("", "inherit", "gpt-5", None, 3, ["haiku"]):
            self.assertIsNone(tiers.model_name(value), value)


class TestEffectiveModel(unittest.TestCase):
    def setUp(self):
        tiers.reset_cache()
        self.addCleanup(tiers.reset_cache)

    def test_explicit_override_wins_over_everything(self):
        with _options({"inherit_model": "opus", "type_models": {"Explore": "sonnet"}}):
            self.assertEqual(tiers.effective_model("Explore", "haiku"), "haiku")
            self.assertEqual(tiers.effective_model("workerO", "sonnet"), "sonnet")

    def test_explore_runs_on_haiku_by_default(self):
        with _options({}):
            self.assertEqual(tiers.effective_model("Explore", ""), "haiku")

    def test_general_types_stay_on_the_agent_ladder_without_inherit_model(self):
        with _options({}):
            for t in ("", "claude", "general-purpose", "Plan"):
                self.assertIsNone(tiers.effective_model(t, ""), t)

    def test_inherit_model_applies_to_general_types_only(self):
        with _options({"inherit_model": "opus"}):
            self.assertEqual(tiers.effective_model("general-purpose", ""), "opus")
            self.assertEqual(tiers.effective_model("", ""), "opus")
            self.assertIsNone(tiers.effective_model("workerS", ""))
            self.assertIsNone(tiers.effective_model("fable", ""))

    def test_type_models_adds_and_bad_entries_are_ignored(self):
        with _options({"type_models": {"my-reader": "haiku", "bad": "gpt-5", 7: "haiku"}}):
            self.assertEqual(tiers.effective_model("my-reader", ""), "haiku")
            self.assertIsNone(tiers.effective_model("bad", ""))

    def test_object_config_on_disk_is_read(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "tiers.json"
        p.write_text(json.dumps({"inherit_model": "sonnet"}))
        self.assertEqual(tiers.effective_model("claude", "", str(p)), "sonnet")
        self.assertEqual(tiers.load_ladder(str(p)), [list(r) for r in tiers.DEFAULT_LADDER])


class TestModelVerdict(unittest.TestCase):
    def setUp(self):
        tiers.reset_cache()
        self.addCleanup(tiers.reset_cache)
        p = _options({})
        p.start()
        self.addCleanup(p.stop)

    def _verdict(self, task_kind, subagent_type, model, prior_failed=0.0):
        return policy.evaluate_tier(task_kind, 1.0, prior_failed, subagent_type,
                                    task_kind_margin=1.0, model_override=model)

    def test_opus_for_a_lookup_is_two_models_over(self):
        v = self._verdict("lookup", "general-purpose", "opus")
        self.assertEqual(v["ladder"], "model")
        self.assertEqual(v["chosen_model"], "opus")
        self.assertEqual(v["adequate_rung"], "haiku")
        self.assertEqual(v["rung_diff"], 2)
        self.assertTrue(v["would_deny"])

    def test_the_right_model_is_silent(self):
        v = self._verdict("scoped_implementation", "general-purpose", "sonnet")
        self.assertEqual(v["rung_diff"], 0)
        self.assertFalse(v["would_deny"])
        self.assertFalse(v["under_tiered"])

    def test_too_cheap_is_logged_never_denied(self):
        v = self._verdict("judgement", "general-purpose", "haiku")
        self.assertTrue(v["under_tiered"])
        self.assertFalse(v["would_deny"])

    def test_no_model_known_keeps_the_agent_ladder(self):
        v = self._verdict("lookup", "claude", "")
        self.assertEqual(v["ladder"], "agent")
        self.assertIsNone(v["chosen_model"])
        self.assertEqual(v["adequate_rung"], "scout-find")

    def test_fable_is_the_model_not_the_type_name(self):
        self.assertTrue(self._verdict("hard_problem", "general-purpose", "fable")["would_deny"])
        self.assertFalse(self._verdict("hard_problem", "general-purpose", "fable",
                                       prior_failed=0.9)["would_deny"])
        v = self._verdict("lookup", "fable", "haiku")
        self.assertFalse(v["would_deny"])

    def test_deny_possible_follows_the_model(self):
        self.assertFalse(policy.deny_possible_agent("Explore"))
        self.assertFalse(policy.deny_possible_agent("general-purpose", "haiku"))
        self.assertTrue(policy.deny_possible_agent("general-purpose", "sonnet"))
        self.assertTrue(policy.deny_possible_agent("claude"))
        self.assertFalse(policy.deny_possible_agent("scout-find"))

    def test_model_rewrite_target_only_goes_down(self):
        base = {"ladder": "model", "chosen_type": "general-purpose", "rung_diff": 1,
                "task_kind": "mechanical_edit", "task_kind_confidence": 1.0, "margin": 1.0}
        self.assertEqual(policy.tier_rewrite_target(
            dict(base, chosen_model="sonnet", suggestion="haiku")), "haiku")
        self.assertIsNone(policy.tier_rewrite_target(
            dict(base, chosen_model="haiku", suggestion="sonnet")))
        self.assertIsNone(policy.tier_rewrite_target(
            dict(base, chosen_model="sonnet", suggestion="gpt-5")))


class ModelHookBase(TierSurfaceBase):
    def setUp(self):
        super().setUp()
        p = _options({})
        p.start()
        self.addCleanup(p.stop)


class TestModelAdvice(ModelHookBase):
    def test_one_model_over_warns_with_a_model_to_set(self):
        denied, out = self._run(
            _agent_data("general-purpose", "rename", "Rename foo to bar in utils.py.",
                        extra={"model": "sonnet"}),
            _answer("mechanical_edit"),
        )
        self.assertFalse(denied)
        text = _hso(out)["additionalContext"]
        self.assertIn("subagent_type=general-purpose with model=haiku", text)
        self.assertIn("general-purpose on sonnet", text)
        self.assertIn("nothing was blocked", text)
        self.assertEqual(self._logged[-1]["ladder"], "model")

    def test_two_models_over_blocks_with_a_model_to_set(self):
        denied, out = self._run(
            _agent_data("general-purpose", "find it", "Where is AIRLOCK_DISABLE checked?",
                        extra={"model": "opus"}),
            _answer("lookup", prior_failed=0.9),
        )
        self.assertTrue(denied)
        hso = _hso(out)
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn("model=haiku", hso["permissionDecisionReason"])

    def test_explore_is_never_sent_to_jev(self):
        with mock.patch("airlock.guards.random.random", return_value=1.0):
            denied, out = self._run(
                _agent_data("Explore", "find it", "Where is AIRLOCK_DISABLE checked?"),
                _answer("hard_problem"),
            )
        self.assertFalse(denied)
        self.assertIsNone(out)
        self.assertEqual(self._logged[-1].get("skipped"), "no_deny_possible")
        self.assertIn("'haiku'", self._logged[-1]["detail"])


class TestModelRewrite(RewriteOnBase):
    def setUp(self):
        super().setUp()
        p = _options({})
        p.start()
        self.addCleanup(p.stop)

    def test_rewrite_sets_model_and_leaves_the_type(self):
        data = _agent_data("general-purpose", "rename", "Rename foo to bar in utils.py.",
                           extra={"model": "sonnet", "run_in_background": True})
        original = json.loads(json.dumps(data["tool_input"]))
        denied, out = self._run(data, _answer("mechanical_edit"))
        self.assertTrue(denied)
        updated = _hso(out)["updatedInput"]
        self.assertEqual(updated["model"], "haiku")
        for k, v in original.items():
            if k != "model":
                self.assertEqual(updated[k], v, k)
        self.assertIn("model changed from 'sonnet' to 'haiku'", _hso(out)["additionalContext"])
        entry = self._logged[-1]
        self.assertEqual(entry["rewrote_field"], "model")
        self.assertEqual(entry["rewrote_from"], "sonnet")
        self.assertEqual(entry["rewrote_to"], "haiku")


if __name__ == "__main__":
    unittest.main()
