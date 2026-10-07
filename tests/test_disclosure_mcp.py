import tests  # noqa: F401, I001 -- MUST be the first import; see tests/__init__.py.

import contextlib
import datetime
import io
import json
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from airlock import paths
from disclosure import mcp


def _ts(days_ago):
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _assistant(cwd, ts, tool_name):
    return json.dumps({
        "type": "assistant",
        "cwd": cwd,
        "timestamp": ts,
        "message": {"content": [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "name": tool_name, "input": {}},
        ]},
    })


class TestDisclosureMcp(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.project = self.tmp / "proj"
        self.other = self.tmp / "other"
        self.state = self.tmp / "state"
        for d in (self.home, self.project, self.other, self.state):
            d.mkdir()
        p = mock.patch.object(paths, "state_dir", return_value=self.state)
        p.start()
        self.addCleanup(p.stop)

        tdir = self.home / ".claude" / "projects" / "-proj"
        tdir.mkdir(parents=True)
        lines = [
            _assistant(str(self.project), _ts(2), "mcp__github__search"),
            _assistant(str(self.project), _ts(100), "mcp__linear__create_issue"),
            "this is not json {",
            json.dumps({"type": "user", "cwd": str(self.project),
                        "message": {"content": [{"type": "tool_use",
                                                 "name": "mcp__ignored__x"}]}}),
            json.dumps({"type": "assistant"}),  # no cwd, no message
            _assistant(str(self.other), _ts(1), "mcp__slack__post"),
        ]
        (tdir / "s1.jsonl").write_text("\n".join(lines) + "\n")

        (self.project / ".mcp.json").write_text(json.dumps(
            {"mcpServers": {"github": {}, "linear": {}, "dormant": {}}}))
        (self.home / ".claude.json").write_text(json.dumps(
            {"mcpServers": {"userglobal": {}}, "theme": "dark"}))

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = mcp.main(list(argv))
        return rc, out.getvalue()

    @property
    def settings_path(self):
        return self.project / ".claude" / "settings.local.json"

    @property
    def manifest_path(self):
        return self.state / "mcp-disclosure.json"

    def settings(self):
        return json.loads(self.settings_path.read_text())

    # ------------------------------------------------------------- scanning

    def test_scan_extracts_servers_per_cwd(self):
        usage = mcp.scan_usage(str(self.home))
        self.assertEqual(set(usage[str(self.project)]), {"github", "linear"})
        self.assertEqual(set(usage[str(self.other)]), {"slack"})
        # user records and garbage lines never register a use
        for seen in usage.values():
            self.assertNotIn("ignored", seen)

    def test_scan_keeps_newest_timestamp(self):
        tdir = self.home / ".claude" / "projects" / "-proj"
        (tdir / "s2.jsonl").write_text(
            _assistant(str(self.project), _ts(50), "mcp__github__search") + "\n")
        usage = mcp.scan_usage(str(self.home))
        age = datetime.datetime.now(datetime.timezone.utc) - usage[str(self.project)]["github"]
        self.assertLess(age.days, 10)

    # --------------------------------------------------------------- report

    def test_report_classifies_by_window(self):
        rc, out = self.run_cli("report", "--home", str(self.home),
                               "--project", str(self.project), "--days", "30")
        self.assertEqual(rc, 0)
        by_server = {}
        for line in out.splitlines():
            for s in ("github", "linear", "dormant", "userglobal"):
                if line.strip().startswith(s):
                    by_server[s] = line
        self.assertIn("used-within-window", by_server["github"])
        self.assertIn("unused (last used", by_server["linear"])
        self.assertIn("unused (never)", by_server["dormant"])
        self.assertIn("unused (never)", by_server["userglobal"])
        self.assertIn("recommend:", out)

    def test_report_wide_window_counts_old_use(self):
        rc, out = self.run_cli("report", "--home", str(self.home),
                               "--project", str(self.project), "--days", "365")
        self.assertEqual(rc, 0)
        linear = [l for l in out.splitlines() if l.strip().startswith("linear")][0]
        self.assertIn("used-within-window", linear)

    def test_report_all_projects_from_transcripts(self):
        rc, out = self.run_cli("report", "--home", str(self.home))
        self.assertEqual(rc, 0)
        self.assertIn(str(self.project), out)
        self.assertIn(str(self.other), out)

    # ---------------------------------------------------------------- apply

    def test_apply_writes_disabled_preserves_keys_bak_manifest(self):
        original = {"permissions": {"allow": ["Bash"]},
                    "disabledMcpjsonServers": ["usermade"]}
        self.settings_path.parent.mkdir(parents=True)
        self.settings_path.write_text(json.dumps(original))

        rc, out = self.run_cli("apply", "--home", str(self.home),
                               "--project", str(self.project))
        self.assertEqual(rc, 0)
        got = self.settings()
        self.assertEqual(got["disabledMcpjsonServers"],
                         ["dormant", "linear", "usermade"])
        self.assertEqual(got["permissions"], {"allow": ["Bash"]})
        bak = json.loads((self.settings_path.parent / "settings.local.json.bak").read_text())
        self.assertEqual(bak, original)
        manifest = json.loads(self.manifest_path.read_text())
        self.assertEqual(manifest[str(self.project)]["added"], ["dormant", "linear"])
        self.assertIn("ts", manifest[str(self.project)])

    def test_apply_creates_settings_when_missing(self):
        rc, _ = self.run_cli("apply", "--home", str(self.home),
                             "--project", str(self.project))
        self.assertEqual(rc, 0)
        self.assertEqual(self.settings()["disabledMcpjsonServers"],
                         ["dormant", "linear"])
        # no pre-existing file, so nothing to back up
        self.assertFalse((self.settings_path.parent / "settings.local.json.bak").exists())

    def test_apply_is_idempotent(self):
        self.run_cli("apply", "--home", str(self.home), "--project", str(self.project))
        first = self.settings()
        rc, out = self.run_cli("apply", "--home", str(self.home),
                               "--project", str(self.project))
        self.assertEqual(rc, 0)
        self.assertIn("nothing to disable", out)
        self.assertEqual(self.settings(), first)
        manifest = json.loads(self.manifest_path.read_text())
        self.assertEqual(manifest[str(self.project)]["added"], ["dormant", "linear"])

    def test_apply_nothing_to_do_writes_nothing(self):
        (self.project / ".mcp.json").write_text(json.dumps({"mcpServers": {"github": {}}}))
        rc, out = self.run_cli("apply", "--home", str(self.home),
                               "--project", str(self.project))
        self.assertEqual(rc, 0)
        self.assertIn("nothing to disable", out)
        self.assertFalse(self.settings_path.exists())
        self.assertFalse(self.manifest_path.exists())

    def test_apply_never_writes_user_scope_only_reports(self):
        before = (self.home / ".claude.json").read_bytes()
        rc, out = self.run_cli("apply", "--home", str(self.home),
                               "--project", str(self.project))
        self.assertEqual(rc, 0)
        self.assertEqual((self.home / ".claude.json").read_bytes(), before)
        self.assertIn('user-scope server "userglobal" unused', out)
        self.assertNotIn("userglobal", self.settings().get("disabledMcpjsonServers", []))

    # -------------------------------------------------------------- restore

    def test_restore_removes_only_manifest_tracked_entries(self):
        self.settings_path.parent.mkdir(parents=True)
        self.settings_path.write_text(json.dumps(
            {"disabledMcpjsonServers": ["usermade"]}))
        self.run_cli("apply", "--home", str(self.home), "--project", str(self.project))

        rc, out = self.run_cli("restore", "--home", str(self.home),
                               "--project", str(self.project))
        self.assertEqual(rc, 0)
        self.assertEqual(self.settings()["disabledMcpjsonServers"], ["usermade"])
        manifest = json.loads(self.manifest_path.read_text())
        self.assertNotIn(str(self.project), manifest)

    def test_restore_without_manifest_entry_is_a_noop(self):
        rc, out = self.run_cli("restore", "--home", str(self.home),
                               "--project", str(self.project))
        self.assertEqual(rc, 0)
        self.assertIn("nothing to restore", out)
        self.assertFalse(self.settings_path.exists())

    # ------------------------------------------------------------ fail open

    def test_corrupt_settings_means_no_crash_no_write(self):
        self.settings_path.parent.mkdir(parents=True)
        self.settings_path.write_text("{not json")
        rc, out = self.run_cli("apply", "--home", str(self.home),
                               "--project", str(self.project))
        self.assertEqual(rc, 0)
        self.assertIn("changing nothing", out)
        self.assertEqual(self.settings_path.read_text(), "{not json")
        self.assertFalse((self.settings_path.parent / "settings.local.json.bak").exists())
        self.assertFalse(self.manifest_path.exists())

    def test_corrupt_manifest_means_no_crash_no_write(self):
        self.manifest_path.write_text("][")
        rc, out = self.run_cli("apply", "--home", str(self.home),
                               "--project", str(self.project))
        self.assertEqual(rc, 0)
        self.assertFalse(self.settings_path.exists())
        self.assertEqual(self.manifest_path.read_text(), "][")
        rc, _ = self.run_cli("restore", "--home", str(self.home),
                             "--project", str(self.project))
        self.assertEqual(rc, 0)
        self.assertEqual(self.manifest_path.read_text(), "][")

    def test_corrupt_mcp_json_and_claude_json_tolerated(self):
        (self.project / ".mcp.json").write_text("not json at all")
        (self.home / ".claude.json").write_text("{{{{")
        rc, out = self.run_cli("report", "--home", str(self.home),
                               "--project", str(self.project))
        self.assertEqual(rc, 0)
        rc, out = self.run_cli("apply", "--home", str(self.home),
                               "--project", str(self.project))
        self.assertEqual(rc, 0)
        self.assertIn("nothing to disable", out)
        self.assertFalse(self.settings_path.exists())

    def test_corrupt_transcript_record_fields_tolerated(self):
        tdir = self.home / ".claude" / "projects" / "-junk"
        tdir.mkdir(parents=True)
        (tdir / "bad.jsonl").write_text("\n".join([
            json.dumps({"type": "assistant", "cwd": 7, "message": {"content": "x"}}),
            json.dumps({"type": "assistant", "cwd": str(self.project),
                        "timestamp": "garbage",
                        "message": {"content": [{"type": "tool_use", "name": "mcp__"}]}}),
            json.dumps([1, 2, 3]),
            "",
        ]) + "\n")
        usage = mcp.scan_usage(str(self.home))
        self.assertEqual(set(usage[str(self.project)]), {"github", "linear"})


if __name__ == "__main__":
    unittest.main()
