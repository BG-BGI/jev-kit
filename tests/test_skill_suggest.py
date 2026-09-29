"""The skill-suggest component: roster discovery, the two-request recipe, and
the UserPromptSubmit hook. Fully mocked: no key, no network."""
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from suggest import roster, suggest

HOOK_PATH = Path(__file__).resolve().parent.parent / "hooks" / "airlock_skill_suggest.py"


def _load_hook():
    spec = importlib.util.spec_from_file_location("airlock_skill_suggest", str(HOOK_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _skill(name, description="", body="body"):
    return {"name": name, "description": description, "body": body}


ROSTER = [
    _skill("ros2", "ROS 2 nodes, launch files and colcon."),
    _skill("pptx", "Create PowerPoint decks."),
    _skill("zebra", "Print labels on the Zebra printer."),
    _skill("tdd", "Red-green-refactor."),
]


class FakeAsk:
    """Answers request 1 with `ranking` and `gate`, request 2 with `winner`
    and `fits`, and records every body it was sent."""

    def __init__(self, ranking=None, gate=(0.9, 0.9, 0.1), winner=None, fits=None):
        self.ranking = ranking or {}
        self.gate = gate
        self.winner = winner
        self.fits = fits or {}
        self.bodies = []

    def __call__(self, body, timeout_s=None):
        self.bodies.append(body)
        qs = body["questions"]
        answers = {}
        if any(k.startswith("which::") for k in qs):
            keys = ("acts_on_user_system", "would_follow_documented_procedure", "prose_suffices")
            for key, value in zip(keys, self.gate):
                answers["gate::%s" % key] = {"type": "noul", "noul": value}
            for key, q in qs.items():
                if key.startswith("which::"):
                    answers[key] = {"type": "choice", "probabilities": {
                        n: self.ranking.get(n, 0.0) for n in q["criteria"]}}
        else:
            answers["which"] = {"type": "choice", "choice": self.winner}
            for key in qs:
                if key.startswith("fits::"):
                    answers[key] = {"type": "noul", "noul": self.fits.get(key[6:], 0.0)}
        return {"answers": answers, "usage": {"input_tokens": 100, "output_tokens": 5}}, 42


class TestFrontmatter(unittest.TestCase):
    def test_plain_quoted_and_block_values(self):
        text = ("---\nname: x\ndescription: \"Quoted: with colon\"\nother: >\n  folded\n"
                "  lines\nlit: |\n  a\n  b\n---\n# Body\ntext")
        fields, body = roster.parse_frontmatter(text)
        self.assertEqual(fields["description"], "Quoted: with colon")
        self.assertEqual(fields["other"], "folded lines")
        self.assertEqual(fields["lit"], "a\nb")
        self.assertIn("text", body)

    def test_no_frontmatter_is_all_body(self):
        self.assertEqual(roster.parse_frontmatter("just text"), ({}, "just text"))


class TestDiskRoster(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.cwd = Path(tempfile.mkdtemp())

    def test_user_project_and_plugin_skills(self):
        _write(self.home / ".claude/skills/alpha/SKILL.md", "---\ndescription: user alpha\n---\nA")
        _write(self.home / ".claude/skills/hidden/SKILL.md",
               "---\ndescription: h\ndisable-model-invocation: true\n---\n")
        _write(self.cwd / ".claude/skills/alpha/SKILL.md", "---\ndescription: project alpha\n---\n")
        plugin = self.home / "plugins/cache/mk/tool/1.0"
        _write(plugin / "SKILL.md", "---\ndescription: root skill\n---\n")
        _write(plugin / "skills/sub/SKILL.md", "---\ndescription: sub skill\n---\n")
        _write(self.home / ".claude/plugins/installed_plugins.json", json.dumps(
            {"plugins": {"tool@mk": [{"installPath": str(plugin)}]}}))
        found = {s["name"]: s["description"] for s in roster.discover(str(self.cwd), str(self.home))}
        self.assertEqual(found, {"alpha": "project alpha", "tool:tool": "root skill",
                                 "tool:sub": "sub skill"})

    def test_nothing_on_disk_is_empty_not_an_error(self):
        self.assertEqual(roster.discover(None, str(self.home)), [])


class TestTranscriptRoster(unittest.TestCase):
    def _transcript(self, *attachments):
        path = Path(tempfile.mkdtemp()) / "t.jsonl"
        lines = [json.dumps({"type": "user", "message": "skill_listing mentioned in passing"})]
        lines += [json.dumps({"type": "attachment", "attachment": a}) for a in attachments]
        lines.append("not json skill_listing")
        path.write_text("\n".join(lines))
        return str(path)

    def test_listing_names_and_descriptions(self):
        path = self._transcript(
            {"type": "skill_listing", "content": "- alpha\n- beta: does beta: really\n- org:pptx",
             "names": ["alpha", "beta", "org:pptx", "gamma"]},
            {"type": "skill_listing", "content": "- alpha: now described", "names": ["alpha"]},
        )
        self.assertEqual(roster.from_transcript(path), {
            "alpha": "now described", "beta": "does beta: really", "org:pptx": "", "gamma": ""})

    def test_listing_is_the_roster_and_disk_describes_it(self):
        path = self._transcript({"type": "skill_listing", "content": "- alpha: short\n- org:pptx",
                                 "names": []})
        disk = [_skill("alpha", "the full alpha description", "alpha body"),
                _skill("only-on-disk", "never listed")]
        merged = roster.merge(roster.from_transcript(path), disk)
        self.assertEqual([s["name"] for s in merged], ["alpha", "org:pptx"])
        self.assertEqual(merged[0]["description"], "the full alpha description")
        self.assertEqual(merged[0]["body"], "alpha body")

    def test_missing_transcript_falls_back_to_disk(self):
        self.assertEqual(roster.from_transcript("/nonexistent/t.jsonl"), {})
        self.assertEqual(roster.from_transcript(None), {})


class TestSkipReason(unittest.TestCase):
    def test_code_only_skips(self):
        self.assertEqual(suggest.skip_reason("/commit"), "slash_command")
        self.assertEqual(suggest.skip_reason("! ls -la"), "shell")
        self.assertEqual(suggest.skip_reason("commit it"), "too_short")
        self.assertEqual(suggest.skip_reason("   "), "empty")
        self.assertEqual(suggest.skip_reason("<command-name>/plan</command-name> x y z"),
                         "slash_command")
        self.assertIsNone(suggest.skip_reason("write a launch file for the lidar"))


class TestRecipe(unittest.TestCase):
    def test_suggests_the_rerank_winner(self):
        ask = FakeAsk(ranking={"ros2": 0.8, "tdd": 0.15, "zebra": 0.05},
                      winner="ros2", fits={"ros2": 0.9})
        result = suggest.suggest("write a launch file", ROSTER, ask)
        self.assertEqual(result["skill"], "ros2")
        self.assertEqual(result["reason"], "suggested")
        self.assertEqual(result["shortlist"], ["ros2", "tdd", "zebra"])
        self.assertEqual(len(ask.bodies), 2)
        self.assertEqual(result["tokens"], 210)
        self.assertEqual(set(ask.bodies[1]["questions"]),
                         {"which", "fits::ros2", "fits::tdd", "fits::zebra"})

    def test_a_low_gate_asks_once_and_suggests_nothing(self):
        ask = FakeAsk(gate=(0.1, 0.1, 0.9))
        result = suggest.suggest("explain a mutex", ROSTER, ask)
        self.assertIsNone(result["skill"])
        self.assertEqual(result["reason"], "gate")
        self.assertEqual(len(ask.bodies), 1)

    def test_nothing_fits_suggests_nothing(self):
        ask = FakeAsk(ranking={"pptx": 0.9}, winner="pptx", fits={"pptx": 0.2})
        result = suggest.suggest("post to mastodon", ROSTER, ask)
        self.assertIsNone(result["skill"])
        self.assertEqual(result["reason"], "nothing_fits")

    def test_a_winner_outside_the_roster_is_refused(self):
        ask = FakeAsk(ranking={"pptx": 0.9}, winner="not-a-skill", fits={"pptx": 0.9})
        self.assertIsNone(suggest.suggest("x y z w", ROSTER, ask)["skill"])

    def test_empty_roster_never_asks(self):
        ask = FakeAsk()
        self.assertEqual(suggest.suggest("x y z w", [], ask)["reason"], "no_roster")
        self.assertEqual(ask.bodies, [])

    def test_a_large_roster_is_chunked_under_the_option_limit(self):
        big = [_skill("s%03d" % i, "skill %d" % i) for i in range(suggest.MAX_CHOICE_OPTIONS + 10)]
        qs = suggest.wide_questions(big)
        choices = [q for k, q in qs.items() if k.startswith("which::")]
        self.assertEqual(len(choices), 2)
        self.assertTrue(all(len(q["criteria"]) <= suggest.MAX_CHOICE_OPTIONS for q in choices))

    def test_a_name_only_skill_sends_null_criteria(self):
        qs = suggest.wide_questions([_skill("org:pptx", "", "")])
        self.assertIsNone(qs["which::0"]["criteria"]["org:pptx"])
        rerank = suggest.rerank_questions([_skill("org:pptx", "", "")])
        self.assertIsNone(rerank["which"]["criteria"]["org:pptx"])
        self.assertNotIn("described as", rerank["fits::org:pptx"]["instructions"])

    def test_context_block_is_the_measured_wording(self):
        self.assertEqual(
            suggest.context_block("ros2"),
            "<skill_relevance>\nRelevant to the current request: ros2. Ignore this if it does "
            "not fit what the user actually asked for.\n</skill_relevance>")
        self.assertIn("No skill in the roster appears relevant", suggest.context_block(None))


class TestHook(unittest.TestCase):
    def setUp(self):
        self.hook = _load_hook()
        self.tmp = Path(tempfile.mkdtemp())
        env = {"AIRLOCK_CONFIG_DIR": str(self.tmp / "config"),
               "AIRLOCK_STATE_DIR": str(self.tmp / "state")}
        for var in self.hook.DISABLE_VARS + (self.hook.MODE_ENV,):
            env[var] = ""
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        disk = mock.patch("suggest.roster.discover", return_value=ROSTER)
        disk.start()
        self.addCleanup(disk.stop)

    def _main(self, prompt, ask, key="key"):
        payload = {"session_id": "s", "cwd": "/tmp", "prompt": prompt}
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
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_emits_the_block_as_additional_context(self):
        ask = FakeAsk(ranking={"ros2": 1.0}, winner="ros2", fits={"ros2": 0.9})
        out = self._main("write a launch file for the lidar", ask)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "UserPromptSubmit")
        self.assertIn("Relevant to the current request: ros2.", hso["additionalContext"])
        row = self._rows()[-1]
        self.assertEqual(row["skill"], "ros2")
        self.assertTrue(row["emitted"])

    def test_the_prompt_is_redacted_before_it_leaves(self):
        ask = FakeAsk(gate=(0.0, 0.0, 1.0))
        self._main("deploy with token sk-ant-api03-" + "a" * 40 + " to prod now", ask)
        sent = json.dumps(ask.bodies[0]["state"])
        self.assertNotIn("a" * 40, sent)
        self.assertNotIn("a" * 40, json.dumps(self._rows()[-1]))

    def test_short_prompt_is_never_sent(self):
        ask = FakeAsk()
        self.assertIsNone(self._main("commit it", ask))
        self.assertEqual(ask.bodies, [])
        self.assertEqual(self._rows(), [])

    def test_no_key_is_silent(self):
        ask = FakeAsk()
        self.assertIsNone(self._main("write a launch file for the lidar", ask, key=None))
        self.assertEqual(ask.bodies, [])

    def test_an_error_is_silent_and_logged(self):
        def boom(body, timeout_s=None):
            raise OSError("down")
        self.assertIsNone(self._main("write a launch file for the lidar", boom))
        self.assertIn("down", self._rows()[-1]["error"])

    def test_shadow_logs_and_emits_nothing(self):
        (self.tmp / "config").mkdir(parents=True)
        (self.tmp / "config" / "skill-suggest").write_text("shadow\n")
        ask = FakeAsk(ranking={"ros2": 1.0}, winner="ros2", fits={"ros2": 0.9})
        self.assertIsNone(self._main("write a launch file for the lidar", ask))
        row = self._rows()[-1]
        self.assertEqual((row["mode"], row["skill"], row["emitted"]), ("shadow", "ros2", False))

    def test_off_and_kill_switch_do_nothing(self):
        ask = FakeAsk()
        with mock.patch.dict(os.environ, {self.hook.MODE_ENV: "off"}):
            self.assertIsNone(self._main("write a launch file for the lidar", ask))
        with mock.patch.dict(os.environ, {"AIRLOCK_DISABLE": "1"}):
            self.assertIsNone(self._main("write a launch file for the lidar", ask))
        self.assertEqual(ask.bodies, [])
        self.assertEqual(self._rows(), [])

    def test_garbage_stdin_is_silent(self):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("not json")), mock.patch("sys.stdout", out):
            self.assertEqual(self.hook.main(), 0)
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
