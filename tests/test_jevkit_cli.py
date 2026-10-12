import tests  # noqa: F401, I001 -- MUST be the first import; see tests/__init__.py.

import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from airlock import mode as airlock_mode
from airlock import paths, rules
from jevkit import cli, common, config, rollback, status


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "cfg"
        self.cfg.mkdir()
        for target, value in ((airlock_mode, "MODE_FILE"), (rules, "CONFIG_FILE")):
            name = "mode" if value == "MODE_FILE" else "rules.json"
            p = mock.patch.object(target, value, str(self.cfg / name))
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {"AIRLOCK_CONFIG_DIR": str(self.cfg), "JEVKIT_CONFIG_DIR": str(self.tmp / "kit")})
        p.start()
        self.addCleanup(p.stop)
        for var in airlock_mode.MODE_ENV + ("AIRLOCK_DISABLE",):
            os.environ.pop(var, None)
        self.out = io.StringIO()

    def set(self, key, value, yes=True, force=False):
        return config.set_value(key, value, yes, self.out, force=force)


class TestConfig(Base):
    def test_dry_run_writes_nothing(self):
        self.set("mode", "enforce", yes=False)
        self.assertFalse((self.cfg / "mode").exists())
        self.assertIn("dry run", self.out.getvalue())

    def test_yes_writes_and_backs_up(self):
        (self.cfg / "mode").write_text("shadow\n")
        self.set("mode", "enforce")
        self.assertEqual((self.cfg / "mode").read_text(), "enforce\n")
        self.assertEqual(len(list(self.cfg.glob("mode.bak.*"))), 1)
        self.assertEqual(config.get("mode"), ("enforce", "file"))

    def test_invalid_values_are_rejected(self):
        with self.assertRaises(config.ConfigError):
            self.set("mode", "loud")
        with self.assertRaises(config.ConfigError):
            self.set("rule.R5-sudo", "explode")
        with self.assertRaises(config.ConfigError):
            self.set("rule.R999-nope", "deny")
        with self.assertRaises(config.ConfigError):
            self.set("nonsense", "x")
        self.assertEqual(list(self.cfg.iterdir()), [])

    def test_shared_machine_needs_the_flag_for_enforce(self):
        common.write_text(paths.kit_config_dir() / "profile.json", json.dumps({"profile": "shared"}), True, self.out)
        with self.assertRaises(config.ConfigError):
            self.set("mode", "enforce")
        self.set("mode", "enforce", force=True)
        self.set("mode", "shadow")  # lowering is never blocked

    def test_kill_switch_round_trip(self):
        self.assertEqual(config.get("kill-switch")[0], "off")
        self.set("kill-switch", "on")
        self.assertEqual(config.get("kill-switch")[0], "on")
        self.set("kill-switch", "off")
        self.assertFalse((self.cfg / "disabled").exists())

    def test_r6_and_rule_overrides_keep_the_file_format(self):
        self.set("r6", "on")
        self.assertEqual(json.loads((self.cfg / "rules.json").read_text()), {"R6-gui-or-browser": "deny"})
        self.assertEqual(config.get("r6"), ("on", "rules.json"))
        (self.cfg / "rules.json").write_text(json.dumps({"rules": {"R5-sudo": "warn"}}))
        self.set("rule.R7-destructive", "ask")
        data = json.loads((self.cfg / "rules.json").read_text())
        self.assertEqual(data, {"rules": {"R5-sudo": "warn", "R7-destructive": "ask"}})
        config.unset("rule.R5-sudo", True, self.out)
        self.assertEqual(json.loads((self.cfg / "rules.json").read_text()), {"rules": {"R7-destructive": "ask"}})

    def test_env_override_is_reported(self):
        with mock.patch.dict(os.environ, {"AIRLOCK_MODE": "off"}):
            self.assertEqual(config.get("mode"), ("off", "env"))
            self.set("mode", "enforce")
        self.assertIn("env override", self.out.getvalue())


class TestRollback(Base):
    def make(self, names, current):
        home = self.tmp / "home"
        for i, n in enumerate(names):
            d = home / "releases" / n
            d.mkdir(parents=True)
            os.utime(d, (1000 + i, 1000 + i))
        os.symlink(home / "releases" / current, home / "current")
        return home

    def test_previous_release_and_apply(self):
        home = self.make(["a", "b", "c"], "c")
        self.assertEqual(rollback.previous_release(str(home)), ("c", "b"))
        args = mock.Mock(yes=True, settings=False)
        with mock.patch.object(paths, "install_home", return_value=home):
            self.assertEqual(rollback.run(args, self.out, io.StringIO()), 0)
        self.assertEqual(os.path.basename(os.path.realpath(home / "current")), "b")

    def test_dry_run_and_no_older_release(self):
        home = self.make(["a", "b"], "b")
        args = mock.Mock(yes=False, settings=False)
        with mock.patch.object(paths, "install_home", return_value=home):
            rollback.run(args, self.out, io.StringIO())
        self.assertEqual(os.path.basename(os.path.realpath(home / "current")), "b")
        self.assertEqual(rollback.previous_release(str(self.tmp / "none")), (None, None))
        self.assertEqual(rollback.previous_release(str(home)), ("b", "a"))
        os.utime(home / "releases" / "a", (5000, 5000))  # oldest is now the current one's newer sibling
        self.assertEqual(rollback.previous_release(str(home)), ("b", None))

    def test_settings_restore_uses_newest_backup(self):
        settings = self.tmp / "settings.json"
        settings.write_text("now")
        (self.tmp / "settings.json.bak.20260101T000000").write_text("old")
        (self.tmp / "settings.json.bak.20260202T000000").write_text("newer")
        self.assertTrue(rollback.newest_backup(str(settings)).endswith("20260202T000000"))


class TestStatusAndCli(Base):
    def test_status_collect_and_warnings(self):
        claude = self.tmp / "claude"
        (claude / "plugins").mkdir(parents=True)
        (claude / "settings.json").write_text(json.dumps({
            "pluginConfigs": {status.COMPACTION_ID: {"options": {"mode": "tool"}}},
            "env": {"CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "1"}}))
        (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(
            {"plugins": {"jev-stats@jev-kit": [{"version": "0.1.0"}], "other@x": [{"version": "1"}]}}))
        s = status.collect(str(claude), [str(claude / "settings.json")])
        self.assertEqual(s["plugins"], {"jev-stats@jev-kit": "0.1.0"})
        self.assertEqual(s["compaction"]["options"], {"mode": "tool"})
        self.assertTrue(s["compaction"]["function_hooks"])
        self.assertFalse(s["wired"][str(claude / "settings.json")])
        self.assertIn("the guard hook is not wired into settings.json", status.warnings(s))
        self.assertNotIn("TYPESAFE_API_KEY=", json.dumps(s))

    def test_cli_routes_status_json_and_config(self):
        out = io.StringIO()
        with mock.patch.object(status, "collect", return_value={
                "release": {"current": None}, "mode": {"value": "shadow", "source": "default"}, "kill_switch": "off",
                "profile": None, "key_present": True, "wired": {}, "plugins": {}, "pricing_age_h": None,
                "compaction": {"configured": False, "options": {}, "function_hooks": False}, "metrics": None}):
            self.assertEqual(cli.main(["status", "--json"], out, io.StringIO()), 0)
        self.assertEqual(json.loads(out.getvalue())["mode"]["value"], "shadow")
        out = io.StringIO()
        self.assertEqual(cli.main(["config", "get", "kill-switch"], out, io.StringIO()), 0)
        self.assertEqual(out.getvalue().strip(), "off")
        err = io.StringIO()
        self.assertEqual(cli.main(["config", "set", "mode", "bogus"], io.StringIO(), err), 2)

    def test_passthrough_runs_the_airlock_module(self):
        with mock.patch.object(cli.subprocess, "call", return_value=0) as call:
            self.assertEqual(cli.main(["pricing", "jev-rates"]), 0)
        argv = call.call_args[0][0]
        self.assertEqual(argv[1:], ["-m", "airlock.pricing", "jev-rates"])


if __name__ == "__main__":
    unittest.main()
