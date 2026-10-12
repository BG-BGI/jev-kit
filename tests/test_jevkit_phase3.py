import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jevkit import compaction, profile
from jevkit.cli import main

ROOT = Path(__file__).resolve().parent.parent
SHA = "297fcb5d9e18c58d03351d8e64a8b1b331170bf2"
SCHEMA = {
    "mode": {"type": "string", "default": "tool"},
    "apiKey": {"type": "string", "sensitive": True},
    "minChars": {"type": "number", "default": 4000},
    "compactRead": {"type": "boolean", "default": True},
}


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    rc = main(argv, out, err)
    return rc, out.getvalue(), err.getvalue()


class PinTests(unittest.TestCase):
    def test_pin_is_a_full_sha_and_the_installer_reads_it(self):
        pin = compaction.read_pin(str(ROOT))
        self.assertRegex(pin["commit"], r"^[0-9a-f]{40}$")
        self.assertTrue(pin["url"].startswith("https://"))
        self.assertRegex(pin["min_claude"], r"^\d+\.\d+\.\d+$")
        sh = (ROOT / "compaction" / "install.sh").read_text()
        self.assertNotRegex(sh, r"[0-9a-f]{40}")
        self.assertIn("compaction/PIN", sh)


class ValidateTests(unittest.TestCase):
    def test_types_and_bounds(self):
        self.assertEqual(compaction.validate("minChars", "1200", SCHEMA), 1200)
        self.assertEqual(compaction.validate("minChars", "0.5", SCHEMA), 0.5)
        self.assertIs(compaction.validate("compactRead", "off", SCHEMA), False)
        self.assertEqual(compaction.validate("mode", "both", SCHEMA), "both")

    def test_rejects_bad_input(self):
        for key, raw in (("minChars", "abc"), ("compactRead", "maybe"), ("mode", "all"), ("nope", "1"), ("apiKey", "k")):
            with self.assertRaises(ValueError, msg=key):
                compaction.validate(key, raw, SCHEMA)

    def test_not_installed(self):
        with self.assertRaises(ValueError):
            compaction.validate("mode", "tool", None)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = os.path.join(self.tmp.name, "settings.json")
        Path(self.settings).write_text(json.dumps(
            {"model": "x", "pluginConfigs": {compaction.PLUGIN_ID: {"options": {"mode": "tool"}}}}, indent=2) + "\n")
        p = mock.patch.object(compaction, "schema", lambda pin: SCHEMA)
        p.start()
        self.addCleanup(p.stop)

    def _data(self):
        return json.loads(Path(self.settings).read_text())

    def test_set_dry_run_writes_nothing(self):
        rc, out, _ = _run(["compaction", "--settings-file", self.settings, "set", "minChars", "1200"])
        self.assertEqual(rc, 0)
        self.assertIn("dry run", out)
        self.assertNotIn("minChars", self._data()["pluginConfigs"][compaction.PLUGIN_ID]["options"])

    def test_set_yes_writes_with_backup_and_keeps_other_keys(self):
        rc, out, _ = _run(["compaction", "--settings-file", self.settings, "set", "minChars", "1200", "--yes"])
        self.assertEqual(rc, 0)
        data = self._data()
        self.assertEqual(data["pluginConfigs"][compaction.PLUGIN_ID]["options"], {"mode": "tool", "minChars": 1200})
        self.assertEqual(data["model"], "x")
        self.assertTrue([n for n in os.listdir(self.tmp.name) if ".bak." in n])

    def test_mode_change_needs_no_reinstall(self):
        _run(["compaction", "--settings-file", self.settings, "set", "mode", "both", "--yes"])
        self.assertEqual(self._data()["pluginConfigs"][compaction.PLUGIN_ID]["options"]["mode"], "both")

    def test_invalid_value_is_rejected_and_nothing_written(self):
        before = Path(self.settings).read_text()
        rc, _, err = _run(["compaction", "--settings-file", self.settings, "set", "minChars", "lots", "--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("expected a number", err)
        self.assertEqual(before, Path(self.settings).read_text())

    def test_unset_and_get(self):
        rc, out, _ = _run(["compaction", "--settings-file", self.settings, "get"])
        self.assertIn('mode               "tool"', out)
        self.assertIn("(default)", out)
        self.assertNotIn("apiKey", out)
        _run(["compaction", "--settings-file", self.settings, "unset", "mode", "--yes"])
        self.assertEqual(self._data()["pluginConfigs"][compaction.PLUGIN_ID]["options"], {})

    def test_ensure_settings_is_idempotent(self):
        d = compaction.ensure_settings({})
        d = compaction.ensure_settings(d)
        self.assertEqual(d["env"][compaction.HOOKS_FLAG], "1")
        self.assertEqual(d["permissions"]["allow"], [compaction.ALLOW_ENTRY])


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = os.path.join(self.tmp.name, "settings.json")
        Path(self.settings).write_text("{}\n")
        env = mock.patch.dict(os.environ, {"COMPACTION_HOME": os.path.join(self.tmp.name, "home"),
                                           "CLAUDE_CONFIG_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def _ns(self, **kw):
        import types
        base = dict(settings_file=self.settings, yes=False, mode="tool", purge=False)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_install_dry_run_shows_egress_and_runs_nothing(self):
        calls = []
        out = io.StringIO()
        rc = compaction.cmd_install(self._ns(), out, io.StringIO(), runner=lambda *a, **k: calls.append(a) or 0)
        self.assertEqual((rc, calls), (0, []))
        self.assertIn("TypeSafe", out.getvalue())
        self.assertEqual(Path(self.settings).read_text(), "{}\n")

    def test_install_yes_edits_settings_then_runs_installer_with_mode(self):
        seen = {}

        def runner(cmd, env=None):
            seen["cmd"], seen["mode"] = cmd, env["COMPACTION_MODE"]
            return 0
        rc = compaction.cmd_install(self._ns(yes=True, mode="both"), io.StringIO(), io.StringIO(), runner=runner)
        self.assertEqual(rc, 0)
        self.assertEqual(seen["mode"], "both")
        self.assertTrue(seen["cmd"][1].endswith(os.path.join("compaction", "install.sh")))
        data = json.loads(Path(self.settings).read_text())
        self.assertEqual(data["env"][compaction.HOOKS_FLAG], "1")

    def test_update_up_to_date_and_not_installed(self):
        with mock.patch.object(compaction, "installed_sha12", lambda: None):
            self.assertEqual(compaction.cmd_update(self._ns(), io.StringIO(), io.StringIO()), 1)
        with mock.patch.object(compaction, "installed_sha12", lambda: SHA[:12]):
            out = io.StringIO()
            self.assertEqual(compaction.cmd_update(self._ns(), out, io.StringIO()), 0)
            self.assertIn("up to date", out.getvalue())

    def test_update_behind_dry_run_then_yes_keeps_mode(self):
        Path(self.settings).write_text(json.dumps(
            {"pluginConfigs": {compaction.PLUGIN_ID: {"options": {"mode": "session"}}}}))
        calls = []

        def runner(cmd, env=None):
            calls.append((cmd, (env or {}).get("COMPACTION_MODE")))
            return 0
        with mock.patch.object(compaction, "installed_sha12", lambda: "aaaaaaaaaaaa"):
            compaction.cmd_update(self._ns(), io.StringIO(), io.StringIO(), runner=runner)
            self.assertEqual(calls, [])
            compaction.cmd_update(self._ns(yes=True), io.StringIO(), io.StringIO(), runner=runner)
        self.assertEqual(calls[0][0][:4], ["claude", "plugin", "marketplace", "remove"])
        self.assertEqual(calls[1][1], "session")

    def test_remove_drops_config_and_permission_keeps_hooks_flag(self):
        Path(self.settings).write_text(json.dumps({
            "env": {compaction.HOOKS_FLAG: "1"},
            "permissions": {"allow": [compaction.ALLOW_ENTRY, "Bash(ls)"]},
            "pluginConfigs": {compaction.PLUGIN_ID: {"options": {"mode": "tool"}}}}))
        calls = []
        compaction.cmd_remove(self._ns(), io.StringIO(), io.StringIO(), runner=lambda c: calls.append(c) or 0)
        self.assertEqual(calls, [])
        compaction.cmd_remove(self._ns(yes=True), io.StringIO(), io.StringIO(), runner=lambda c: calls.append(c) or 0)
        data = json.loads(Path(self.settings).read_text())
        self.assertNotIn("pluginConfigs", data)
        self.assertEqual(data["permissions"]["allow"], ["Bash(ls)"])
        self.assertEqual(data["env"][compaction.HOOKS_FLAG], "1")
        self.assertEqual(len(calls), 2)


class ProfileTests(unittest.TestCase):
    def test_set_get_and_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(profile.paths, "kit_config_dir", lambda: Path(tmp)), \
                mock.patch("jevkit.common.paths.kit_config_dir", lambda: Path(tmp)):
            rc, out, _ = _run(["profile", "set", "owner"])
            self.assertIn("dry run", out)
            self.assertFalse((Path(tmp) / "profile.json").exists())
            _run(["profile", "set", "shared", "--yes"])
            rc, out, _ = _run(["profile", "get"])
            self.assertEqual(out.strip(), "shared")


if __name__ == "__main__":
    unittest.main()
