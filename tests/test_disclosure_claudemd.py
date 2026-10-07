"""disclosure/claudemd.py and hooks/airlock_claudemd_inject.py.

The slim/restore pair must never lose a byte of the user's CLAUDE.md: the
backup is written before the original is touched, a divergent older backup
is a refusal rather than a clobber, and restore round-trips exactly. The
hook is tested the way Claude Code runs it -- a subprocess fed JSON on
stdin -- because its whole contract is what it prints and that it exits 0
no matter what it was fed.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from disclosure import claudemd

HOOK = REPO_ROOT / "hooks" / "airlock_claudemd_inject.py"

FW_BLOCK = (
    "# Freshworks Agentic Developer Toolkit\n\n"
    "Managed install. Update with: npx tool update\n\n"
    "## Non-negotiables\n\n- Platform version 3.0\n\n"
)
TAIL = (
    "# Output style (standing)\n\n- ALWAYS caveman.\n\n"
    "# Decisions via Jev (standing)\n\n- Use jev-decide for big files.\n"
)
ORIGINAL = FW_BLOCK + TAIL


def run_cli(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = claudemd.main(args)
    return rc, out.getvalue(), err.getvalue()


class FakeHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.claude = self.home / ".claude"
        self.claude.mkdir()
        self.claude_md = self.claude / "CLAUDE.md"
        self.full_md = self.claude / "CLAUDE-full.md"

    def write_original(self, text=ORIGINAL):
        self.claude_md.write_text(text)

    def slim(self):
        return run_cli(["slim", "--home", str(self.home)])


class TestSlim(FakeHome):
    def test_creates_backup_and_slim_file(self):
        self.write_original()
        rc, _, _ = self.slim()
        self.assertEqual(rc, 0)
        self.assertEqual(self.full_md.read_text(), ORIGINAL)
        slim = self.claude_md.read_text()
        self.assertIn(claudemd.MARKER, slim)
        self.assertIn("CLAUDE-full.md", slim)
        self.assertIn(claudemd.RESTORE_CMD, slim)

    def test_preserves_tail_after_output_style(self):
        self.write_original()
        self.slim()
        slim = self.claude_md.read_text()
        self.assertIn("# Output style (standing)", slim)
        self.assertIn("# Decisions via Jev (standing)", slim)
        self.assertIn("ALWAYS caveman", slim)
        # The toolkit block above the heading is gone.
        self.assertNotIn("Freshworks Agentic Developer Toolkit", slim)
        self.assertNotIn("Non-negotiables", slim)

    def test_no_output_style_heading_keeps_only_pointer(self):
        self.write_original(FW_BLOCK)
        rc, _, _ = self.slim()
        self.assertEqual(rc, 0)
        slim = self.claude_md.read_text()
        self.assertIn(claudemd.MARKER, slim)
        self.assertNotIn("Non-negotiables", slim)

    def test_idempotent(self):
        self.write_original()
        self.slim()
        first = self.claude_md.read_text()
        rc, out, _ = self.slim()
        self.assertEqual(rc, 0)
        self.assertIn("already slim", out)
        self.assertEqual(self.claude_md.read_text(), first)
        self.assertEqual(self.full_md.read_text(), ORIGINAL)

    def test_refuses_divergent_backup(self):
        self.write_original()
        self.full_md.write_text("an older, different backup\n")
        rc, _, err = self.slim()
        self.assertEqual(rc, 1)
        self.assertIn("refusing", err)
        # Neither file was touched.
        self.assertEqual(self.claude_md.read_text(), ORIGINAL)
        self.assertEqual(self.full_md.read_text(), "an older, different backup\n")

    def test_matching_backup_is_not_a_refusal(self):
        self.write_original()
        self.full_md.write_text(ORIGINAL)
        rc, _, _ = self.slim()
        self.assertEqual(rc, 0)
        self.assertIn(claudemd.MARKER, self.claude_md.read_text())

    def test_missing_claude_md(self):
        rc, _, err = self.slim()
        self.assertEqual(rc, 1)
        self.assertIn("no CLAUDE.md", err)


class TestRestore(FakeHome):
    def test_round_trip(self):
        self.write_original()
        self.slim()
        rc, _, _ = run_cli(["restore", "--home", str(self.home)])
        self.assertEqual(rc, 0)
        self.assertEqual(self.claude_md.read_text(), ORIGINAL)
        # The backup is kept.
        self.assertEqual(self.full_md.read_text(), ORIGINAL)

    def test_refuses_without_marker(self):
        self.write_original()
        self.full_md.write_text(ORIGINAL)
        rc, _, err = run_cli(["restore", "--home", str(self.home)])
        self.assertEqual(rc, 1)
        self.assertIn("marker", err)
        self.assertEqual(self.claude_md.read_text(), ORIGINAL)

    def test_refuses_without_backup(self):
        self.write_original()
        rc, _, err = run_cli(["restore", "--home", str(self.home)])
        self.assertEqual(rc, 1)
        self.assertIn("nothing to restore", err)


class TestStatus(FakeHome):
    def test_full_and_slim(self):
        self.write_original()
        rc, out, _ = run_cli(["status", "--home", str(self.home)])
        self.assertEqual(rc, 0)
        self.assertIn("CLAUDE.md: full", out)
        self.assertIn("tokens", out)
        self.slim()
        rc, out, _ = run_cli(["status", "--home", str(self.home)])
        self.assertEqual(rc, 0)
        self.assertIn("CLAUDE.md: slim", out)


class TestWire(FakeHome):
    def setUp(self):
        super().setUp()
        self.settings = self.claude / "settings.json"
        self.bak = self.claude / "settings.json.bak"

    def wire(self):
        return run_cli(["wire", "--home", str(self.home)])

    def load(self):
        return json.loads(self.settings.read_text())

    def our_commands(self, data):
        cmds = []
        for entry in data.get("hooks", {}).get("SessionStart", []):
            for h in entry.get("hooks", []):
                if str(claudemd.HOOK_PATH) in h.get("command", ""):
                    cmds.append(h)
        return cmds

    def test_creates_settings_when_missing(self):
        rc, _, _ = self.wire()
        self.assertEqual(rc, 0)
        hooks = self.our_commands(self.load())
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0]["type"], "command")
        # No .bak when there was nothing to back up.
        self.assertFalse(self.bak.exists())

    def test_merges_preserving_existing_hooks(self):
        existing = {
            "env": {"FOO": "1"},
            "hooks": {
                "PreToolUse": [{"matcher": "*", "hooks": [
                    {"type": "command", "command": "python3 /x/hooks/airlock.py",
                     "timeout": 5}]}],
                "SessionStart": [{"matcher": "*", "hooks": [
                    {"type": "command",
                     "command": "python3 /x/hooks/airlock_session_check.py",
                     "timeout": 5}]}],
            },
        }
        self.settings.write_text(json.dumps(existing, indent=2))
        rc, _, _ = self.wire()
        self.assertEqual(rc, 0)
        data = self.load()
        # Everything that was there survives.
        self.assertEqual(data["env"], {"FOO": "1"})
        cmds = [h["command"]
                for entry in data["hooks"]["SessionStart"]
                for h in entry["hooks"]]
        self.assertIn("python3 /x/hooks/airlock_session_check.py", cmds)
        self.assertEqual(len(self.our_commands(data)), 1)
        self.assertEqual(
            data["hooks"]["PreToolUse"],
            existing["hooks"]["PreToolUse"])
        # Backed up first.
        self.assertTrue(self.bak.exists())
        self.assertEqual(json.loads(self.bak.read_text()), existing)

    def test_idempotent(self):
        self.wire()
        after_first = self.settings.read_text()
        rc, out, _ = self.wire()
        self.assertEqual(rc, 0)
        self.assertIn("already wired", out)
        self.assertEqual(self.settings.read_text(), after_first)
        self.assertEqual(len(self.our_commands(self.load())), 1)

    def test_bak_not_overwritten(self):
        self.settings.write_text("{}")
        self.bak.write_text("the first backup")
        self.wire()
        self.assertEqual(self.bak.read_text(), "the first backup")

    def test_refuses_invalid_json(self):
        self.settings.write_text("{not json")
        rc, _, err = self.wire()
        self.assertEqual(rc, 1)
        self.assertIn("refusing", err)
        self.assertEqual(self.settings.read_text(), "{not json")


class TestInjectHook(FakeHome):
    FULL = "# Freshworks Agentic Developer Toolkit\n\nfull toolkit text\n"

    def run_hook(self, stdin, cwd_payload=None):
        payload = stdin
        if payload is None:
            payload = json.dumps({"cwd": str(cwd_payload), "session_id": "s1"})
        env = dict(os.environ, AIRLOCK_HOME_OVERRIDE=str(self.home))
        proc = subprocess.run(
            [sys.executable, str(HOOK)], input=payload,
            capture_output=True, text=True, env=env, timeout=30)
        return proc

    def make_fw_dir(self, name="fwapp"):
        d = self.home / name
        d.mkdir()
        (d / "manifest.json").write_text(
            json.dumps({"platform-version": "3.0", "modules": {}}))
        return d

    def test_emits_context_in_fw_dir(self):
        self.full_md.write_text(self.FULL)
        fw = self.make_fw_dir()
        proc = self.run_hook(None, cwd_payload=fw)
        self.assertEqual(proc.returncode, 0)
        out = json.loads(proc.stdout)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "SessionStart")
        self.assertEqual(
            hso["additionalContext"],
            "<fw-dev-toolkit-instructions>\n%s\n</fw-dev-toolkit-instructions>"
            % self.FULL)

    def test_emits_from_subdirectory_of_fw_dir(self):
        self.full_md.write_text(self.FULL)
        fw = self.make_fw_dir()
        sub = fw / "app" / "scripts"
        sub.mkdir(parents=True)
        proc = self.run_hook(None, cwd_payload=sub)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("fw-dev-toolkit-instructions", proc.stdout)

    def test_silent_in_plain_dir(self):
        self.full_md.write_text(self.FULL)
        plain = self.home / "plain"
        plain.mkdir()
        proc = self.run_hook(None, cwd_payload=plain)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_global_fdk_state_dir_in_home_does_not_count(self):
        # The FDK CLI keeps a global ~/.fdk state dir; home is an ancestor of
        # every project, so counting it would inject the text everywhere.
        self.full_md.write_text(self.FULL)
        (self.home / ".fdk").mkdir()
        plain = self.home / "plain"
        plain.mkdir()
        proc = self.run_hook(None, cwd_payload=plain)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_fdk_dir_inside_an_app_dir_still_counts(self):
        self.full_md.write_text(self.FULL)
        app = self.home / "myapp"
        (app / ".fdk").mkdir(parents=True)
        proc = self.run_hook(None, cwd_payload=app)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("fw-dev-toolkit-instructions", proc.stdout)

    def test_silent_when_backup_missing(self):
        fw = self.make_fw_dir()
        proc = self.run_hook(None, cwd_payload=fw)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_survives_garbage_stdin(self):
        self.full_md.write_text(self.FULL)
        for garbage in ("", "not json at all", "[1, 2, 3]", '"a string"'):
            proc = self.run_hook(garbage)
            self.assertEqual(proc.returncode, 0, garbage)
            self.assertEqual(proc.stdout, "", garbage)

    def test_fdk_dir_and_iparams_also_match(self):
        self.full_md.write_text(self.FULL)
        a = self.home / "a"
        (a / ".fdk").mkdir(parents=True)
        b = self.home / "b" / "config"
        b.mkdir(parents=True)
        (b / "iparams.json").write_text("{}")
        for d in (a, self.home / "b"):
            proc = self.run_hook(None, cwd_payload=d)
            self.assertEqual(proc.returncode, 0)
            self.assertIn("fw-dev-toolkit-instructions", proc.stdout, str(d))


if __name__ == "__main__":
    unittest.main()
