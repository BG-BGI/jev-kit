import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jevkit import modversion, plugins, update
from jevkit.cli import main

ROOT = Path(__file__).resolve().parent.parent
MOD = ROOT / "mods" / "jev-stats"


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    rc = main(argv, out, err)
    return rc, out.getvalue(), err.getvalue()


class ModVersionTests(unittest.TestCase):
    def test_shipped_mod_hash_matches_its_version(self):
        self.assertEqual(
            modversion.stored_hash(str(MOD)), modversion.content_hash(str(MOD)),
            "mods/jev-stats changed without a version bump: run python3 tools/bump_mod.py",
        )

    def _mod(self, tmp):
        d = Path(tmp) / "m"
        (d / ".claude-plugin").mkdir(parents=True)
        (d / "hooks").mkdir()
        (d / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "m", "version": "0.1.9"}))
        (d / "hooks" / "a.ts").write_text("one")
        return d

    def test_bump_patch_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._mod(tmp)
            self.assertEqual(modversion.bump(str(d)), ("0.1.9", "0.1.10"))
            self.assertIsNone(modversion.bump(str(d)))
            (d / "hooks" / "a.ts").write_text("two")
            self.assertEqual(modversion.bump(str(d)), ("0.1.10", "0.1.11"))

    def test_tests_and_manifest_do_not_change_the_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._mod(tmp)
            before = modversion.content_hash(str(d))
            (d / "hooks" / "a.test.ts").write_text("t")
            (d / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "9.9.9"}))
            self.assertEqual(before, modversion.content_hash(str(d)))


class PluginsTests(unittest.TestCase):
    def test_dry_run_runs_nothing(self):
        calls = []
        out, err = io.StringIO(), io.StringIO()
        rc = plugins.update_plugins(False, out, err, runner=lambda c: calls.append(c) or 0)
        self.assertEqual((rc, calls), (0, []))
        self.assertIn("claude plugin marketplace update jev-kit", out.getvalue())
        self.assertIn("claude plugin update jev-stats@jev-kit", out.getvalue())

    def test_yes_runs_in_order_and_reminds_reload(self):
        calls = []
        out = io.StringIO()
        rc = plugins.update_plugins(True, out, io.StringIO(), runner=lambda c: calls.append(c) or 0)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][:4], ["claude", "plugin", "marketplace", "update"])
        self.assertEqual(calls[1][:3], ["claude", "plugin", "update"])
        self.assertIn("/reload-plugins", out.getvalue())

    def test_failure_is_nonzero(self):
        rc = plugins.update_plugins(True, io.StringIO(), io.StringIO(), runner=lambda c: 1)
        self.assertEqual(rc, 1)

    def test_missing_claude_is_reported(self):
        def boom(_):
            raise OSError("no claude")
        err = io.StringIO()
        self.assertEqual(plugins.update_plugins(True, io.StringIO(), err, runner=boom), 1)
        self.assertIn("cannot run claude", err.getvalue())


def _git(repo, *a):
    subprocess.run(["git", "-C", repo] + list(a), check=True, capture_output=True)


class UpdateTests(unittest.TestCase):
    def _repo(self, tmp):
        repo = os.path.join(tmp, "kit")
        os.makedirs(os.path.join(repo, "install"))
        Path(repo, "install", "install.sh").write_text("#!/bin/sh\n")
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
        _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x")
        return repo

    def test_no_checkout_found(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(update.paths, "kit_config_dir", lambda: Path(tmp)):
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                rc, _, err = _run(["update"])
            finally:
                os.chdir(cwd)
            self.assertEqual(rc, 1)
            self.assertIn("no jev-kit checkout", err)

    def test_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(update.paths, "kit_config_dir", lambda: Path(tmp)):
            repo = self._repo(tmp)
            with mock.patch("jevkit.update.subprocess.call") as call:
                rc, out, _ = _run(["update", "--repo", repo])
            self.assertEqual(rc, 0)
            call.assert_not_called()
            self.assertIn("dry run", out)
            self.assertFalse(os.path.exists(os.path.join(tmp, "state.json")))

    def test_refuses_dirty_checkout(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(update.paths, "kit_config_dir", lambda: Path(tmp)):
            repo = self._repo(tmp)
            Path(repo, "dirty").write_text("x")
            rc, _, err = _run(["update", "--repo", repo])
            self.assertEqual(rc, 1)
            self.assertIn("uncommitted", err)

    def test_refuses_non_main_branch(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(update.paths, "kit_config_dir", lambda: Path(tmp)):
            repo = self._repo(tmp)
            _git(repo, "checkout", "-q", "-b", "feat")
            rc, _, err = _run(["update", "--repo", repo])
            self.assertEqual(rc, 1)
            self.assertIn("not main", err)


if __name__ == "__main__":
    unittest.main()
