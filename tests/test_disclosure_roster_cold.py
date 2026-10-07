"""Cold skills (~/.claude/skills-cold, moved by disclosure/skills.py) in the
suggest pipeline: roster discovery, merge, ranking, and the pointer block."""
import tempfile
import unittest
from pathlib import Path

from suggest import roster, suggest


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class FakeAsk:
    """Ranks `winner` first, passes the gate, and says everything fits."""

    def __init__(self, winner):
        self.winner = winner

    def __call__(self, body, timeout_s=None):
        answers = {}
        for key, q in body["questions"].items():
            if key.startswith("which::"):
                answers[key] = {"probabilities": {self.winner: 0.9}}
            elif key == "which":
                answers[key] = {"choice": self.winner}
            elif key.startswith("gate::"):
                answers[key] = {"noul": 0.1 if key.endswith("prose_suffices") else 0.9}
            elif key.startswith("fits::"):
                answers[key] = {"noul": 0.9}
        return {"answers": answers}, None


class TestColdRoster(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.cwd = Path(tempfile.mkdtemp())

    def test_cold_source_carries_flag_and_path(self):
        skill_md = self.home / ".claude/skills-cold/zebra/SKILL.md"
        _write(skill_md, "---\ndescription: zebra label printing\n---\nZPL body")
        found = roster.discover_disk(str(self.cwd), str(self.home))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["name"], "zebra")
        self.assertEqual(found[0]["description"], "zebra label printing")
        self.assertEqual(found[0]["body"], "ZPL body")
        self.assertIs(found[0]["cold"], True)
        self.assertEqual(found[0]["path"], str(skill_md))

    def test_cold_does_not_shadow_a_hot_same_name(self):
        _write(self.home / ".claude/skills/zebra/SKILL.md", "---\ndescription: hot zebra\n---\n")
        _write(self.home / ".claude/skills-cold/zebra/SKILL.md", "---\ndescription: cold zebra\n---\n")
        found = roster.discover_disk(None, str(self.home))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["description"], "hot zebra")
        self.assertNotIn("cold", found[0])

    def test_disabled_cold_skill_stays_invisible(self):
        _write(self.home / ".claude/skills-cold/hidden/SKILL.md",
               "---\ndescription: h\ndisable-model-invocation: true\n---\n")
        self.assertEqual(roster.discover_disk(None, str(self.home)), [])

    def test_merge_keeps_cold_skills_the_listing_never_names(self):
        disk = [{"name": "alpha", "description": "a", "body": ""},
                {"name": "unlisted", "description": "u", "body": ""},
                {"name": "zebra", "description": "z", "body": "", "cold": True, "path": "/p"}]
        merged = roster.merge({"alpha": "short"}, disk)
        self.assertEqual([s["name"] for s in merged], ["alpha", "zebra"])
        self.assertIs(merged[1]["cold"], True)

    def test_discover_with_a_listing_still_sees_cold(self):
        _write(self.home / ".claude/skills-cold/zebra/SKILL.md", "---\ndescription: z\n---\n")
        transcript = Path(tempfile.mkdtemp()) / "t.jsonl"
        transcript.write_text(
            '{"type": "attachment", "attachment": {"type": "skill_listing", '
            '"content": "- alpha: listed", "names": []}}')
        found = roster.discover(str(self.cwd), str(self.home), transcript_path=str(transcript))
        self.assertEqual([s["name"] for s in found], ["alpha", "zebra"])
        self.assertIs(found[1]["cold"], True)


class TestColdSuggestion(unittest.TestCase):
    COLD = {"name": "zebra", "description": "print labels", "body": "zpl",
            "cold": True, "path": "/home/u/.claude/skills-cold/zebra/SKILL.md"}
    HOT = {"name": "tdd", "description": "red-green-refactor", "body": ""}

    def test_suggest_result_carries_cold_and_path(self):
        result = suggest.suggest("print a shipping label for order 7",
                                 [self.COLD, self.HOT], FakeAsk("zebra"))
        self.assertEqual((result["skill"], result["reason"]), ("zebra", "suggested"))
        self.assertIs(result["cold"], True)
        self.assertEqual(result["path"], self.COLD["path"])

    def test_a_hot_winner_carries_neither(self):
        result = suggest.suggest("write a failing test first",
                                 [self.COLD, self.HOT], FakeAsk("tdd"))
        self.assertEqual(result["skill"], "tdd")
        self.assertNotIn("cold", result)
        self.assertNotIn("path", result)

    def test_cold_block_points_at_the_skill_md(self):
        self.assertEqual(
            suggest.context_block("zebra", self.COLD["path"]),
            "<skill_relevance>\n"
            "Relevant to the current request: zebra. This skill is not in the "
            "session roster; read /home/u/.claude/skills-cold/zebra/SKILL.md and "
            "follow it. Ignore this if it does not fit what the user actually "
            "asked for.\n"
            "</skill_relevance>")

    def test_hot_and_none_blocks_are_unchanged(self):
        self.assertEqual(
            suggest.context_block("zebra"),
            "<skill_relevance>\n"
            "Relevant to the current request: zebra. Ignore this if it does not "
            "fit what the user actually asked for.\n"
            "</skill_relevance>")
        self.assertIn(suggest.NONE_TEXT, suggest.context_block(None))


if __name__ == "__main__":
    unittest.main()
