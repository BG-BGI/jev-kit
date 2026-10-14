"""decide/install.sh: handshake, idempotent registration. A stub `claude` on
PATH stands in for the CLI, so no real ~/.claude.json is touched."""
import tests  # noqa: F401, I001 -- first import: isolates HOME and AIRLOCK_* (see tests/__init__.py)

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "decide" / "install.sh"
SERVER = SCRIPT.parent / "server.py"
STUB = """#!/bin/sh
echo "$@" >> "$STUB_LOG"
if [ "$1 $2" = "mcp get" ]; then
  [ -n "$STUB_REGISTERED" ] && { echo "Args: $STUB_REGISTERED"; exit 0; }
  exit 1
fi
exit 0
"""


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestDecideInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        stub = Path(self.tmp) / "bin" / "claude"
        stub.parent.mkdir()
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.log = Path(self.tmp) / "calls.log"

    def _run(self, *args, registered="", with_claude=True):
        env = dict(os.environ, HOME=self.tmp, AIRLOCK_HOME=str(Path(self.tmp) / "al"),
                   STUB_LOG=str(self.log), STUB_REGISTERED=registered)
        env["PATH"] = (str(Path(self.tmp) / "bin") + os.pathsep if with_claude else "") + env["PATH"]
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)

    def _calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_registers_through_claude_mcp_add(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        add = [c for c in self._calls() if c.startswith("mcp add")]
        self.assertEqual(len(add), 1)
        self.assertIn("--scope user jev-decide --", add[0])
        self.assertIn(str(SERVER), add[0])

    def test_already_registered_at_this_path_is_left_alone(self):
        r = self._run(registered=str(SERVER))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already registered at this path", r.stdout)
        self.assertEqual([c for c in self._calls() if c.startswith("mcp add")], [])

    def test_registered_elsewhere_is_not_overwritten(self):
        r = self._run(registered="/elsewhere/server.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("different path", r.stderr)
        self.assertEqual([c for c in self._calls() if c.startswith("mcp add")], [])

    def test_print_only_registers_nothing(self):
        r = self._run("--print-only")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("claude mcp add --scope user jev-decide", r.stdout)
        self.assertEqual(self._calls(), [])

    def test_handshake_is_checked_and_reported(self):
        self.assertIn("handshake ok (tool listed, instructions sent)", self._run("--print-only").stdout)


if __name__ == "__main__":
    unittest.main()
