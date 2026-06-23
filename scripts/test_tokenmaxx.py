import importlib.util
import io
import json
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("tokenmaxx.py")
spec = importlib.util.spec_from_file_location("tokenmaxx", MODULE_PATH)
tokenmaxx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tokenmaxx)


class TokenmaxxTests(unittest.TestCase):
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

    def write_queue(self, text):
        self.queue_path.write_text(text)
        return self.queue_path

    def write_session(self, filename, payload):
        path = self.sessions_dir / filename
        path.write_text(json.dumps(payload))
        return path

    def test_load_queue_skips_blank_lines_and_preserves_item(self):
        path = self.write_queue('\n{"cwd":"/tmp/repo","sessionId":"abc","status":"pending"}\n')
        items = tokenmaxx.load_queue(path)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].cwd, "/tmp/repo")
        self.assertEqual(items[0].session_id, "abc")
        self.assertEqual(items[0].status, "pending")

    def test_append_queue_item_writes_jsonl(self):
        item = tokenmaxx.QueueItem(cwd="/tmp/repo", session_id="abc")
        tokenmaxx.append_queue_item(self.queue_path, item)
        items = tokenmaxx.load_queue(self.queue_path)
        self.assertEqual(items[0].cwd, "/tmp/repo")
        self.assertEqual(items[0].session_id, "abc")

    def test_classify_output_finds_limit_done_and_unknown(self):
        self.assertEqual(tokenmaxx.classify_output("usage limit reached"), "limited")
        self.assertEqual(tokenmaxx.classify_output("Server is temporarily limiting requests"), "limited")
        self.assertEqual(tokenmaxx.classify_output("DONE"), "done")
        self.assertEqual(tokenmaxx.classify_output("still working"), "unknown")

    def test_load_claude_sessions_reads_metadata(self):
        self.write_session(
            "80544.json",
            {
                "pid": 80544,
                "status": "busy",
                "cwd": "/tmp/repo",
                "sessionId": "abc",
                "updatedAt": 1782185388898,
            },
        )
        sessions = tokenmaxx.load_claude_sessions(self.sessions_dir)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["pid"], 80544)
        self.assertEqual(sessions[0]["sessionId"], "abc")
        self.assertEqual(sessions[0]["cwd"], "/tmp/repo")

    def test_add_writes_selected_session_to_queue_by_pid(self):
        self.write_session("80544.json", {"pid": 80544, "status": "busy", "cwd": "/tmp/repo", "sessionId": "abc"})
        with redirect_stdout(io.StringIO()):
            code = tokenmaxx.cmd_add(self.args(pid=80544))
        self.assertEqual(code, 0)
        self.assertEqual(tokenmaxx.load_queue(self.queue_path)[0].session_id, "abc")

    def test_add_writes_selected_session_to_queue_by_session_id(self):
        self.write_session("80544.json", {"pid": 80544, "status": "busy", "cwd": "/tmp/repo", "sessionId": "abc"})
        with redirect_stdout(io.StringIO()):
            code = tokenmaxx.cmd_add(self.args(session_id="abc"))
        self.assertEqual(code, 0)
        self.assertEqual(tokenmaxx.load_queue(self.queue_path)[0].cwd, "/tmp/repo")

    def test_build_resume_command_uses_guarded_prompt(self):
        item = tokenmaxx.QueueItem(cwd="/tmp/repo", session_id="abc")
        command = tokenmaxx.build_resume_command(item, "claude", "continue carefully")
        self.assertEqual(command[:3], ["claude", "--resume", "abc"])
        self.assertEqual(command[-2:], ["-p", "continue carefully"])

    def test_due_filter_respects_next_attempt(self):
        item = tokenmaxx.QueueItem(cwd="/tmp/repo", session_id="abc", next_attempt_at=200)
        self.assertFalse(tokenmaxx.is_due(item, now=100))
        self.assertTrue(tokenmaxx.is_due(item, now=200))

    def test_run_due_item_dry_run_does_not_mark_done(self):
        item = tokenmaxx.QueueItem(cwd="/tmp/repo", session_id="abc")
        result = tokenmaxx.run_due_item(item, now=1000, args=self.args(dry_run=True))
        self.assertEqual(result.status, "pending")
        self.assertIn("claude", result.last_output)

    def test_watch_dry_run_prints_generated_command(self):
        tokenmaxx.append_queue_item(self.queue_path, tokenmaxx.QueueItem(cwd="/tmp/repo", session_id="abc"))
        output = io.StringIO()
        with redirect_stdout(output):
            code = tokenmaxx.cmd_watch(self.args(dry_run=True))
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN: claude --resume abc", output.getvalue())

    def test_update_item_after_limited_output_sets_next_attempt(self):
        item = tokenmaxx.QueueItem(cwd="/tmp/repo", session_id="abc")
        updated = tokenmaxx.update_item_after_output(item, "usage limit reached", now=1000, args=self.args())
        self.assertEqual(updated.status, "pending")
        self.assertEqual(updated.next_attempt_at, 19000)

    def test_update_item_after_done_output_marks_done(self):
        item = tokenmaxx.QueueItem(cwd="/tmp/repo", session_id="abc")
        updated = tokenmaxx.update_item_after_output(item, "DONE", now=1000, args=self.args())
        self.assertEqual(updated.status, "done")

    def test_update_item_blocks_after_max_attempts(self):
        item = tokenmaxx.QueueItem(cwd="/tmp/repo", session_id="abc", attempts=2)
        updated = tokenmaxx.update_item_after_output(item, "still working", now=1000, args=self.args(max_attempts=3))
        self.assertEqual(updated.status, "blocked")
        self.assertIn("max attempts", updated.blocked_reason)

    def test_queue_lock_path_sits_next_to_queue(self):
        self.assertEqual(tokenmaxx.queue_lock_path(self.queue_path), self.root / "queue.jsonl.lock")

    def test_status_prints_next_attempt_and_last_output(self):
        item = tokenmaxx.QueueItem(
            cwd="/tmp/repo",
            session_id="abc",
            status="pending",
            next_attempt_at=9_999_999_999,
            attempts=1,
            last_output="usage limit reached",
        )
        tokenmaxx.append_queue_item(self.queue_path, item)
        output = io.StringIO()
        with redirect_stdout(output):
            tokenmaxx.cmd_status(self.args())
        rendered = output.getvalue()
        self.assertIn("attempts=1", rendered)
        self.assertIn("last=usage limit reached", rendered)
        self.assertIn("next=", rendered)

    def test_build_launchd_plist_contains_watch_command(self):
        plist = tokenmaxx.build_launchd_plist(
            script_path=Path("/repo/scripts/tokenmaxx.py"),
            queue_path=Path("/tmp/queue.jsonl"),
            log_path=Path("/tmp/tokenmaxx.log"),
            interval_seconds=300,
        )
        self.assertIn("<string>/repo/scripts/tokenmaxx.py</string>", plist)
        self.assertIn("<string>watch</string>", plist)
        self.assertIn("<string>--queue</string>", plist)
        self.assertIn("<string>/tmp/queue.jsonl</string>", plist)

    def test_install_dry_run_prints_plist_without_writing(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = tokenmaxx.cmd_install(self.args(dry_run=True))
        self.assertEqual(code, 0)
        self.assertIn("com.local.tokenmaxx", output.getvalue())
        self.assertFalse(self.root.joinpath("com.local.tokenmaxx.plist").exists())


if __name__ == "__main__":
    unittest.main()
