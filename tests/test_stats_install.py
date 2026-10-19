"""mods/install.sh: idempotent marketplace registration and jev-stats plugin
install. A stub `claude` on PATH stands in for the CLI, so no real
settings.json or installed_plugins.json is touched."""
import tests  # noqa: F401, I001 -- first import: isolates HOME and AIRLOCK_* (see tests/__init__.py)

import os
import shutil
import stat
import subprocess
import unittest
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "mods" / "install.sh"
STUB = """#!/bin/sh
echo "$@" >> "$STUB_LOG"
if [ "$1 $2 $3" = "plugin marketplace list" ]; then
  [ -n "$STUB_MARKETPLACES" ] && echo "$STUB_MARKETPLACES"
  exit 0
fi
if [ "$1 $2" = "plugin list" ]; then
  [ -n "$STUB_PLUGINS" ] && echo "$STUB_PLUGINS"
  exit 0
fi
exit 0
"""


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestStatsInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        stub = Path(self.tmp) / "bin" / "claude"
        stub.parent.mkdir()
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.log = Path(self.tmp) / "calls.log"

    def _run(self, *args, marketplaces="", plugins="", airlock_home=None):
        env = dict(os.environ, HOME=self.tmp,
                   AIRLOCK_HOME=airlock_home or str(Path(self.tmp) / "al"),
                   STUB_LOG=str(self.log), STUB_MARKETPLACES=marketplaces,
                   STUB_PLUGINS=plugins)
        env["PATH"] = str(Path(self.tmp) / "bin") + os.pathsep + env["PATH"]
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)

    def _calls(self, prefix):
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        return [c for c in lines if c.startswith(prefix)]

    def test_fresh_machine_registers_and_installs(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        add = self._calls("plugin marketplace add")
        self.assertEqual(len(add), 1)
        self.assertIn(str(REPO), add[0])  # no release deployed, falls back to checkout
        self.assertEqual(self._calls("plugin install"), ["plugin install jev-stats@jev-kit"])

    def test_registered_marketplace_is_left_alone(self):
        r = self._run(marketplaces="jev-kit  /some/where")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already registered", r.stdout)
        self.assertEqual(self._calls("plugin marketplace add"), [])
        self.assertEqual(self._calls("plugin install"), ["plugin install jev-stats@jev-kit"])

    def test_installed_plugin_is_left_alone(self):
        r = self._run(marketplaces="jev-kit", plugins="jev-stats@jev-kit 0.1.1 enabled")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already installed", r.stdout)
        self.assertEqual(self._calls("plugin install"), [])

    def test_prefers_the_deployed_release(self):
        ah = Path(self.tmp) / "al"
        manifest = ah / "current" / ".claude-plugin"
        manifest.mkdir(parents=True)
        (manifest / "marketplace.json").write_text("{}")
        r = self._run(airlock_home=str(ah))
        self.assertEqual(r.returncode, 0, r.stderr)
        add = self._calls("plugin marketplace add")
        self.assertEqual(len(add), 1)
        self.assertIn(str(ah / "current"), add[0])

    def test_print_only_changes_nothing(self):
        r = self._run("--print-only")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("claude plugin marketplace add", r.stdout)
        self.assertIn("claude plugin install jev-stats@jev-kit", r.stdout)
        self.assertFalse(self.log.exists())

    def test_default_install_plan_includes_stats(self):
        r = subprocess.run(["bash", str(REPO / "install" / "install.sh"), "--check-only"],
                           capture_output=True, text=True,
                           env=dict(os.environ, HOME=self.tmp))
        self.assertIn("stats (claude plugin install jev-stats@jev-kit)", r.stdout)


if __name__ == "__main__":
    unittest.main()
