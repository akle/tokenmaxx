import io
import json
import os
import re
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
    merge_resumed_item,
    queue_lock_path,
    update_item_after_output,
)

DEAD_PID = 4_190_000  # above macOS/Linux default pid ranges, never a live process


def synthetic_line(text):
    return json.dumps(
        {
            "type": "assistant",
            "message": {"model": "<synthetic>", "role": "assistant", "content": [{"type": "text", "text": text}]},
        }
    ) + "\n"


def assistant_line(text):
    return json.dumps(
        {
            "type": "assistant",
            "message": {"model": "claude-sonnet-4-6", "role": "assistant", "content": [{"type": "text", "text": text}]},
        }
    ) + "\n"


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
            "lines": 80,
            "follow": False,
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
        self.assertEqual(classify_output("You've hit your limit · resets 7pm (America/Mexico_City)"), "limited")
        self.assertEqual(classify_output("DONE"), "done")
        self.assertEqual(classify_output("**DONE.** The work is complete."), "done")
        self.assertEqual(classify_output("DONE. Resumed after the usage limit reset and finished."), "done")
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

        dated_now = int(datetime(2026, 4, 20, 10, 0, tzinfo=mexico).timestamp())
        dated_limited = update_item_after_output(
            QueueItem(cwd="/tmp/repo", session_id="abc"),
            "You've hit your limit · resets Apr 23, 2pm (America/Mexico_City)",
            now=dated_now,
            retry_delay_seconds=18_000,
            followup_delay_seconds=900,
            max_attempts=3,
        )
        self.assertEqual(dated_limited.status, "pending")
        self.assertEqual(dated_limited.next_attempt_at, int(datetime(2026, 4, 23, 14, 1, tzinfo=mexico).timestamp()))

        rollover_now = int(datetime(2026, 12, 30, 10, 0, tzinfo=mexico).timestamp())
        rollover_limited = update_item_after_output(
            QueueItem(cwd="/tmp/repo", session_id="abc"),
            "You've hit your limit · resets Jan 2, 2pm (America/Mexico_City)",
            now=rollover_now,
            retry_delay_seconds=18_000,
            followup_delay_seconds=900,
            max_attempts=3,
        )
        self.assertEqual(rollover_limited.status, "pending")
        self.assertEqual(rollover_limited.next_attempt_at, int(datetime(2027, 1, 2, 14, 1, tzinfo=mexico).timestamp()))

        at_now = int(datetime(2026, 5, 9, 10, 0, tzinfo=mexico).timestamp())
        at_limited = update_item_after_output(
            QueueItem(cwd="/tmp/repo", session_id="abc"),
            "You've hit your limit · resets May 12 at 2pm (America/Mexico_City)",
            now=at_now,
            retry_delay_seconds=18_000,
            followup_delay_seconds=900,
            max_attempts=3,
        )
        self.assertEqual(at_limited.status, "pending")
        self.assertEqual(at_limited.next_attempt_at, int(datetime(2026, 5, 12, 14, 1, tzinfo=mexico).timestamp()))

        # "Feb 29" rolled into a non-leap year must not crash the daemon.
        leap_now = int(datetime(2028, 3, 1, 10, 0, tzinfo=mexico).timestamp())
        leap_limited = update_item_after_output(
            QueueItem(cwd="/tmp/repo", session_id="abc"),
            "usage limit reached · resets Feb 29, 2pm (America/Mexico_City)",
            now=leap_now,
            retry_delay_seconds=18_000,
            followup_delay_seconds=900,
            max_attempts=3,
        )
        self.assertEqual(leap_limited.status, "pending")
        self.assertEqual(leap_limited.next_attempt_at, leap_now + 18_000)

        # A stale quoted date far in the past must fall back to the retry
        # delay, not park the item for ~a year.
        stale_now = int(datetime(2026, 5, 9, 10, 0, tzinfo=mexico).timestamp())
        stale_limited = update_item_after_output(
            QueueItem(cwd="/tmp/repo", session_id="abc"),
            "usage limit reached · resets Apr 23, 2pm (America/Mexico_City)",
            now=stale_now,
            retry_delay_seconds=18_000,
            followup_delay_seconds=900,
            max_attempts=3,
        )
        self.assertEqual(stale_limited.status, "pending")
        self.assertEqual(stale_limited.next_attempt_at, stale_now + 18_000)

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
        self.write_transcript("limited", synthetic_line("You've hit your session limit · resets 5:10pm (America/Mexico_City)"))
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
        self.write_transcript("old", synthetic_line("You've hit your session limit · resets 5:10pm (America/Mexico_City)"))
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
        self.write_transcript("not-limited", assistant_line("All set"))
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
        self.write_transcript("already", synthetic_line("You've hit your session limit · resets 5:10pm (America/Mexico_City)"))

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_autoqueue(self.args(now=1000, max_session_age_hours=0.0005))

        self.assertEqual(code, 0)
        self.assertIn("Auto-queued 1 session", output.getvalue())
        self.assertEqual([item.session_id for item in load_queue(self.queue_path)], ["already", "limited"])

    def test_autoqueue_ignores_limit_text_in_regular_messages(self):
        self.write_session(
            "meta.json",
            {
                "pid": 5,
                "status": "idle",
                "cwd": "/tmp/meta",
                "sessionId": "meta",
                "updatedAt": 999_000,
            },
        )
        self.write_transcript(
            "meta",
            assistant_line("The queue marks items limited when output says usage limit reached."),
        )

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_autoqueue(self.args(now=1000, max_session_age_hours=1))

        self.assertEqual(code, 0)
        self.assertIn("Auto-queued 0 sessions", output.getvalue())
        self.assertEqual(load_queue(self.queue_path), [])

    def test_autoqueue_skips_session_resumed_after_limit(self):
        self.write_session(
            "resumed.json",
            {
                "pid": 6,
                "status": "idle",
                "cwd": "/tmp/resumed",
                "sessionId": "resumed",
                "updatedAt": 999_000,
            },
        )
        self.write_transcript(
            "resumed",
            synthetic_line("You've hit your session limit · resets 5:10pm (America/Mexico_City)")
            + assistant_line("Resumed after the reset and finished the task."),
        )

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_autoqueue(self.args(now=1000, max_session_age_hours=1))

        self.assertEqual(code, 0)
        self.assertEqual(load_queue(self.queue_path), [])

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
        self.assertRegex(output.getvalue(), r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]")

    def test_watch_defers_item_owned_by_busy_session(self):
        now = 1_000_000
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/repo", session_id="abc"))
        self.write_session(
            "owner.json",
            {
                "pid": os.getpid(),
                "status": "busy",
                "cwd": "/tmp/repo",
                "sessionId": "abc",
                "updatedAt": (now - 7200) * 1000,
            },
        )

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_watch(self.args(dry_run=False, now=now, auto_queue=False))

        self.assertEqual(code, 0)
        self.assertIn("Deferred abc", output.getvalue())
        self.assertNotIn("DRY RUN", output.getvalue())
        item = load_queue(self.queue_path)[0]
        self.assertEqual(item.status, "pending")
        self.assertEqual(item.attempts, 0)
        self.assertEqual(item.next_attempt_at, now + 900)

    def test_watch_dry_run_reports_defer_without_mutating(self):
        now = 1_000_000
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/repo", session_id="abc"))
        self.write_session(
            "owner.json",
            {
                "pid": os.getpid(),
                "status": "busy",
                "cwd": "/tmp/repo",
                "sessionId": "abc",
                "updatedAt": (now - 10) * 1000,
            },
        )

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_watch(self.args(dry_run=True, now=now, auto_queue=False))

        self.assertEqual(code, 0)
        self.assertIn("Would defer abc", output.getvalue())
        self.assertEqual(load_queue(self.queue_path)[0].next_attempt_at, 0)

    def test_watch_defers_item_owned_by_recently_active_session(self):
        now = 1_000_000
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/repo", session_id="abc"))
        self.write_session(
            "owner.json",
            {
                "pid": os.getpid(),
                "status": "idle",
                "cwd": "/tmp/repo",
                "sessionId": "abc",
                "updatedAt": (now - 10) * 1000,
            },
        )

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_watch(self.args(dry_run=False, now=now, auto_queue=False))

        self.assertEqual(code, 0)
        self.assertIn("Deferred abc", output.getvalue())
        self.assertEqual(load_queue(self.queue_path)[0].next_attempt_at, now + 900)

    def test_watch_resumes_item_with_stale_busy_session_file(self):
        now = 1_000_000
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/repo", session_id="abc"))
        self.write_session(
            "owner.json",
            {
                "pid": os.getpid(),
                "status": "busy",
                "cwd": "/tmp/repo",
                "sessionId": "abc",
                "updatedAt": (now - 100_000) * 1000,
            },
        )

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_watch(self.args(dry_run=True, now=now, auto_queue=False))

        self.assertEqual(code, 0)
        self.assertIn("DRY RUN: claude --resume abc", output.getvalue())

    def test_watch_resumes_item_with_stale_idle_session(self):
        now = 1_000_000
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/repo", session_id="abc"))
        self.write_session(
            "owner.json",
            {
                "pid": os.getpid(),
                "status": "idle",
                "cwd": "/tmp/repo",
                "sessionId": "abc",
                "updatedAt": (now - 7200) * 1000,
            },
        )

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_watch(self.args(dry_run=True, now=now, auto_queue=False))

        self.assertEqual(code, 0)
        self.assertIn("DRY RUN: claude --resume abc", output.getvalue())

    def test_watch_resumes_item_whose_owner_process_died(self):
        now = 1_000_000
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/repo", session_id="abc"))
        self.write_session(
            "owner.json",
            {
                "pid": DEAD_PID,
                "status": "busy",
                "cwd": "/tmp/repo",
                "sessionId": "abc",
                "updatedAt": now * 1000,
            },
        )

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_watch(self.args(dry_run=True, now=now, auto_queue=False))

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
        self.write_transcript("limited", synthetic_line("You've hit your session limit · resets 5:10pm (America/Mexico_City)"))
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

    def test_status_prints_daemon_state(self):
        self.args().plist_path.write_text("<plist/>")
        loaded = types.SimpleNamespace(installed=True, loaded=True, detail="loaded")

        output = io.StringIO()
        with patch("tokenmaxx.cli.launchd_state", return_value=loaded), redirect_stdout(output):
            cli.cmd_status(self.args())

        rendered = output.getvalue()
        self.assertIn("Daemon: loaded", rendered)
        self.assertIn(str(self.args().plist_path), rendered)

    def test_build_launchd_plist_contains_watch_command(self):
        plist = launchd.build_launchd_plist(
            program="/usr/local/bin/tokenmaxx",
            claude_bin="/usr/local/bin/claude",
            queue_path=Path("/tmp/queue.jsonl"),
            log_path=Path("/tmp/tokenmaxx.log"),
            interval_seconds=300,
            sessions_dir=Path("/tmp/sessions"),
            projects_dir=Path("/tmp/projects"),
            lock_timeout_seconds=7,
        )
        self.assertIn("<string>/usr/local/bin/tokenmaxx</string>", plist)
        self.assertIn("<string>watch</string>", plist)
        self.assertIn("<string>--claude-bin</string>", plist)
        self.assertIn("<string>/usr/local/bin/claude</string>", plist)
        self.assertIn("<string>--queue</string>", plist)
        self.assertIn("<string>/tmp/queue.jsonl</string>", plist)
        self.assertIn("<string>--sessions-dir</string>", plist)
        self.assertIn("<string>/tmp/sessions</string>", plist)
        self.assertIn("<string>--projects-dir</string>", plist)
        self.assertIn("<string>/tmp/projects</string>", plist)
        self.assertIn("<string>--lock-timeout-seconds</string>", plist)
        self.assertIn("<string>7</string>", plist)

    def test_build_launchd_plist_embeds_path_environment(self):
        plist = launchd.build_launchd_plist(
            program="/usr/local/bin/tokenmaxx",
            claude_bin="/usr/local/bin/claude",
            queue_path=Path("/tmp/queue.jsonl"),
            log_path=Path("/tmp/tokenmaxx.log"),
            interval_seconds=300,
            path_env="/opt/custom/bin:/usr/bin:/bin",
        )
        self.assertIn("<key>EnvironmentVariables</key>", plist)
        self.assertIn("<key>PATH</key>", plist)
        self.assertIn("<string>/opt/custom/bin:/usr/bin:/bin</string>", plist)

    def test_drop_tombstones_queue_item(self):
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/repo", session_id="abc"))
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/repo", session_id="keep"))

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_drop(self.args(session_id="abc"))

        self.assertEqual(code, 0)
        self.assertIn("Dropped abc", output.getvalue())
        dropped, kept = load_queue(self.queue_path)
        self.assertEqual(dropped.status, "blocked")
        self.assertEqual(dropped.blocked_reason, "dropped by user")
        self.assertFalse(is_due(dropped))
        self.assertEqual(kept.status, "pending")

    def test_drop_reports_missing_session(self):
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/repo", session_id="keep"))

        error = io.StringIO()
        with redirect_stderr(error):
            code = cli.cmd_drop(self.args(session_id="missing"))

        self.assertEqual(code, 1)
        self.assertIn("No queued item", error.getvalue())
        self.assertEqual(load_queue(self.queue_path)[0].status, "pending")

    def test_dropped_session_is_not_requeued_or_resumed_by_watch(self):
        self.write_session(
            "limited.json",
            {
                "pid": DEAD_PID,
                "status": "idle",
                "cwd": "/tmp/limited",
                "sessionId": "limited",
                "updatedAt": 999_000,
            },
        )
        self.write_transcript("limited", synthetic_line("You've hit your session limit · resets 5:10pm (America/Mexico_City)"))

        with redirect_stdout(io.StringIO()):
            cli.cmd_autoqueue(self.args(now=1000))
            cli.cmd_drop(self.args(session_id="limited"))
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_watch(self.args(dry_run=True, now=1000))

        self.assertEqual(code, 0)
        self.assertNotIn("Auto-queued", output.getvalue())
        self.assertNotIn("DRY RUN", output.getvalue())
        items = load_queue(self.queue_path)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].status, "blocked")

    def test_watch_claims_resume_with_lease_and_merges_result(self):
        now = 1_000_000
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/repo", session_id="abc"))
        updated = QueueItem(cwd="/tmp/repo", session_id="abc", status="done", attempts=1, last_output="DONE")

        observed = {}

        def fake_run(item, **kwargs):
            observed["item"] = item
            observed["queue_at_resume"] = load_queue(self.queue_path)
            return updated

        output = io.StringIO()
        with patch("tokenmaxx.cli.run_due_item", side_effect=fake_run), redirect_stdout(output):
            code = cli.cmd_watch(self.args(dry_run=False, now=now, auto_queue=False))

        self.assertEqual(code, 0)
        self.assertTrue(is_due(observed["item"], now))
        lease_row = observed["queue_at_resume"][0]
        self.assertEqual(lease_row.status, "pending")
        self.assertGreater(lease_row.next_attempt_at, now + 7_200)
        final = load_queue(self.queue_path)[0]
        self.assertEqual(final.status, "done")
        self.assertEqual(final.attempts, 1)

    def test_merge_resumed_item_respects_midflight_resolution(self):
        pending = [QueueItem(cwd="/tmp/repo", session_id="abc", status="pending")]
        done = QueueItem(cwd="/tmp/repo", session_id="abc", status="done", attempts=1)
        merge_resumed_item(pending, done)
        self.assertEqual(pending[0].status, "done")

        tombstoned = [QueueItem(cwd="/tmp/repo", session_id="abc", status="blocked", blocked_reason="dropped by user")]
        merge_resumed_item(tombstoned, done)
        self.assertEqual(tombstoned[0].status, "blocked")
        self.assertEqual(tombstoned[0].blocked_reason, "dropped by user")

        # A session re-added after a drop: the result must land on the pending
        # row, not stop at the blocked tombstone.
        readded = [
            QueueItem(cwd="/tmp/repo", session_id="abc", status="blocked", blocked_reason="dropped by user"),
            QueueItem(cwd="/tmp/repo", session_id="abc", status="pending"),
        ]
        merge_resumed_item(readded, done)
        self.assertEqual(readded[0].status, "blocked")
        self.assertEqual(readded[1].status, "done")

        deleted: list[QueueItem] = []
        merge_resumed_item(deleted, done)
        self.assertEqual(deleted, [])

    def test_main_reports_locked_queue_without_traceback(self):
        error = io.StringIO()
        with patch("tokenmaxx.cli.queue_lock", side_effect=TimeoutError("locked")), redirect_stderr(error):
            code = cli.main(["drop", "--session-id", "x", "--queue", str(self.queue_path)])

        self.assertEqual(code, 1)
        self.assertIn("locked by another tokenmaxx process", error.getvalue())

    def test_launchd_install_dry_run_prints_plist_without_writing(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_launchd_install(self.args(claude_bin="/usr/local/bin/claude", dry_run=True))
        self.assertEqual(code, 0)
        self.assertIn("com.local.tokenmaxx", output.getvalue())
        self.assertFalse(self.root.joinpath("com.local.tokenmaxx.plist").exists())

    def test_launchd_install_requires_program_when_not_on_path(self):
        error = io.StringIO()
        with patch("tokenmaxx.cli.resolve_default_program", return_value=None), redirect_stderr(error):
            code = cli.cmd_launchd_install(self.args(program=None, dry_run=True))
        self.assertEqual(code, 1)
        self.assertIn("Pass --program", error.getvalue())

    def test_start_writes_plist_and_loads_service(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if command[:2] == ["launchctl", "print"]:
                return subprocess.CompletedProcess(command, 3, "", "not loaded")
            return subprocess.CompletedProcess(command, 0, "", "")

        output = io.StringIO()
        args = self.args(
            program=None,
            claude_bin=None,
            queue=self.root / "state" / "queue.jsonl",
            log_path=self.root / "logs" / "tokenmaxx.log",
            plist_path=self.root / "agents" / "com.local.tokenmaxx.plist",
        )
        with patch("tokenmaxx.cli.resolve_default_program", return_value="/usr/local/bin/tokenmaxx"), patch(
            "tokenmaxx.cli.resolve_default_claude_bin", return_value="/usr/local/bin/claude"
        ), patch(
            "tokenmaxx.launchd.subprocess.run", side_effect=fake_run
        ), redirect_stdout(output):
            code = cli.cmd_start(args)

        self.assertEqual(code, 0)
        self.assertTrue(args.queue.parent.exists())
        self.assertTrue(args.log_path.parent.exists())
        self.assertTrue(args.plist_path.exists())
        self.assertIn("<string>--claude-bin</string>", args.plist_path.read_text())
        self.assertIn("<string>/usr/local/bin/claude</string>", args.plist_path.read_text())
        self.assertIn("<key>EnvironmentVariables</key>", args.plist_path.read_text())
        self.assertIn("<key>PATH</key>", args.plist_path.read_text())
        self.assertIn("Started com.local.tokenmaxx", output.getvalue())
        self.assertIn(["launchctl", "load", str(args.plist_path)], calls)

    def test_start_warns_when_loaded_service_keeps_old_arguments(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        output = io.StringIO()
        args = self.args(
            program=None,
            claude_bin=None,
            plist_path=self.root / "agents" / "com.local.tokenmaxx.plist",
        )
        args.plist_path.parent.mkdir(parents=True)
        args.plist_path.write_text("<plist>old</plist>")

        with patch("tokenmaxx.cli.resolve_default_program", return_value="/usr/local/bin/tokenmaxx"), patch(
            "tokenmaxx.cli.resolve_default_claude_bin", return_value="/usr/local/bin/claude"
        ), patch(
            "tokenmaxx.launchd.subprocess.run", side_effect=fake_run
        ), redirect_stdout(output):
            code = cli.cmd_start(args)

        self.assertEqual(code, 0)
        self.assertIn("already loaded", output.getvalue())
        self.assertIn("Run `tokenmaxx stop` then `tokenmaxx start`", output.getvalue())
        self.assertIn("<string>--claude-bin</string>", args.plist_path.read_text())
        self.assertNotIn(["launchctl", "load", str(args.plist_path)], calls)

    def test_stop_unloads_loaded_service(self):
        self.args().plist_path.write_text("<plist/>")
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        output = io.StringIO()
        with patch("tokenmaxx.launchd.subprocess.run", side_effect=fake_run), redirect_stdout(output):
            code = cli.cmd_stop(self.args())

        self.assertEqual(code, 0)
        self.assertIn("Stopped com.local.tokenmaxx", output.getvalue())
        self.assertIn(["launchctl", "unload", str(self.args().plist_path)], calls)

    def test_logs_prints_recent_log_lines(self):
        self.args().log_path.write_text("one\ntwo\nthree\n")

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_logs(self.args(lines=2))

        self.assertEqual(code, 0)
        self.assertNotIn("one", output.getvalue())
        self.assertIn("two\nthree\n", output.getvalue())


if __name__ == "__main__":
    unittest.main()
