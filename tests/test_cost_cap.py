"""tiers.json `cost_cap`: sub-agents may not cost more than a reference model,
priced from the Vercel AI Gateway cache (airlock/pricing.py). Same cost passes;
anything unknown fails open."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from airlock import enforce, guards, pricing, tiers

GATEWAY = [
    {"id": "anthropic/claude-3-haiku", "pricing": {"input": "0.00000025", "output": "0.00000125"}},
    {"id": "anthropic/claude-haiku-4.5", "pricing": {"input": "0.000001", "output": "0.000005"}},
    {"id": "anthropic/claude-sonnet-4.6", "pricing": {"input": "0.000003", "output": "0.000015"}},
    {"id": "anthropic/claude-sonnet-5.5", "pricing": {"input": "0.000002", "output": "0.00001"}},
    {"id": "anthropic/claude-sonnet-5.5-fast", "pricing": {"input": "0.9", "output": "0.9"}},
    {"id": "anthropic/claude-opus-5.5", "pricing": {"input": "0.000004", "output": "0.00002"}},
    {"id": "anthropic/claude-fable-5.1", "pricing": {"input": "0.00001", "output": "0.00005"}},
    {"id": "openai/gpt-5", "pricing": {"input": "0.000001", "output": "0.00001"}},
    {"id": "anthropic/claude-opus-9", "pricing": {"input": "0.1"}},
]
CACHE = {"fetched_at": 0, "aliases": pricing.extract(GATEWAY)}


def _options(options):
    return mock.patch.object(tiers, "_options", side_effect=lambda path=None: options)


def _agent(model="", subagent_type="general-purpose"):
    return {"tool_input": {"subagent_type": subagent_type, "model": model, "description": "d", "prompt": "p"}}


class TestExtract(unittest.TestCase):
    def test_newest_non_fast_row_per_family(self):
        a = pricing.extract(GATEWAY)
        self.assertEqual(sorted(a), ["fable", "haiku", "opus", "sonnet"])
        self.assertEqual(a["sonnet"]["id"], "anthropic/claude-sonnet-5.5")
        self.assertEqual(a["haiku"]["id"], "anthropic/claude-haiku-4.5")

    def test_incomplete_and_garbage_rows_are_skipped(self):
        self.assertEqual(pricing.extract("nope"), {})
        self.assertEqual(pricing.extract([{"id": "anthropic/claude-opus-9", "pricing": {"input": "1"}}, 3]), {})

    def test_alias_price_is_input_plus_output(self):
        self.assertAlmostEqual(pricing.alias_price("haiku", CACHE), 0.000006)
        self.assertIsNone(pricing.alias_price("haiku", {"aliases": {}}))


class TestCacheFile(unittest.TestCase):
    def test_refresh_writes_and_load_reads_and_failure_keeps_old(self):
        path = os.path.join(tempfile.mkdtemp(), "p.json")
        body = io.BytesIO(json.dumps({"data": GATEWAY}).encode())
        with mock.patch.object(pricing.urllib.request, "urlopen", return_value=mock.MagicMock(
                __enter__=lambda s: body, __exit__=lambda *a: False)):
            self.assertTrue(pricing.refresh(path, now=100.0))
        self.assertEqual(pricing.load(path)["fetched_at"], 100.0)
        with mock.patch.object(pricing.urllib.request, "urlopen", side_effect=OSError):
            self.assertIsNone(pricing.refresh(path))
        self.assertEqual(pricing.load(path)["fetched_at"], 100.0)

    def test_missing_or_corrupt_cache_is_none_and_stale(self):
        path = os.path.join(tempfile.mkdtemp(), "p.json")
        self.assertIsNone(pricing.load(path))
        with open(path, "w") as f:
            f.write("{")
        self.assertIsNone(pricing.load(path))
        self.assertTrue(pricing.is_stale(None))

    def test_staleness_window(self):
        self.assertFalse(pricing.is_stale({"fetched_at": 1000}, now=1000 + 3600))
        self.assertTrue(pricing.is_stale({"fetched_at": 1000}, now=1000 + 86400))

    def test_async_refresh_only_when_stale_and_not_just_tried(self):
        state = tempfile.mkdtemp()
        with mock.patch.object(pricing.paths, "state_file", side_effect=lambda n: os.path.join(state, n)), \
                mock.patch.object(pricing.subprocess, "Popen") as popen:
            self.assertFalse(pricing.maybe_refresh_async({"fetched_at": 1000}, now=2000))
            self.assertTrue(pricing.maybe_refresh_async(None, now=2000))
            self.assertFalse(pricing.maybe_refresh_async(None, now=2100))
            self.assertTrue(pricing.maybe_refresh_async(None, now=2000 + pricing.RETRY_AFTER_S + 1))
            self.assertEqual(popen.call_count, 2)


class TestCostCapReference(unittest.TestCase):
    def setUp(self):
        tiers.reset_cache()
        self.addCleanup(tiers.reset_cache)

    def test_true_uses_inherit_model(self):
        with _options({"cost_cap": True, "inherit_model": "opus"}):
            self.assertEqual(tiers.cost_cap_reference(), "opus")

    def test_alias_string_wins(self):
        with _options({"cost_cap": "sonnet", "inherit_model": "opus"}):
            self.assertEqual(tiers.cost_cap_reference(), "sonnet")

    def test_off_malformed_or_unanchored_is_none(self):
        for opts in ({}, {"cost_cap": False}, {"cost_cap": "gpt-5"}, {"cost_cap": 1}, {"cost_cap": True}):
            with _options(opts):
                self.assertIsNone(tiers.cost_cap_reference(), opts)


class TestViolation(unittest.TestCase):
    def setUp(self):
        tiers.reset_cache()
        self.addCleanup(tiers.reset_cache)
        for p in (mock.patch.object(pricing, "load", return_value=CACHE),
                  mock.patch.object(pricing, "maybe_refresh_async")):
            p.start()
            self.addCleanup(p.stop)

    def _v(self, data, cap="sonnet", **opts):
        with _options(dict(opts, cost_cap=cap)):
            return guards.cost_cap_violation(data)

    def test_dearer_model_is_a_violation_and_lists_allowed(self):
        v = self._v(_agent("opus"))
        self.assertEqual((v["chosen_model"], v["cap_model"]), ("opus", "sonnet"))
        self.assertEqual(v["allowed_models"], ["haiku", "sonnet"])

    def test_same_cost_and_cheaper_pass(self):
        self.assertIsNone(self._v(_agent("sonnet")))
        self.assertIsNone(self._v(_agent("haiku")))

    def test_fixed_model_type_is_judged(self):
        self.assertIsNone(self._v(_agent("", "Explore"), cap="haiku"))
        self.assertIsNotNone(self._v(_agent("", "Explore"), cap="haiku", type_models={"Explore": "opus"}))

    def test_inherited_general_type_over_cap_is_caught(self):
        self.assertIsNotNone(self._v(_agent(""), cap="sonnet", inherit_model="fable"))

    def test_unknown_model_fails_open(self):
        self.assertIsNone(self._v(_agent("")))
        self.assertIsNone(self._v(_agent("", "house-agent")))

    def test_no_prices_fails_open(self):
        with mock.patch.object(pricing, "load", return_value=None):
            self.assertIsNone(self._v(_agent("fable")))

    def test_cap_off_is_none(self):
        with _options({}):
            self.assertIsNone(guards.cost_cap_violation(_agent("fable")))


class TestHook(unittest.TestCase):
    def setUp(self):
        tiers.reset_cache()
        self.addCleanup(tiers.reset_cache)
        for p in (mock.patch.object(pricing, "load", return_value=CACHE),
                  mock.patch.object(pricing, "maybe_refresh_async"),
                  mock.patch.object(enforce.log, "append")):
            p.start()
            self.addCleanup(p.stop)

    def _run(self, data, mode="enforce"):
        out = io.StringIO()
        with _options({"cost_cap": "sonnet"}), redirect_stdout(out):
            denied = enforce._handle(data, "Agent", mode)
        return denied, out.getvalue()

    def test_enforce_denies_with_the_allowed_models(self):
        denied, out = self._run(_agent("fable"))
        self.assertTrue(denied)
        hso = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn("haiku, sonnet", hso["permissionDecisionReason"])

    def test_shadow_logs_but_never_denies(self):
        denied, out = self._run(_agent("fable"), mode="shadow")
        self.assertFalse(denied)
        self.assertEqual(out, "")
        self.assertFalse(enforce.log.append.call_args[0][0]["enforced"])

    def test_within_cap_is_untouched(self):
        with mock.patch.object(enforce.rules_mod, "prefilter_matches", return_value=[]):
            denied, out = self._run(_agent("sonnet"))
        self.assertFalse(denied)
        self.assertEqual(out, "")

    def test_other_tools_are_ignored(self):
        self.assertFalse(enforce._cost_cap(_agent("fable"), "Bash", "enforce", {}))


if __name__ == "__main__":
    unittest.main()
