import io
import json
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tokenmaxx import claude, cli, launchd
from tokenmaxx.queue import QueueItem, append_queue_item, load_queue


class TokenmaxxPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.queue_path = self.root / "queue.jsonl"
        self.sessions_dir = self.root / "sessions"
        self.sessions_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, **kwargs):
        defaults = {
            "queue": self.queue_path,
            "sessions_dir": self.sessions_dir,
            "pid": None,
            "session_id": None,
            "cwd": None,
            "retry_delay_seconds": 18000,
            "followup_delay_seconds": 900,
            "max_attempts": 3,
            "lock_timeout_seconds": 1,
            "claude_bin": "claude",
            "dry_run": True,
            "once": True,
            "sleep_seconds": 0,
            "now": 1000,
            "script_path": self.root / "tokenmaxx.py",
            "plist_path": self.root / "com.local.tokenmaxx.plist",
            "log_path": self.root / "tokenmaxx.log",
            "interval_seconds": 300,
        }
        defaults.update(kwargs)
        return types.SimpleNamespace(**defaults)

    def write_session(self, filename, payload):
        path = self.sessions_dir / filename
        path.write_text(json.dumps(payload))
        return path

    def test_package_loads_queue_items(self):
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/repo", session_id="abc"))
        items = load_queue(self.queue_path)
        self.assertEqual(items[0].cwd, "/tmp/repo")
        self.assertEqual(items[0].session_id, "abc")

    def test_package_loads_claude_sessions(self):
        self.write_session("80544.json", {"pid": 80544, "status": "busy", "cwd": "/tmp/repo", "sessionId": "abc"})
        sessions = claude.load_claude_sessions(self.sessions_dir)
        self.assertEqual(sessions[0]["sessionId"], "abc")

    def test_package_install_dry_run_does_not_write_plist(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_install(self.args(dry_run=True))
        self.assertEqual(code, 0)
        self.assertIn("com.local.tokenmaxx", output.getvalue())
        self.assertFalse(self.root.joinpath("com.local.tokenmaxx.plist").exists())

    def test_launchd_plist_uses_tokenmaxx_command(self):
        plist = launchd.build_launchd_plist(
            program="tokenmaxx",
            queue_path=Path("/tmp/queue.jsonl"),
            log_path=Path("/tmp/tokenmaxx.log"),
            interval_seconds=300,
        )
        self.assertIn("<string>tokenmaxx</string>", plist)
        self.assertIn("<string>watch</string>", plist)


if __name__ == "__main__":
    unittest.main()
