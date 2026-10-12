import io
import json
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from jevkit import install, update


def _repo(tmp):
    repo = os.path.join(tmp, "kit")
    os.makedirs(os.path.join(repo, "install"))
    Path(repo, "install", "install.sh").write_text("#!/bin/sh\n")
    return repo


def _args(repo, **kw):
    base = dict(repo=repo, profile=None, yes=False, installer_args=[])
    base.update(kw)
    return types.SimpleNamespace(**base)


class ChooseProfileTests(unittest.TestCase):
    def test_precedence(self):
        self.assertEqual(install.choose_profile("shared", "owner", True), "shared")
        self.assertEqual(install.choose_profile(None, "owner", True), "owner")
        self.assertIsNone(install.choose_profile(None, None, False))

    def test_prompts_until_valid(self):
        answers = iter(["maybe", "OWNER"])
        self.assertEqual(install.choose_profile(None, None, True, ask=lambda _: next(answers)), "owner")


class InstallRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "cfg"
        for target in ("jevkit.update.paths.kit_config_dir", "jevkit.common.paths.kit_config_dir"):
            p = mock.patch(target, lambda: self.cfg)
            p.start()
            self.addCleanup(p.stop)
        self.repo = _repo(self.tmp.name)

    def test_dry_run_runs_check_only_and_writes_nothing(self):
        calls = []
        out = io.StringIO()
        rc = install.run(_args(self.repo, installer_args=["--", "--guard"]), out, io.StringIO(),
                         runner=lambda c, cwd=None: calls.append(c) or 0, interactive=False)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][2:], ["--check-only", "--guard"])
        self.assertFalse(self.cfg.exists())
        self.assertIn("dry run", out.getvalue())

    def test_yes_without_a_profile_and_no_terminal_is_refused(self):
        calls = []
        err = io.StringIO()
        rc = install.run(_args(self.repo, yes=True), io.StringIO(), err,
                         runner=lambda c, cwd=None: calls.append(c) or 0, interactive=False)
        self.assertEqual((rc, calls), (1, []))
        self.assertIn("--profile", err.getvalue())

    def test_yes_installs_then_records_profile_and_repo(self):
        calls = []
        out = io.StringIO()
        rc = install.run(_args(self.repo, yes=True, profile="owner", installer_args=["--guard"]), out, io.StringIO(),
                         runner=lambda c, cwd=None: calls.append(c) or 0, interactive=False)
        self.assertEqual(rc, 0)
        self.assertNotIn("--check-only", calls[0])
        self.assertEqual(json.loads((self.cfg / "profile.json").read_text())["profile"], "owner")
        self.assertEqual(json.loads((self.cfg / "state.json").read_text())["repo"], os.path.realpath(self.repo))
        self.assertIn("opt-in", out.getvalue())

    def test_installer_failure_records_nothing(self):
        err = io.StringIO()
        rc = install.run(_args(self.repo, yes=True, profile="owner"), io.StringIO(), err,
                         runner=lambda c, cwd=None: 3, interactive=False)
        self.assertEqual(rc, 3)
        self.assertFalse(self.cfg.exists())

    def test_never_adds_a_compaction_flag(self):
        calls = []
        install.run(_args(self.repo, yes=True, profile="owner"), io.StringIO(), io.StringIO(),
                    runner=lambda c, cwd=None: calls.append(c) or 0, interactive=False)
        self.assertNotIn("--compaction", calls[0])


if __name__ == "__main__":
    unittest.main()
