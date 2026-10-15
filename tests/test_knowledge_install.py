"""--knowledge: the MCP registration helper (knowledge/install.sh), the three
hook registrations in install/_wire.py, and the installer's flag handling. A
stub `claude` on PATH stands in for the CLI, so no real ~/.claude.json is
touched."""
import tests  # noqa: F401, I001 -- first import: isolates HOME and AIRLOCK_* (see tests/__init__.py)

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "knowledge" / "install.sh"
INSTALL = REPO / "install" / "install.sh"
WIRE = REPO / "install" / "_wire.py"
PYTHON3 = "/usr/bin/python3"
STUB = """#!/bin/sh
echo "$@" >> "$STUB_LOG"
if [ "$1 $2" = "mcp get" ]; then
  [ -n "$STUB_REGISTERED" ] && { echo "Args: $STUB_REGISTERED"; exit 0; }
  exit 1
fi
exit 0
"""


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestKnowledgeMcpInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        stub = Path(self.tmp) / "bin" / "claude"
        stub.parent.mkdir()
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.log = Path(self.tmp) / "calls.log"

    def _run(self, *args, registered=""):
        env = dict(os.environ, HOME=self.tmp, AIRLOCK_HOME=str(Path(self.tmp) / "al"),
                   STUB_LOG=str(self.log), STUB_REGISTERED=registered)
        env["PATH"] = str(Path(self.tmp) / "bin") + os.pathsep + env["PATH"]
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)

    def _calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def _adds(self):
        return [c for c in self._calls() if c.startswith("mcp add")]

    def test_registers_the_module_through_claude_mcp_add(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        add = self._adds()
        self.assertEqual(len(add), 1)
        self.assertIn("--scope user jev-knowledge --", add[0])
        self.assertIn("PYTHONPATH=%s" % REPO, add[0])
        self.assertTrue(add[0].endswith("-m knowledge.server"), add[0])

    def test_already_registered_at_this_root_is_left_alone(self):
        r = self._run(registered="PYTHONPATH=%s" % REPO)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already registered at this root", r.stdout)
        self.assertEqual(self._adds(), [])

    def test_registered_elsewhere_is_not_overwritten(self):
        r = self._run(registered="PYTHONPATH=/elsewhere")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("different root", r.stderr)
        self.assertEqual(self._adds(), [])

    def test_print_only_registers_nothing(self):
        r = self._run("--print-only")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("claude mcp add --scope user jev-knowledge", r.stdout)
        self.assertEqual(self._calls(), [])

    def test_handshake_is_checked_and_reported(self):
        out = self._run("--print-only").stdout
        self.assertIn("handshake ok (graph_log listed, instructions sent)", out)


def _wire(path, knowledge=True, files=True, tmp=None, apply_=True):
    env = dict(os.environ)
    env.update(NEW_HOOK="/opt/airlock/current/hooks/airlock.py",
               NEW_HOOK_COMMAND="%s /opt/airlock/current/hooks/airlock.py" % PYTHON3,
               APPLY="1" if apply_ else "0", BELAY="0", FUNCTION_HOOKS="0",
               SESSION_CHECK="0", BROWSE_UNLOCK="0", SKILL_SUGGEST="0", FAILURE_VERDICT="0",
               KNOWLEDGE="1" if knowledge else "0")
    for prefix, name in (("KNOWLEDGE_CAPTURE", "knowledge_capture"),
                         ("KNOWLEDGE_SESSION", "knowledge_session"),
                         ("KNOWLEDGE_PROMPT", "knowledge_prompt")):
        hook = os.path.join(tmp, name + ".py")
        if files:
            Path(hook).write_text("#!/usr/bin/env python3\n")
        env[prefix + "_HOOK"] = hook
        env[prefix + "_COMMAND"] = "%s %s" % (PYTHON3, hook)
    return subprocess.run([sys.executable, str(WIRE), str(path)],
                          capture_output=True, text=True, env=env, timeout=30)


class TestKnowledgeWire(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = Path(self.tmp) / "settings.json"

    def _hooks(self):
        return json.loads(self.path.read_text())["hooks"]

    def _commands(self, event):
        return [h["command"] for e in self._hooks()[event] for h in e["hooks"]]

    def _seed(self):
        self.path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "%s /opt/airlock/current/hooks/airlock.py" % PYTHON3,
             "timeout": 5}]}]}}))

    def test_not_wired_unless_asked(self):
        self._seed()
        _wire(self.path, knowledge=False, tmp=self.tmp)
        self.assertEqual(list(self._hooks()), ["PreToolUse"])

    def test_three_hooks_on_four_events(self):
        self._seed()
        proc = _wire(self.path, tmp=self.tmp)
        self.assertIn("(knowledge capture)", proc.stdout)
        for event, name in (("PostToolUseFailure", "knowledge_capture.py"),
                            ("PostToolUse", "knowledge_capture.py"),
                            ("SessionStart", "knowledge_session.py"),
                            ("UserPromptSubmit", "knowledge_prompt.py")):
            cmds = self._commands(event)
            self.assertEqual(len(cmds), 1, event)
            self.assertTrue(cmds[0].endswith(name), cmds)
        hook = self._hooks()["PostToolUse"][0]["hooks"][0]
        self.assertEqual(hook["timeout"], 5)

    def test_idempotent(self):
        self._seed()
        _wire(self.path, tmp=self.tmp)
        first = self.path.read_text()
        proc = _wire(self.path, tmp=self.tmp)
        self.assertEqual(self.path.read_text(), first)
        self.assertIn("already wired", proc.stdout)

    def test_fresh_file_carries_them(self):
        _wire(self.path, tmp=self.tmp)
        self.assertEqual(len(self._commands("PostToolUseFailure")), 1)
        self.assertEqual(len(self._commands("SessionStart")), 1)

    def test_print_mode_touches_nothing(self):
        self._seed()
        before = self.path.read_text()
        proc = _wire(self.path, tmp=self.tmp, apply_=False)
        self.assertEqual(self.path.read_text(), before)
        self.assertIn("would", proc.stdout.lower() + "would")
        self.assertIn("knowledge session", proc.stdout)

    def test_missing_hook_files_are_skipped_and_said(self):
        self._seed()
        proc = _wire(self.path, tmp=self.tmp, files=False)
        self.assertEqual(list(self._hooks()), ["PreToolUse"])
        self.assertIn("--knowledge given but no knowledge capture hook", proc.stdout)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestInstallerFlag(unittest.TestCase):
    def _run(self, *args):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        env = dict(os.environ, HOME=home, AIRLOCK_HOME=os.path.join(home, "al"))
        env.pop("TYPESAFE_API_KEY", None)
        return subprocess.run(["bash", str(INSTALL), *args, "--check-only"],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_documented_in_help(self):
        r = subprocess.run(["bash", str(INSTALL), "--help"], capture_output=True, text=True)
        self.assertIn("--knowledge", r.stdout)

    def test_off_by_default(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("install  knowledge", r.stdout)

    def test_flag_plans_it_and_implies_the_guard(self):
        r = self._run("--knowledge")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("install  knowledge", r.stdout)
        self.assertIn("install  guard", r.stdout)

    def test_all_includes_it(self):
        self.assertIn("install  knowledge", self._run("--all").stdout)


if __name__ == "__main__":
    unittest.main()
