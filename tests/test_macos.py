import tests  # noqa: F401, I001 -- MUST be the first import; see test_policy.py.

"""The macOS port: systemd units rendered as LaunchAgents, Spotlight as the
filename index, and the shell shims. Everything here runs on any POSIX host;
nothing needs a Mac except the one test that asks launchctl a question."""
import importlib.util
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from airlock import paths, platform_compat, policy, scope

REPO_ROOT = Path(__file__).resolve().parent.parent
HOME = "/Users/alice"


def _load_launchd():
    spec = importlib.util.spec_from_file_location(
        "launchd", REPO_ROOT / "install" / "launchd.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launchd = _load_launchd()


def _render(service, timer=None, python="/opt/homebrew/bin/python3"):
    service_path = REPO_ROOT / service
    timer_text = (REPO_ROOT / timer).read_text() if timer else None
    return launchd.render(service_path.read_text(), timer_text,
                          name=service_path.name, home=HOME, python=python)


class TestEveryShippedUnitRenders(unittest.TestCase):
    """The .service/.timer files stay the one description of what runs; a
    unit the translator cannot read would silently leave macOS unscheduled."""

    PAIRS = (
        ("deploy/airlock-daemon.service", None),
        ("monitoring/airlock-health.service", "monitoring/airlock-health.timer"),
        ("tuning/airlock-tune.service", "tuning/airlock-tune.timer"),
        ("claude-update/claude-auto-update.service",
         "claude-update/claude-auto-update.timer"),
    )

    def test_each_renders_to_a_valid_plist(self):
        for service, timer in self.PAIRS:
            with self.subTest(service=service):
                plist = _render(service, timer)
                plistlib.loads(plistlib.dumps(plist))
                self.assertTrue(plist["Label"].startswith(launchd.LABEL_PREFIX))
                self.assertNotIn("%h", " ".join(plist["ProgramArguments"]))


class TestTheDaemon(unittest.TestCase):
    def setUp(self):
        self.plist = _render("deploy/airlock-daemon.service")

    def test_restarts_on_failure_only(self):
        self.assertTrue(self.plist["RunAtLoad"])
        self.assertEqual(self.plist["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(self.plist["ThrottleInterval"], 5)

    def test_the_system_python_is_replaced(self):
        """/usr/bin/python3 on macOS is the Command Line Tools stub, often
        older than the 3.10 the kit needs."""
        self.assertEqual(self.plist["ProgramArguments"][:3],
                         ["/opt/homebrew/bin/python3", "-m", "airlock.daemon"])

    def test_working_directory_is_the_live_release(self):
        self.assertEqual(self.plist["WorkingDirectory"],
                         HOME + "/.local/share/airlock/current")

    def test_path_reaches_user_and_homebrew_bins(self):
        path = self.plist["EnvironmentVariables"]["PATH"].split(":")
        self.assertIn(HOME + "/.local/bin", path)
        self.assertIn("/opt/homebrew/bin", path)


class TestTimers(unittest.TestCase):
    def test_every_five_minutes(self):
        plist = _render("monitoring/airlock-health.service",
                        "monitoring/airlock-health.timer")
        self.assertEqual(plist["StartInterval"], 300)
        self.assertTrue(plist["RunAtLoad"])
        self.assertNotIn("KeepAlive", plist)

    def test_hourly_is_on_the_hour(self):
        plist = _render("claude-update/claude-auto-update.service",
                        "claude-update/claude-auto-update.timer")
        self.assertEqual(plist["StartCalendarInterval"], {"Minute": 0})
        self.assertNotIn("RunAtLoad", plist)

    def test_an_unknown_calendar_is_refused_not_guessed(self):
        with self.assertRaises(ValueError):
            launchd.render("[Service]\nExecStart=/bin/true\n",
                           "[Timer]\nOnCalendar=Mon *-*-* 03:00\n", home=HOME)

    def test_spans(self):
        self.assertEqual(launchd.seconds("30min"), 1800)
        self.assertEqual(launchd.seconds("2min"), 120)
        self.assertEqual(launchd.seconds("300"), 300)
        self.assertEqual(launchd.seconds("1h"), 3600)


class TestUnitParsing(unittest.TestCase):
    def test_environment_and_comments(self):
        plist = launchd.render(
            "# c\n[Service]\nExecStart=%h/bin/x --flag 'a b'\n"
            "Environment=FOO=1 BAR=%h/y\n", home=HOME)
        self.assertEqual(plist["ProgramArguments"], [HOME + "/bin/x", "--flag", "a b"])
        self.assertEqual(plist["EnvironmentVariables"]["FOO"], "1")
        self.assertEqual(plist["EnvironmentVariables"]["BAR"], HOME + "/y")

    def test_no_execstart_is_an_error(self):
        with self.assertRaises(ValueError):
            launchd.render("[Service]\nType=oneshot\n", home=HOME)

    def test_label(self):
        self.assertEqual(launchd.label_for("airlock-health.service"),
                         "com.jev-kit.airlock-health")
        self.assertEqual(launchd.label_for("airlock-health"),
                         "com.jev-kit.airlock-health")


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("launchctl"),
                     "asks the real launchctl")
class TestStatusOnARealMac(unittest.TestCase):
    def test_an_agent_that_does_not_exist_is_not_loaded(self):
        with mock.patch("sys.stdout"):
            self.assertEqual(launchd.status("jev-kit-test-no-such-agent"), 1)


class TestIsMacos(unittest.TestCase):
    def test_injection(self):
        self.assertTrue(platform_compat.is_macos(platform="darwin"))
        self.assertFalse(platform_compat.is_macos(platform="linux"))
        self.assertTrue(platform_compat.is_macos(macos=True, platform="linux"))


class TestRuntimeDir(unittest.TestCase):
    def test_xdg_runtime_dir_still_wins_on_macos(self):
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/tmp/rt"}):
            self.assertEqual(paths._runtime_dir(macos=True), "/tmp/rt")

    def test_macos_without_xdg_uses_library_caches(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
        env["HOME"] = HOME
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(paths._runtime_dir(macos=True),
                             HOME + "/Library/Caches")


class TestSpotlightPolicy(unittest.TestCase):
    """On macOS the index is Spotlight. It covers $HOME minus hidden
    directories, and `mdfind` already IS the indexed search."""

    def setUp(self):
        policy.reset_availability_cache()
        self.addCleanup(policy.reset_availability_cache)
        patcher = mock.patch.dict(os.environ, {"HOME": HOME})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _suggest(self, roots):
        return policy.filename_search_suggestion(
            windows=False, roots=roots, wsl=False,
            db_kind=policy.SPOTLIGHT_KIND, has_es=False)

    def test_home_gets_mdfind(self):
        self.assertEqual(self._suggest([HOME]), policy.SPOTLIGHT_COMMAND)

    def test_a_hidden_directory_is_not_spotlight_ground(self):
        self.assertFalse(policy.root_is_plocate_covered(
            HOME + "/.config", home=HOME, db_kind=policy.SPOTLIGHT_KIND))
        self.assertTrue(policy.root_is_plocate_covered(
            HOME + "/Documents", home=HOME, db_kind=policy.SPOTLIGHT_KIND))

    def test_a_find_of_home_is_steered_to_mdfind(self):
        verdict = policy.evaluate_search(
            scope="disk_wide", search_intent="filename_search",
            confidence=0.95, margin=0.9, command="find ~ -name x.txt",
            root_has_graphify_graph=False, windows=False, wsl=False,
            roots=[HOME], db_kind=policy.SPOTLIGHT_KIND, has_es=False)
        self.assertTrue(verdict["would_deny"])
        self.assertIn("mdfind", verdict["suggestion"])

    def test_mdfind_is_already_the_indexed_tool(self):
        self.assertTrue(policy.command_already_uses_indexed_search(
            "mdfind -onlyin ~ -name x.txt", windows=False, wsl=False))
        self.assertIn("mdfind", policy.SKIP_LOCATE_FAMILY)

    def test_detection_on_a_mac_with_mdfind_and_no_plocate(self):
        on_path = {"mdfind": True, "plocate": False, "locate": False}
        with mock.patch.object(policy, "_tool_on_path", on_path.get), \
                mock.patch.object(policy, "is_macos", lambda *a, **k: True):
            self.assertEqual(policy.plocate_db_kind(), policy.SPOTLIGHT_KIND)

    def test_linux_never_detects_spotlight(self):
        on_path = {"mdfind": True, "plocate": False, "locate": False}
        with mock.patch.object(policy, "_tool_on_path", on_path.get), \
                mock.patch.object(policy, "is_macos", lambda *a, **k: False):
            self.assertIsNone(policy.plocate_db_kind())

    def test_scope_reads_mdfind_as_a_locate(self):
        self.assertIn("mdfind", scope.SEARCH_PROGRAMS)


class TestShellShims(unittest.TestCase):
    def _sh(self, script):
        return subprocess.run(
            ["bash", "-c", '. "%s"; %s' % (REPO_ROOT / "install" / "portable.sh", script)],
            capture_output=True, text=True, timeout=30)

    def test_replace_link_swaps_a_symlink_without_entering_it(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("a", "b"):
                os.makedirs(os.path.join(d, name))
            current = os.path.join(d, "current")
            os.symlink(os.path.join(d, "a"), current)
            tmp = os.path.join(d, ".tmp")
            os.symlink(os.path.join(d, "b"), tmp)
            proc = self._sh('airlock_replace_link "%s" "%s"' % (tmp, current))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(os.readlink(current), os.path.join(d, "b"))
            self.assertEqual(os.listdir(os.path.join(d, "a")), [])

    def test_stat_uid_mode(self):
        with tempfile.NamedTemporaryFile() as f:
            os.chmod(f.name, 0o640)
            proc = self._sh('airlock_stat_uid_mode "%s"' % f.name)
            self.assertEqual(proc.stdout.split(), [str(os.getuid()), "640"])

    def test_timeout_kills_and_passes(self):
        self.assertEqual(self._sh("airlock_timeout 5 true").returncode, 0)
        self.assertNotEqual(self._sh("airlock_timeout 1 sleep 5").returncode, 0)


if __name__ == "__main__":
    unittest.main()
