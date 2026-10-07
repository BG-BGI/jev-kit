"""Metrics events from the disclosure commands, the inject hook and pricing.

Each successful state change writes exactly one events row (component
"disclosure" or "pricing") through airlock.log.append; a no-op or refusal
writes nothing; and every write is fail-open -- a broken log or metrics DB
must never fail the command, and the inject hook's stdout must be
byte-identical whether logging worked or exploded.
"""
import tests  # noqa: F401, I001 -- MUST be the first import; see tests/__init__.py.

import io
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from airlock import log, metrics, paths, pricing
from disclosure import claudemd, mcp, skills

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "hooks" / "airlock_claudemd_inject.py"

GATEWAY = [{"id": "anthropic/claude-sonnet-5.5",
            "pricing": {"input": "0.000002", "output": "0.00001"}}]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        p = mock.patch.object(paths, "state_dir", return_value=self.tmp)
        p.start()
        self.addCleanup(p.stop)
        self.home = self.tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()

    def events(self):
        db = self.tmp / metrics.DB_NAME
        if not db.exists():
            return []
        c = sqlite3.connect(str(db))
        try:
            return c.execute("SELECT component, action, raw FROM events ORDER BY id").fetchall()
        finally:
            c.close()

    def actions(self):
        return [(c, a) for c, a, _raw in self.events()]


class TestSkillsEvents(Base):
    def add_skill(self, name="foo"):
        d = self.home / ".claude" / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("a cold skill\n")

    def test_apply_and_restore_log_one_event_each(self):
        self.add_skill()
        skills.cmd_apply(str(self.home), str(self.projects), 30, out=io.StringIO())
        self.assertEqual(self.actions(), [("disclosure", "skills_apply")])
        raw = json.loads(self.events()[0][2])
        self.assertEqual((raw["moved"], raw["skills"]), (1, ["foo"]))
        skills.cmd_restore(str(self.home), out=io.StringIO())
        self.assertEqual(self.actions(), [("disclosure", "skills_apply"),
                                          ("disclosure", "skills_restore")])

    def test_noop_paths_write_nothing(self):
        skills.cmd_apply(str(self.home), str(self.projects), 30, out=io.StringIO())
        skills.cmd_restore(str(self.home), out=io.StringIO())
        self.assertEqual(self.actions(), [])

    def test_broken_log_does_not_break_apply(self):
        self.add_skill()
        with mock.patch.object(log, "append", side_effect=RuntimeError("boom")):
            out = io.StringIO()
            skills.cmd_apply(str(self.home), str(self.projects), 30, out=out)
        self.assertIn("moved 1 of 1", out.getvalue())
        self.assertTrue((self.home / ".claude" / "skills-cold" / "foo").is_dir())


class TestMcpEvents(Base):
    def setUp(self):
        super().setUp()
        self.project = self.tmp / "work"
        self.project.mkdir()
        (self.project / ".mcp.json").write_text(json.dumps({"mcpServers": {"srv": {}}}))

    def test_apply_and_restore_log_one_event_each(self):
        mcp.apply(str(self.home), str(self.project), 30, out=io.StringIO())
        self.assertEqual(self.actions(), [("disclosure", "mcp_apply")])
        raw = json.loads(self.events()[0][2])
        self.assertEqual((raw["disabled"], raw["servers"]), (1, ["srv"]))
        self.assertEqual(raw["project"], str(self.project))
        # Second apply is a no-op: already disabled, nothing new logged.
        mcp.apply(str(self.home), str(self.project), 30, out=io.StringIO())
        self.assertEqual(len(self.actions()), 1)
        mcp.restore(str(self.home), str(self.project), out=io.StringIO())
        self.assertEqual(self.actions(), [("disclosure", "mcp_apply"),
                                          ("disclosure", "mcp_restore")])
        # Nothing left in the manifest: restore again logs nothing.
        mcp.restore(str(self.home), str(self.project), out=io.StringIO())
        self.assertEqual(len(self.actions()), 2)

    def test_broken_metrics_db_does_not_break_apply(self):
        with mock.patch.object(metrics, "_connect", side_effect=OSError("disk")):
            out = io.StringIO()
            rc = mcp.apply(str(self.home), str(self.project), 30, out=out)
        self.assertEqual(rc, 0)
        self.assertIn("disabled 1 project-scope server(s)", out.getvalue())


class TestClaudemdEvents(Base):
    ORIGINAL = "# Freshworks block\n\nstuff\n\n# Output style\n\n- caveman\n"

    def setUp(self):
        super().setUp()
        self.claude_md = self.home / ".claude" / "CLAUDE.md"
        self.claude_md.write_text(self.ORIGINAL)

    def run_cmd(self, fn):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = fn(str(self.home))
        return rc, out.getvalue(), err.getvalue()

    def test_slim_restore_wire_log_one_event_each(self):
        self.assertEqual(self.run_cmd(claudemd.cmd_slim)[0], 0)
        self.assertEqual(self.actions(), [("disclosure", "claudemd_slim")])
        raw = json.loads(self.events()[0][2])
        self.assertEqual(raw["chars_before"], len(self.ORIGINAL))
        self.assertEqual(raw["chars_after"], len(self.claude_md.read_text()))
        # Already slim: no-op, nothing new.
        self.assertEqual(self.run_cmd(claudemd.cmd_slim)[0], 0)
        self.assertEqual(len(self.actions()), 1)
        self.assertEqual(self.run_cmd(claudemd.cmd_restore)[0], 0)
        self.assertEqual(self.actions()[-1], ("disclosure", "claudemd_restore"))
        self.assertEqual(self.run_cmd(claudemd.cmd_wire)[0], 0)
        self.assertEqual(self.actions()[-1], ("disclosure", "claudemd_wire"))
        # Already wired: no-op, nothing new.
        self.assertEqual(self.run_cmd(claudemd.cmd_wire)[0], 0)
        self.assertEqual(len(self.actions()), 3)

    def test_refusal_writes_nothing(self):
        (self.home / ".claude" / "CLAUDE-full.md").write_text("older, different backup\n")
        rc, _, err = self.run_cmd(claudemd.cmd_slim)
        self.assertEqual(rc, 1)
        self.assertIn("refusing", err)
        self.assertEqual(self.actions(), [])

    def test_broken_log_does_not_break_slim(self):
        with mock.patch.object(log, "append", side_effect=RuntimeError("boom")):
            rc, out, _ = self.run_cmd(claudemd.cmd_slim)
        self.assertEqual(rc, 0)
        self.assertIn("slimmed", out)
        self.assertIn(claudemd.MARKER, self.claude_md.read_text())


class TestInjectHookEvents(Base):
    FULL = "# Freshworks Agentic Developer Toolkit\n\nfull toolkit text\n"

    def setUp(self):
        super().setUp()
        (self.home / ".claude" / "CLAUDE-full.md").write_text(self.FULL)
        self.fw = self.home / "fwapp"
        self.fw.mkdir()
        (self.fw / "manifest.json").write_text(
            json.dumps({"platform-version": "3.0", "modules": {}}))

    def run_hook(self, cwd, state_dir):
        env = dict(os.environ, AIRLOCK_HOME_OVERRIDE=str(self.home),
                   AIRLOCK_STATE_DIR=str(state_dir))
        payload = json.dumps({"cwd": str(cwd), "session_id": "s1"})
        return subprocess.run([sys.executable, str(HOOK)], input=payload,
                              capture_output=True, text=True, env=env, timeout=30)

    def test_emitting_logs_one_event(self):
        proc = self.run_hook(self.fw, self.tmp)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("fw-dev-toolkit-instructions", proc.stdout)
        self.assertEqual(self.actions(), [("disclosure", "claudemd_inject")])
        raw = json.loads(self.events()[0][2])
        self.assertEqual(raw["cwd"], str(self.fw))
        self.assertEqual(raw["chars"], len(self.FULL))

    def test_silent_dir_logs_nothing(self):
        plain = self.home / "plain"
        plain.mkdir()
        proc = self.run_hook(plain, self.tmp)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertEqual(self.actions(), [])

    def test_stdout_identical_when_logging_explodes(self):
        good = self.run_hook(self.fw, self.tmp)
        # A state "dir" that is a regular file: log.append and record_event
        # both blow up internally and must swallow it.
        broken = self.tmp / "state-is-a-file"
        broken.write_text("not a directory")
        bad = self.run_hook(self.fw, broken)
        self.assertEqual(bad.returncode, 0)
        self.assertEqual(bad.stdout, good.stdout)


class TestPricingEvents(Base):
    def test_refresh_logs_success_then_failure(self):
        body = io.BytesIO(json.dumps({"data": GATEWAY}).encode())
        with mock.patch.object(pricing.urllib.request, "urlopen", return_value=mock.MagicMock(
                __enter__=lambda s: body, __exit__=lambda *a: False)):
            self.assertTrue(pricing.refresh(str(self.tmp / "p.json"), now=1.0))
        with mock.patch.object(pricing.urllib.request, "urlopen", side_effect=OSError("net")):
            self.assertIsNone(pricing.refresh(str(self.tmp / "p.json")))
        self.assertEqual(self.actions(), [("pricing", "refresh"),
                                          ("pricing", "refresh_failed")])
        raw = json.loads(self.events()[0][2])
        self.assertEqual(raw["families"], 1)

    def test_broken_log_does_not_break_refresh(self):
        body = io.BytesIO(json.dumps({"data": GATEWAY}).encode())
        with mock.patch.object(pricing.urllib.request, "urlopen", return_value=mock.MagicMock(
                __enter__=lambda s: body, __exit__=lambda *a: False)), \
                mock.patch.object(log, "append", side_effect=RuntimeError("boom")):
            self.assertTrue(pricing.refresh(str(self.tmp / "p.json"), now=1.0))


if __name__ == "__main__":
    unittest.main()
