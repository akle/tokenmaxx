import io
import json
import subprocess
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from tokenmaxx import claude, cli, launchd
from tokenmaxx.queue import (
    QueueItem,
    append_queue_item,
    classify_output,
    is_due,
    load_queue,
    queue_lock_path,
    update_item_after_output,
)


class TokenmaxxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.queue_path = self.root / "queue.jsonl"
        self.sessions_dir = self.root / "sessions"
        self.projects_dir = self.root / "projects"
        self.sessions_dir.mkdir()
        self.projects_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, **kwargs):
        defaults = {
            "queue": self.queue_path,
            "sessions_dir": self.sessions_dir,
            "projects_dir": self.projects_dir,
            "pid": None,
            "session_id": None,
            "cwd": None,
            "retry_delay_seconds": 18_000,
            "followup_delay_seconds": 900,
            "max_attempts": 3,
            "resume_timeout_seconds": 7_200,
            "lock_timeout_seconds": 1,
            "claude_bin": "claude",
            "dry_run": True,
            "once": True,
            "sleep_seconds": 0,
            "now": 1000,
            "auto_queue": True,
            "max_session_age_hours": 24,
            "program": "/usr/local/bin/tokenmaxx",
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

    def write_transcript(self, session_id, text):
        project_dir = self.projects_dir / "project"
        project_dir.mkdir(exist_ok=True)
        path = project_dir / f"{session_id}.jsonl"
        path.write_text(text)
        return path

    def test_queue_round_trip_and_status_helpers(self):
        item = QueueItem(cwd="/tmp/repo", session_id="abc", next_attempt_at=200)
        append_queue_item(self.queue_path, item)

        loaded = load_queue(self.queue_path)
        self.assertEqual(loaded[0].cwd, "/tmp/repo")
        self.assertEqual(loaded[0].session_id, "abc")
        self.assertFalse(is_due(loaded[0], now=100))
        self.assertTrue(is_due(loaded[0], now=200))
        self.assertEqual(queue_lock_path(self.queue_path), self.root / "queue.jsonl.lock")

    def test_classify_output_and_retry_updates(self):
        self.assertEqual(classify_output("usage limit reached"), "limited")
        self.assertEqual(classify_output("ran out of credits"), "limited")
        self.assertEqual(classify_output("Server is temporarily limiting requests"), "limited")
        self.assertEqual(classify_output("DONE"), "done")
        self.assertEqual(classify_output("**DONE.** The work is complete."), "done")
        self.assertEqual(classify_output("Prompt is too long"), "blocked")
        self.assertEqual(classify_output("still working"), "unknown")

        limited = update_item_after_output(
            QueueItem(cwd="/tmp/repo", session_id="abc"),
            "usage limit reached",
            now=1000,
            retry_delay_seconds=18_000,
            followup_delay_seconds=900,
            max_attempts=3,
        )
        self.assertEqual(limited.status, "pending")
        self.assertEqual(limited.next_attempt_at, 19_000)

        mexico = ZoneInfo("America/Mexico_City")
        now = int(datetime(2026, 6, 23, 16, 50, tzinfo=mexico).timestamp())
        reset_limited = update_item_after_output(
            QueueItem(cwd="/tmp/repo", session_id="abc"),
            "You've hit your session limit · resets 5:10pm (America/Mexico_City)",
            now=now,
            retry_delay_seconds=18_000,
            followup_delay_seconds=900,
            max_attempts=3,
        )
        self.assertEqual(reset_limited.status, "pending")
        self.assertEqual(reset_limited.next_attempt_at, int(datetime(2026, 6, 23, 17, 11, tzinfo=mexico).timestamp()))

        temporary_limited = update_item_after_output(
            QueueItem(cwd="/tmp/repo", session_id="abc"),
            "API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited",
            now=1000,
            retry_delay_seconds=18_000,
            followup_delay_seconds=900,
            max_attempts=3,
        )
        self.assertEqual(temporary_limited.status, "pending")
        self.assertEqual(temporary_limited.next_attempt_at, 1900)

        done = update_item_after_output(
            QueueItem(cwd="/tmp/repo", session_id="abc"),
            "DONE",
            now=1000,
            retry_delay_seconds=18_000,
            followup_delay_seconds=900,
            max_attempts=3,
        )
        self.assertEqual(done.status, "done")

        blocked = update_item_after_output(
            QueueItem(cwd="/tmp/repo", session_id="abc", attempts=2),
            "still working",
            now=1000,
            retry_delay_seconds=18_000,
            followup_delay_seconds=900,
            max_attempts=3,
        )
        self.assertEqual(blocked.status, "blocked")
        self.assertIn("max attempts", blocked.blocked_reason)

        not_retryable = update_item_after_output(
            QueueItem(cwd="/tmp/repo", session_id="abc"),
            "Prompt is too long",
            now=1000,
            retry_delay_seconds=18_000,
            followup_delay_seconds=900,
            max_attempts=3,
        )
        self.assertEqual(not_retryable.status, "blocked")
        self.assertIn("not retryable", not_retryable.blocked_reason)

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
        self.write_session("broken.json", {"cwd": "/tmp/no-session"})

        sessions = claude.load_claude_sessions(self.sessions_dir)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["pid"], 80544)
        self.assertEqual(sessions[0]["sessionId"], "abc")
        self.assertEqual(sessions[0]["cwd"], "/tmp/repo")

    def test_add_writes_selected_session_to_queue(self):
        self.write_session("80544.json", {"pid": 80544, "status": "busy", "cwd": "/tmp/repo", "sessionId": "abc"})
        with redirect_stdout(io.StringIO()):
            code = cli.cmd_add(self.args(pid=80544))
        self.assertEqual(code, 0)
        self.assertEqual(load_queue(self.queue_path)[0].session_id, "abc")

    def test_autoqueue_adds_recent_limited_sessions_once(self):
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/already", session_id="already"))
        self.write_session(
            "limited.json",
            {
                "pid": 1,
                "status": "idle",
                "cwd": "/tmp/limited",
                "sessionId": "limited",
                "updatedAt": 999_000,
            },
        )
        self.write_transcript("limited", '{"type":"assistant","message":{"content":[{"text":"You have hit your session limit"}]}}\n')
        self.write_session(
            "old.json",
            {
                "pid": 2,
                "status": "idle",
                "cwd": "/tmp/old",
                "sessionId": "old",
                "updatedAt": 990_000,
            },
        )
        self.write_transcript("old", '{"type":"assistant","message":{"content":[{"text":"You have hit your session limit"}]}}\n')
        self.write_session(
            "not-limited.json",
            {
                "pid": 4,
                "status": "idle",
                "cwd": "/tmp/not-limited",
                "sessionId": "not-limited",
                "updatedAt": 999_000,
            },
        )
        self.write_transcript("not-limited", '{"type":"assistant","message":{"content":[{"text":"All set"}]}}\n')
        self.write_session(
            "already.json",
            {
                "pid": 3,
                "status": "idle",
                "cwd": "/tmp/already",
                "sessionId": "already",
                "updatedAt": 999_000,
            },
        )
        self.write_transcript("already", '{"type":"assistant","message":{"content":[{"text":"You have hit your session limit"}]}}\n')

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_autoqueue(self.args(now=1000, max_session_age_hours=0.0005))

        self.assertEqual(code, 0)
        self.assertIn("Auto-queued 1 session", output.getvalue())
        self.assertEqual([item.session_id for item in load_queue(self.queue_path)], ["already", "limited"])

    def test_run_due_item_dry_run_does_not_mark_done(self):
        item = QueueItem(cwd="/tmp/repo", session_id="abc")
        result = claude.run_due_item(
            item,
            now=1000,
            claude_bin="claude",
            dry_run=True,
            retry_delay_seconds=18_000,
            followup_delay_seconds=900,
            max_attempts=3,
            resume_timeout_seconds=7_200,
        )
        self.assertEqual(result.status, "pending")
        self.assertIn("DRY RUN: claude --resume abc", result.last_output)

    def test_run_due_item_times_out_and_kills_process_group(self):
        item = QueueItem(cwd="/tmp/repo", session_id="abc")
        process = Mock()
        process.pid = 4321
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["claude"], 5),
            subprocess.TimeoutExpired(["claude"], 1),
            ("", ""),
        ]
        process.poll.return_value = None
        fake_os = types.SimpleNamespace(killpg=Mock(), getpgid=Mock(return_value=9876))
        fake_signal = types.SimpleNamespace(SIGTERM=15, SIGKILL=9)

        with patch("tokenmaxx.claude.subprocess.Popen", return_value=process) as popen, patch.object(
            claude, "os", fake_os, create=True
        ), patch.object(claude, "signal", fake_signal, create=True):
            try:
                result = claude.run_due_item(
                    item,
                    now=1000,
                    claude_bin="claude",
                    dry_run=False,
                    retry_delay_seconds=18_000,
                    followup_delay_seconds=900,
                    max_attempts=3,
                    resume_timeout_seconds=5,
                )
            except TypeError as exc:
                self.fail(f"run_due_item should accept a resume timeout: {exc}")

        popen.assert_called_once()
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(fake_os.killpg.call_count, 2)
        self.assertEqual(result.status, "pending")
        self.assertEqual(result.next_attempt_at, 1900)
        self.assertIn("timed out after 5 seconds", result.last_output)

    def test_watch_dry_run_prints_generated_command(self):
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/repo", session_id="abc"))
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_watch(self.args(dry_run=True))
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN: claude --resume abc", output.getvalue())

    def test_watch_autoqueues_before_processing_due_item(self):
        self.write_session(
            "limited.json",
            {
                "pid": 1,
                "status": "idle",
                "cwd": "/tmp/limited",
                "sessionId": "limited",
                "updatedAt": 999_000,
            },
        )
        self.write_transcript("limited", '{"type":"assistant","message":{"content":[{"text":"You have hit your session limit"}]}}\n')
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_watch(self.args(dry_run=True, now=1000))

        self.assertEqual(code, 0)
        self.assertIn("Auto-queued 1 session", output.getvalue())
        self.assertIn("DRY RUN: claude --resume limited", output.getvalue())

    def test_status_prints_next_attempt_and_last_output(self):
        item = QueueItem(
            cwd="/tmp/repo",
            session_id="abc",
            status="pending",
            next_attempt_at=9_999_999_999,
            attempts=1,
            last_output="usage limit reached",
        )
        append_queue_item(self.queue_path, item)
        output = io.StringIO()
        with redirect_stdout(output):
            cli.cmd_status(self.args())
        rendered = output.getvalue()
        self.assertIn("attempts=1", rendered)
        self.assertIn("last=usage limit reached", rendered)
        self.assertIn("next=", rendered)

    def test_build_launchd_plist_contains_watch_command(self):
        plist = launchd.build_launchd_plist(
            program="/usr/local/bin/tokenmaxx",
            queue_path=Path("/tmp/queue.jsonl"),
            log_path=Path("/tmp/tokenmaxx.log"),
            interval_seconds=300,
        )
        self.assertIn("<string>/usr/local/bin/tokenmaxx</string>", plist)
        self.assertIn("<string>watch</string>", plist)
        self.assertIn("<string>--queue</string>", plist)
        self.assertIn("<string>/tmp/queue.jsonl</string>", plist)

    def test_launchd_install_dry_run_prints_plist_without_writing(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_launchd_install(self.args(dry_run=True))
        self.assertEqual(code, 0)
        self.assertIn("com.local.tokenmaxx", output.getvalue())
        self.assertFalse(self.root.joinpath("com.local.tokenmaxx.plist").exists())

    def test_launchd_install_requires_program_when_not_on_path(self):
        error = io.StringIO()
        with patch("tokenmaxx.cli.resolve_default_program", return_value=None), redirect_stderr(error):
            code = cli.cmd_launchd_install(self.args(program=None, dry_run=True))
        self.assertEqual(code, 1)
        self.assertIn("Pass --program", error.getvalue())


if __name__ == "__main__":
    unittest.main()
