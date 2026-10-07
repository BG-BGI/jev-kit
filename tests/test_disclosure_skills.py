"""disclosure/skills.py: the usage scan, the hot/cold split, and the
apply/restore round trip. Hermetic: a temp home, a temp projects dir."""
import contextlib
import datetime
import io
import json
import tempfile
import unittest
from pathlib import Path

from disclosure import skills

UTC = datetime.timezone.utc


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _record(name, ts):
    return json.dumps({
        "type": "assistant", "timestamp": ts,
        "message": {"content": [
            {"type": "text", "text": "loading"},
            {"type": "tool_use", "name": "Skill", "input": {"skill": name}},
        ]},
    })


class TestUsageScan(unittest.TestCase):
    def setUp(self):
        self.projects = Path(tempfile.mkdtemp())

    def test_counts_and_newest_timestamp(self):
        _write(self.projects / "proj-a/t1.jsonl", "\n".join([
            _record("ros2", "2026-09-01T10:00:00Z"),
            _record("ros2", "2026-10-01T10:00:00Z"),
            _record("mk:pptx", "2026-10-02T10:00:00.5+00:00"),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "Skill"}}]}}),
            json.dumps({"type": "user", "message": "mentions Skill in passing"}),
            'not json but mentions "Skill"',
        ]))
        _write(self.projects / "proj-b/t2.jsonl", _record("ros2", "2026-09-15T10:00:00Z"))
        usage = skills.scan_usage(str(self.projects))
        self.assertEqual(usage["ros2"]["count"], 3)
        self.assertEqual(usage["ros2"]["last"],
                         datetime.datetime(2026, 10, 1, 10, tzinfo=UTC))
        self.assertEqual(usage["mk:pptx"]["count"], 1)
        self.assertNotIn("Bash", usage)

    def test_missing_timestamp_still_counts(self):
        line = json.dumps({"message": {"content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": "tdd"}}]}})
        _write(self.projects / "p/t.jsonl", line)
        usage = skills.scan_usage(str(self.projects))
        self.assertEqual(usage["tdd"], {"count": 1, "last": None})

    def test_missing_dir_is_empty_not_an_error(self):
        self.assertEqual(skills.scan_usage("/nonexistent/projects"), {})


class TestSplit(unittest.TestCase):
    NOW = datetime.datetime(2026, 10, 7, tzinfo=UTC)

    def test_days_window(self):
        usage = {
            "fresh": {"count": 1, "last": self.NOW - datetime.timedelta(days=5)},
            "stale": {"count": 9, "last": self.NOW - datetime.timedelta(days=45)},
        }
        roster = [("fresh", "/s/fresh"), ("stale", "/s/stale"), ("neverused", "/s/neverused")]
        hot, cold = skills.split(roster, usage, 30, now=self.NOW)
        self.assertEqual([n for n, _ in hot], ["fresh"])
        self.assertEqual([n for n, _ in cold], ["stale", "neverused"])
        hot, cold = skills.split(roster, usage, 60, now=self.NOW)
        self.assertEqual([n for n, _ in cold], ["neverused"])


class TestApplyRestore(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.projects = Path(tempfile.mkdtemp())
        _write(self.home / ".claude/skills/hotskill/SKILL.md", "---\ndescription: h\n---\nH")
        _write(self.home / ".claude/skills/coldskill/SKILL.md", "---\ndescription: c\n---\nC")
        _write(self.home / ".claude/skills/hidden/SKILL.md",
               "---\ndescription: x\ndisable-model-invocation: true\n---\n")
        _write(self.projects / "p/t.jsonl",
               _record("hotskill", skills._utcnow().isoformat()))

    def _run(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            skills.main(list(argv) + ["--home", str(self.home),
                                      "--projects", str(self.projects)])
        return out.getvalue()

    def test_report_names_hot_cold_never_and_plugins(self):
        _write(self.projects / "p/t2.jsonl", _record("mk:pptx", "2026-10-01T00:00:00Z"))
        text = self._run("report")
        self.assertRegex(text, r"hotskill\s+\S+\s+hot")
        self.assertRegex(text, r"coldskill\s+never\s+cold")
        self.assertNotIn("hidden", text)
        self.assertIn("plugins (report-only, not moved):", text)
        self.assertIn("mk:pptx", text)

    def test_plan_prints_moves_but_moves_nothing(self):
        text = self._run("plan")
        self.assertIn("would move", text)
        self.assertIn("coldskill", text)
        self.assertNotIn("hotskill", text)
        self.assertTrue((self.home / ".claude/skills/coldskill/SKILL.md").is_file())

    def test_apply_moves_cold_and_writes_manifest(self):
        self._run("apply")
        self.assertFalse((self.home / ".claude/skills/coldskill").exists())
        self.assertTrue((self.home / ".claude/skills-cold/coldskill/SKILL.md").is_file())
        self.assertTrue((self.home / ".claude/skills/hotskill/SKILL.md").is_file())
        self.assertTrue((self.home / ".claude/skills/hidden/SKILL.md").is_file())
        moved = skills.read_manifest(str(self.home / ".claude/skills-cold"))
        self.assertEqual(sorted(moved), ["coldskill"])
        self.assertEqual(moved["coldskill"]["from"],
                         str(self.home / ".claude/skills/coldskill"))
        self.assertIn("ts", moved["coldskill"])

    def test_apply_is_idempotent_and_never_overwrites(self):
        self._run("apply")
        self.assertIn("nothing to move", self._run("apply"))
        _write(self.home / ".claude/skills/coldskill/SKILL.md", "---\ndescription: new\n---\n")
        text = self._run("apply")
        self.assertIn("already exists", text)
        self.assertTrue((self.home / ".claude/skills/coldskill/SKILL.md").is_file())
        self.assertEqual((self.home / ".claude/skills-cold/coldskill/SKILL.md").read_text(),
                         "---\ndescription: c\n---\nC")

    def test_restore_round_trips_and_prunes_manifest(self):
        self._run("apply")
        self._run("restore")
        self.assertEqual((self.home / ".claude/skills/coldskill/SKILL.md").read_text(),
                         "---\ndescription: c\n---\nC")
        self.assertFalse((self.home / ".claude/skills-cold/coldskill").exists())
        self.assertEqual(skills.read_manifest(str(self.home / ".claude/skills-cold")), {})

    def test_restore_by_name_leaves_the_rest_cold(self):
        _write(self.home / ".claude/skills/othercold/SKILL.md", "---\ndescription: o\n---\n")
        self._run("apply")
        self._run("restore", "coldskill")
        self.assertTrue((self.home / ".claude/skills/coldskill/SKILL.md").is_file())
        self.assertFalse((self.home / ".claude/skills/othercold").exists())
        moved = skills.read_manifest(str(self.home / ".claude/skills-cold"))
        self.assertEqual(sorted(moved), ["othercold"])

    def test_restore_never_overwrites_an_existing_destination(self):
        self._run("apply")
        _write(self.home / ".claude/skills/coldskill/SKILL.md", "---\ndescription: new\n---\n")
        text = self._run("restore")
        self.assertIn("already exists", text)
        self.assertTrue((self.home / ".claude/skills-cold/coldskill/SKILL.md").is_file())
        moved = skills.read_manifest(str(self.home / ".claude/skills-cold"))
        self.assertEqual(sorted(moved), ["coldskill"])


if __name__ == "__main__":
    unittest.main()
