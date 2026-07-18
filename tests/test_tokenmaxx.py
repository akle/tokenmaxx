import io
import json
import os
import plistlib
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

from tokenmaxx import claude, cli, launchd, queue as queue_module, runner
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


def synthetic_line(text, timestamp=None):
    record = {
        "type": "assistant",
        "message": {"model": "<synthetic>", "role": "assistant", "content": [{"type": "text", "text": text}]},
    }
    if timestamp is not None:
        record["timestamp"] = timestamp
    return json.dumps(record) + "\n"


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
        self.codex_sessions_dir = self.root / "codex-sessions"
        self.codex_history_file = self.root / "codex-history.jsonl"
        self.codex_logs_db = self.root / "codex-logs.sqlite"
        self.projects_dir = self.root / "projects"
        self.sessions_dir.mkdir()
        self.codex_sessions_dir.mkdir()
        self.projects_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, **kwargs):
        defaults = {
            "queue": self.queue_path,
            "sessions_dir": self.sessions_dir,
            "codex_sessions_dir": self.codex_sessions_dir,
            "codex_history_file": self.codex_history_file,
            "codex_logs_db": self.codex_logs_db,
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
            "codex_bin": "codex",
            "provider": None,
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

    def write_codex_session(self, session_id, cwd="/tmp/codex", event=None, timestamp="1970-01-01T00:16:39Z"):
        path = self.codex_sessions_dir / f"{session_id}.jsonl"
        records = [
            {"type": "session_meta", "payload": {"id": session_id, "cwd": cwd}},
        ]
        if event is not None:
            records.append(
                {
                    "type": "event_msg",
                    "timestamp": timestamp,
                    "payload": event,
                }
            )
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        os.utime(path, (999, 999))
        return path

    def test_queue_round_trip_and_status_helpers(self):
        item = QueueItem(
            cwd="/tmp/repo",
            session_id="abc",
            next_attempt_at=200,
            lease_id="lease-1",
            lease_pid=4321,
            lease_lock_path="/tmp/lease-1.lock",
        )
        append_queue_item(self.queue_path, item)

        loaded = load_queue(self.queue_path)
        self.assertEqual(loaded[0].cwd, "/tmp/repo")
        self.assertEqual(loaded[0].session_id, "abc")
        self.assertEqual(loaded[0].lease_id, "lease-1")
        self.assertEqual(loaded[0].lease_pid, 4321)
        self.assertEqual(loaded[0].lease_lock_path, "/tmp/lease-1.lock")
        self.assertFalse(is_due(loaded[0], now=100))
        self.assertTrue(is_due(loaded[0], now=200))
        self.assertEqual(queue_lock_path(self.queue_path), self.root / "queue.jsonl.lock")

    def test_queue_normalizes_invalid_lease_pid(self):
        self.assertEqual(QueueItem(cwd="/tmp/repo", session_id="abc", lease_pid=-1).lease_pid, 0)

    def test_queue_provider_migration_and_composite_identity(self):
        self.queue_path.write_text('{"cwd":"/tmp/r","sessionId":"same"}\n')
        legacy = load_queue(self.queue_path)[0]
        self.assertEqual(legacy.provider, "claude")
        self.assertEqual(legacy.key, ("claude", "same"))

        rows = [
            QueueItem(cwd="/tmp/r", session_id="same", provider="codex"),
            QueueItem(cwd="/tmp/r", session_id="same", provider="claude"),
        ]
        self.assertEqual(rows[0].to_dict()["provider"], "codex")
        merge_resumed_item(
            rows,
            QueueItem(cwd="/tmp/r", session_id="same", provider="claude", status="done"),
        )
        self.assertEqual([row.status for row in rows], ["pending", "done"])

        self.queue_path.write_text('{"cwd":"/tmp/r","sessionId":"x","provider":"other"}\n')
        with self.assertRaisesRegex(ValueError, "unsupported provider"):
            load_queue(self.queue_path)

    def test_classify_output_and_retry_updates(self):
        self.assertEqual(classify_output("usage limit reached"), "limited")
        self.assertEqual(classify_output("ran out of credits"), "limited")
        self.assertEqual(classify_output("Server is temporarily limiting requests"), "limited")
        self.assertEqual(classify_output("You've hit your limit · resets 7pm (America/Mexico_City)"), "limited")
        self.assertEqual(classify_output("You're out of usage credits. Run /usage-credits to keep using Fable 5 or /model to switch."), "limited")
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

        codex_limited = update_item_after_output(
            QueueItem(cwd="/tmp/repo", session_id="codex-1", provider="codex"),
            "You've hit your usage limit. Try again at 12:52 AM.",
            now=now,
            retry_delay_seconds=18_000,
            followup_delay_seconds=900,
            max_attempts=3,
        )
        self.assertEqual(codex_limited.status, "pending")
        self.assertEqual(codex_limited.next_attempt_at, int(datetime(2026, 6, 24, 0, 53, tzinfo=mexico).timestamp()))

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

    def test_load_claude_sessions_skips_file_deleted_mid_scan(self):
        # Claude Code deletes session files on exit; a dangling symlink
        # reproduces "globbed, then gone at read time".
        (self.sessions_dir / "ghost.json").symlink_to(self.sessions_dir / "does-not-exist.json")
        self.write_session("80544.json", {"pid": 80544, "status": "idle", "cwd": "/tmp/repo", "sessionId": "abc"})

        sessions = claude.load_claude_sessions(self.sessions_dir)
        self.assertEqual([s["sessionId"] for s in sessions], ["abc"])

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

    def test_load_claude_sessions_skips_malformed_metadata(self):
        (self.sessions_dir / "invalid-utf8.json").write_bytes(b"\xff\xfe\n")
        (self.sessions_dir / "list.json").write_text("[]\n")
        self.write_session(
            "bad-updated.json",
            {"pid": DEAD_PID, "status": "idle", "cwd": "/tmp/bad", "sessionId": "bad", "updatedAt": "nope"},
        )
        self.write_session(
            "good.json",
            {"pid": DEAD_PID, "status": "idle", "cwd": "/tmp/good", "sessionId": "good", "updatedAt": 999_000},
        )

        sessions = claude.load_claude_sessions(self.sessions_dir)

        self.assertEqual([session["sessionId"] for session in sessions], ["good"])

    def test_message_text_skips_non_string_content_parts(self):
        self.assertEqual(
            claude.message_text({"content": [{"text": "limit"}, {"text": 123}, {"text": None}]}),
            "limit",
        )

    def test_claude_connection_refused_requires_synthetic_provider_record(self):
        provider_error = json.loads(
            synthetic_line("API Error: Unable to connect to API (ConnectionRefused)")
        )
        regular_message = json.loads(assistant_line("API Error: Unable to connect to API (ConnectionRefused)"))

        self.assertTrue(claude.is_connection_refused_error(provider_error))
        self.assertFalse(claude.is_connection_refused_error(regular_message))

    def test_autoqueue_adds_claude_connection_refused_error(self):
        self.write_session(
            "connection-refused.json",
            {
                "pid": DEAD_PID,
                "status": "idle",
                "cwd": "/tmp/connection-refused",
                "sessionId": "connection-refused",
                "updatedAt": 999_000,
            },
        )
        self.write_transcript(
            "connection-refused",
            synthetic_line(
                "API Error: Unable to connect to API (ConnectionRefused)",
                timestamp="1970-01-01T00:16:39Z",
            ),
        )

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_autoqueue(self.args(now=1000, max_session_age_hours=1))

        self.assertEqual(code, 0)
        self.assertIn("Auto-queued 1 session", output.getvalue())
        self.assertEqual(load_queue(self.queue_path)[0].key, ("claude", "connection-refused"))

    def test_find_transcript_skips_file_deleted_mid_scan(self):
        project_dir = self.projects_dir / "project"
        project_dir.mkdir()
        (project_dir / "abc.jsonl").symlink_to(project_dir / "missing.jsonl")

        self.assertIsNone(claude.find_transcript(self.projects_dir, "abc"))

    def test_add_writes_selected_session_to_queue(self):
        self.write_session("80544.json", {"pid": 80544, "status": "busy", "cwd": "/tmp/repo", "sessionId": "abc"})
        with redirect_stdout(io.StringIO()):
            code = cli.cmd_add(self.args(pid=80544))
        self.assertEqual(code, 0)
        self.assertEqual(load_queue(self.queue_path)[0].session_id, "abc")

    def test_add_updates_existing_pending_item_cwd(self):
        self.write_session("80544.json", {"pid": 80544, "status": "busy", "cwd": "/tmp/session", "sessionId": "abc"})
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/old", session_id="abc"))

        with redirect_stdout(io.StringIO()):
            code = cli.cmd_add(self.args(pid=80544, cwd="/tmp/override"))

        self.assertEqual(code, 0)
        self.assertEqual(load_queue(self.queue_path)[0].cwd, "/tmp/override")

    def test_add_writes_selected_codex_session_to_queue(self):
        self.write_codex_session("codex-abc")
        with redirect_stdout(io.StringIO()):
            code = cli.cmd_add(self.args(session_id="codex-abc", provider="codex"))
        self.assertEqual(code, 0)
        item = load_queue(self.queue_path)[0]
        self.assertEqual(item.key, ("codex", "codex-abc"))

    def test_add_deduplicates_pending_composite_identity(self):
        self.write_session(
            "80544.json",
            {"pid": 80544, "status": "busy", "cwd": "/tmp/claude", "sessionId": "same"},
        )
        self.write_codex_session("same")

        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_add(self.args(pid=80544, provider="claude")), 0)
            self.assertEqual(cli.cmd_add(self.args(session_id="same", provider="codex")), 0)
            self.assertEqual(cli.cmd_add(self.args(session_id="same", provider="codex")), 0)

        self.assertEqual(
            [item.key for item in load_queue(self.queue_path)],
            [("claude", "same"), ("codex", "same")],
        )

    def test_add_rearms_resolved_item_in_place(self):
        self.write_codex_session("codex-abc", cwd="/tmp/new-cwd")
        append_queue_item(
            self.queue_path,
            QueueItem(
                cwd="/tmp/old-cwd",
                session_id="codex-abc",
                provider="codex",
                status="blocked",
                attempts=3,
                next_attempt_at=1234,
                last_output="old output",
                blocked_reason="dropped by user",
                lease_id="old-lease",
            ),
        )

        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_add(self.args(session_id="codex-abc", provider="codex")), 0)

        items = load_queue(self.queue_path)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].status, "pending")
        self.assertEqual(items[0].cwd, "/tmp/new-cwd")
        self.assertEqual(items[0].attempts, 0)
        self.assertEqual(items[0].next_attempt_at, 0)
        self.assertEqual(items[0].last_output, "")
        self.assertEqual(items[0].blocked_reason, "")
        self.assertEqual(items[0].lease_id, "")

    def test_scan_lists_both_providers(self):
        self.write_session(
            "claude.json",
            {"pid": 1, "status": "idle", "cwd": "/tmp/claude", "sessionId": "claude-abc"},
        )
        self.write_codex_session("codex-abc")
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_scan(self.args())
        self.assertEqual(code, 0)
        self.assertRegex(output.getvalue(), r"PROVIDER\s+SESSION")
        self.assertIn("claude-abc", output.getvalue())
        self.assertIn("codex-abc", output.getvalue())

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

    def test_autoqueue_passes_codex_logs_database_to_provider(self):
        with patch("tokenmaxx.cli.codex.build_limited_queue_items", return_value=[]) as build:
            cli.autoqueue_limited_sessions(
                self.args(),
                [],
                1000,
                [],
                provider="codex",
            )

        self.assertEqual(build.call_args.kwargs["logs_path"], self.codex_logs_db)

    def test_active_codex_check_passes_external_stop_sources(self):
        self.write_codex_session("codex-1")
        item = QueueItem(cwd="/tmp/codex", session_id="codex-1", provider="codex")

        with patch("tokenmaxx.cli.codex.find_active_session", return_value=None) as find:
            cli.find_active_provider_session(self.args(), item, 1000)

        self.assertEqual(find.call_args.kwargs["history_path"], self.codex_history_file)
        self.assertEqual(find.call_args.kwargs["logs_path"], self.codex_logs_db)
        self.assertEqual(find.call_args.kwargs["max_session_age_hours"], 24)

    def test_autoqueue_adds_limited_sessions_from_both_providers(self):
        self.write_session(
            "claude.json",
            {"pid": 1, "status": "idle", "cwd": "/tmp/claude", "sessionId": "same", "updatedAt": 999_000},
        )
        self.write_transcript("same", synthetic_line("usage limit reached", timestamp="1970-01-01T00:16:39Z"))
        self.write_codex_session(
            "same",
            event={"type": "error", "codex_error_info": "usage_limit_exceeded", "message": "limited"},
        )

        with redirect_stdout(io.StringIO()):
            code = cli.cmd_autoqueue(self.args(now=1000, max_session_age_hours=1))

        self.assertEqual(code, 0)
        self.assertEqual({item.key for item in load_queue(self.queue_path)}, {("claude", "same"), ("codex", "same")})

    def test_autoqueue_adds_codex_remote_compaction_failures(self):
        self.write_codex_session("compact")
        self.codex_history_file.write_text(
            json.dumps(
                {
                    "session_id": "compact",
                    "ts": 980,
                    "text": (
                        "\u25a0 Error running remote compact task: stream disconnected before completion: "
                        "error sending request for url\n"
                        "(https://chatgpt.com/backend-api/codex/responses)"
                    ),
                }
            )
            + "\n"
        )

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_autoqueue(self.args(now=1000, max_session_age_hours=1))

        self.assertEqual(code, 0)
        self.assertIn("Auto-queued 1 session", output.getvalue())
        self.assertEqual(load_queue(self.queue_path)[0].key, ("codex", "compact"))

    def rearm_fixture(self, existing_status, existing_updated_at, banner_timestamp, blocked_reason=""):
        append_queue_item(
            self.queue_path,
            QueueItem(
                cwd="/tmp/repo",
                session_id="abc",
                status=existing_status,
                attempts=5,
                blocked_reason=blocked_reason,
                updated_at=existing_updated_at,
            ),
        )
        self.write_session(
            "abc.json",
            {"pid": 1, "status": "idle", "cwd": "/tmp/repo", "sessionId": "abc", "updatedAt": 999_000},
        )
        self.write_transcript(
            "abc",
            synthetic_line("You're out of usage credits. Run /usage-credits to keep going.", timestamp=banner_timestamp),
        )

    def test_autoqueue_rearms_blocked_item_on_newer_limit_banner(self):
        self.rearm_fixture("blocked", 500, "1970-01-01T00:13:20Z", blocked_reason="max attempts (5) reached")

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_autoqueue(self.args(now=1000, max_session_age_hours=1))

        self.assertEqual(code, 0)
        self.assertIn("Auto-queued 1 session", output.getvalue())
        items = load_queue(self.queue_path)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].status, "pending")
        self.assertEqual(items[0].attempts, 0)
        self.assertEqual(items[0].blocked_reason, "")

    def test_autoqueue_never_rearms_user_dropped_item(self):
        self.rearm_fixture("blocked", 500, "1970-01-01T00:13:20Z", blocked_reason="dropped by user")

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_autoqueue(self.args(now=1000, max_session_age_hours=1))

        self.assertEqual(code, 0)
        self.assertIn("Auto-queued 0 sessions", output.getvalue())
        items = load_queue(self.queue_path)
        self.assertEqual(items[0].status, "blocked")
        self.assertEqual(items[0].blocked_reason, "dropped by user")

    def test_autoqueue_ignores_banner_older_than_queue_row(self):
        self.rearm_fixture("blocked", 900, "1970-01-01T00:10:00Z", blocked_reason="max attempts (5) reached")

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_autoqueue(self.args(now=1000, max_session_age_hours=1))

        self.assertEqual(code, 0)
        self.assertIn("Auto-queued 0 sessions", output.getvalue())
        self.assertEqual(load_queue(self.queue_path)[0].status, "blocked")

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

        with patch("tokenmaxx.runner.subprocess.Popen", return_value=process) as popen, patch.object(
            runner, "os", fake_os, create=True
        ), patch.object(runner, "signal", fake_signal, create=True):
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

    def test_run_due_item_handles_missing_working_directory(self):
        item = QueueItem(cwd="/tmp/missing", session_id="abc")

        with patch(
            "tokenmaxx.runner.subprocess.Popen",
            side_effect=FileNotFoundError(2, "No such file or directory", "/tmp/missing"),
        ):
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

        self.assertEqual(result.status, "pending")
        self.assertEqual(result.attempts, 1)
        self.assertIn("failed to start", result.last_output)

    def test_runner_replaces_invalid_provider_output(self):
        process = Mock()
        process.returncode = 1
        process.communicate.return_value = ("\ufffd", "")

        with patch("tokenmaxx.runner.subprocess.Popen", return_value=process) as popen:
            returncode, output = runner.run_resume_command(
                ["codex", "exec", "resume", "abc"],
                cwd="/tmp/repo",
                timeout_seconds=5,
                provider_name="codex",
            )

        self.assertEqual(returncode, 1)
        self.assertEqual(output, "\ufffd")
        self.assertEqual(popen.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(popen.call_args.kwargs["errors"], "replace")

    def test_runner_reports_provider_pid_after_spawn(self):
        process = Mock()
        process.pid = 4321
        process.returncode = 0
        process.communicate.return_value = ("", "")
        on_process_start = Mock()

        with patch("tokenmaxx.runner.subprocess.Popen", return_value=process):
            runner.run_resume_command(
                ["codex", "exec", "resume", "abc"],
                cwd="/tmp/repo",
                timeout_seconds=5,
                provider_name="codex",
                on_process_start=on_process_start,
            )

        on_process_start.assert_called_once_with(4321)

    def test_runner_inherits_lease_lock_during_provider_run(self):
        process = Mock()
        process.pid = 4321
        process.returncode = 0
        process.communicate.return_value = ("", "")
        lease_path = self.root / "lease.lock"

        with patch("tokenmaxx.runner.subprocess.Popen", return_value=process) as popen:
            runner.run_resume_command(
                ["codex", "exec", "resume", "abc"],
                cwd="/tmp/repo",
                timeout_seconds=5,
                provider_name="codex",
                lease_lock_path=lease_path,
            )

        self.assertIn("pass_fds", popen.call_args.kwargs)
        self.assertTrue(popen.call_args.kwargs["pass_fds"])

    def test_runner_cleans_up_provider_after_communication_failure(self):
        process = Mock()
        process.communicate.side_effect = RuntimeError("pipe failed")

        with patch("tokenmaxx.runner.subprocess.Popen", return_value=process), patch(
            "tokenmaxx.runner.terminate_process_group"
        ) as terminate:
            with self.assertRaisesRegex(RuntimeError, "pipe failed"):
                runner.run_resume_command(
                    ["codex", "exec", "resume", "abc"],
                    cwd="/tmp/repo",
                    timeout_seconds=5,
                    provider_name="codex",
                )

        terminate.assert_called_once_with(process)

    def test_watch_loop_logs_startup_line(self):
        # once=False with an immediate-return side effect via sleep patch
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/repo", session_id="abc", status="done"))
        output = io.StringIO()
        with patch("tokenmaxx.cli.time.sleep", side_effect=KeyboardInterrupt), redirect_stdout(output):
            with self.assertRaises(KeyboardInterrupt):
                cli.cmd_watch(self.args(dry_run=True, once=False))
        self.assertIn("tokenmaxx", output.getvalue())
        self.assertIn("watching", output.getvalue())

    def test_watch_dry_run_prints_generated_command(self):
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/repo", session_id="abc", lease_id="running"))
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_watch(self.args(dry_run=True))
        self.assertEqual(code, 0)
        self.assertIn("claude --resume abc", output.getvalue())
        self.assertRegex(output.getvalue(), r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]")

    def test_watch_dry_run_dispatches_due_codex_item(self):
        append_queue_item(
            self.queue_path,
            QueueItem(cwd="/tmp/repo", session_id="codex-session", provider="codex"),
        )
        output = io.StringIO()
        with patch("tokenmaxx.cli.shutil.which", side_effect=lambda name: name), redirect_stdout(output):
            code = cli.cmd_watch(self.args(dry_run=True, auto_queue=False))
        self.assertEqual(code, 0)
        self.assertIn("codex exec resume --all codex-session", output.getvalue())

    def test_watch_defers_due_item_when_provider_executable_is_missing(self):
        append_queue_item(
            self.queue_path,
            QueueItem(cwd="/tmp/repo", session_id="codex-session", provider="codex"),
        )
        output = io.StringIO()
        with patch("tokenmaxx.cli.shutil.which", side_effect=lambda name: name if name == "claude" else None), redirect_stdout(output):
            code = cli.cmd_watch(self.args(dry_run=False, auto_queue=False))
        self.assertEqual(code, 0)
        self.assertIn("Deferred codex:codex-se: codex executable unavailable", output.getvalue())
        self.assertEqual(load_queue(self.queue_path)[0].next_attempt_at, 1900)

    def test_watch_processes_only_one_due_item_across_providers(self):
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/c", session_id="claude-session"))
        append_queue_item(
            self.queue_path,
            QueueItem(cwd="/tmp/x", session_id="codex-session", provider="codex"),
        )
        output = io.StringIO()
        with patch("tokenmaxx.cli.shutil.which", side_effect=lambda name: name), redirect_stdout(output):
            code = cli.cmd_watch(self.args(dry_run=True, auto_queue=False))
        self.assertEqual(code, 0)
        self.assertIn("claude --resume claude-session", output.getvalue())
        self.assertNotIn("codex exec resume", output.getvalue())

    def test_watch_fails_immediately_when_no_provider_executable_resolves(self):
        for once in (True, False):
            with self.subTest(once=once):
                args = self.args(once=once, auto_queue=False)
                output = io.StringIO()
                error = io.StringIO()

                def stop_foreground_loop(_seconds):
                    args.once = True

                with patch("tokenmaxx.cli.shutil.which", return_value=None), patch(
                    "tokenmaxx.cli.time.sleep", side_effect=stop_foreground_loop
                ) as sleep, redirect_stdout(output), redirect_stderr(error):
                    code = cli.cmd_watch(args)

                self.assertNotEqual(code, 0)
                self.assertIn("Claude or Codex executable", error.getvalue())
                self.assertNotIn("No due items", output.getvalue())
                sleep.assert_not_called()

    def test_resume_lock_is_nonblocking_and_released(self):
        lock = getattr(queue_module, "resume_lock", None)
        self.assertIsNotNone(lock, "queue.resume_lock must exist")

        with lock(self.queue_path) as acquired:
            self.assertTrue(acquired)
            with lock(self.queue_path) as contended:
                self.assertFalse(contended)

        with lock(self.queue_path) as acquired_after_release:
            self.assertTrue(acquired_after_release)

    def test_watch_skips_due_provider_item_while_resume_lock_is_held(self):
        item = QueueItem(cwd="/tmp/codex", session_id="codex-session", provider="codex")
        append_queue_item(self.queue_path, item)
        output = io.StringIO()

        with queue_module.resume_lock(self.queue_path) as acquired:
            self.assertTrue(acquired)
            with patch("tokenmaxx.cli.shutil.which", side_effect=lambda name: name), patch(
                "tokenmaxx.cli.codex.run_due_item"
            ) as run_due_item, redirect_stdout(output):
                code = cli.cmd_watch(self.args(dry_run=False, auto_queue=False))

        self.assertEqual(code, 0)
        run_due_item.assert_not_called()
        self.assertTrue(is_due(load_queue(self.queue_path)[0], now=1000))
        self.assertIn("resume already in progress", output.getvalue().lower())

    def test_resolve_provider_bin_rejects_missing_absolute_path(self):
        missing = self.root / "missing-codex"
        self.assertIsNone(cli.resolve_provider_bin("codex", str(missing)))

    def test_parser_accepts_codex_provider_paths_and_selectors(self):
        parser = cli.build_parser()
        add_args = parser.parse_args(
            [
                "add",
                "--provider",
                "codex",
                "--session-id",
                "abc",
                "--codex-sessions-dir",
                str(self.codex_sessions_dir),
            ]
        )
        watch_args = parser.parse_args(
            [
                "watch",
                "--codex-bin",
                "/usr/local/bin/codex",
                "--codex-logs-db",
                str(self.codex_logs_db),
            ]
        )
        drop_args = parser.parse_args(["drop", "--provider", "codex", "--session-id", "abc"])
        self.assertEqual(add_args.provider, "codex")
        self.assertEqual(add_args.codex_sessions_dir, self.codex_sessions_dir)
        self.assertEqual(watch_args.codex_bin, "/usr/local/bin/codex")
        self.assertEqual(watch_args.codex_logs_db, self.codex_logs_db)
        self.assertEqual(drop_args.provider, "codex")

    def test_package_version_is_patch_release(self):
        self.assertEqual(cli.__version__, "0.5.6")

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
        self.assertIn("Deferred claude:abc", output.getvalue())
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
        self.assertIn("Would defer claude:abc", output.getvalue())
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
        self.assertIn("Deferred claude:abc", output.getvalue())
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
        self.assertIn("claude --resume abc", output.getvalue())

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
        self.assertIn("claude --resume abc", output.getvalue())

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
        self.assertIn("claude --resume abc", output.getvalue())

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
        self.assertIn("claude --resume limited", output.getvalue())

    def test_status_prints_columnar_queue_table(self):
        append_queue_item(
            self.queue_path,
            QueueItem(
                cwd=str(Path.home() / "dev" / "repo"),
                session_id="abcdef12-3456-7890-abcd-ef1234567890",
                status="pending",
                next_attempt_at=9_999_999_999,
                attempts=1,
                last_output="usage limit reached",
            ),
        )
        append_queue_item(
            self.queue_path,
            QueueItem(
                cwd="/tmp/other",
                session_id="99999999-aaaa-bbbb-cccc-dddddddddddd",
                status="blocked",
                attempts=5,
                blocked_reason="max attempts (5) reached after unknown output",
            ),
        )

        output = io.StringIO()
        with redirect_stdout(output):
            cli.cmd_status(self.args())
        rendered = output.getvalue()

        self.assertIn("1 pending", rendered)
        self.assertIn("1 blocked", rendered)
        self.assertIn("2 total", rendered)
        header, pending_row, blocked_row = [
            line for line in rendered.splitlines() if line.startswith(("STATUS", "pending", "blocked"))
        ]
        self.assertRegex(header, r"STATUS\s+ATT\s+NEXT\s+PROVIDER\s+SESSION\s+DIRECTORY\s+LAST")
        self.assertIn("abcdef12", pending_row)
        self.assertNotIn("abcdef12-3456", pending_row)
        self.assertIn("~/dev/repo", pending_row)
        self.assertIn("usage limit reached", pending_row)
        self.assertIn("max attempts (5)", blocked_row)
        # columns align: SESSION starts at the same offset in every row
        offset = header.index("SESSION")
        self.assertEqual(pending_row[offset : offset + 8], "abcdef12")
        self.assertEqual(blocked_row[offset : offset + 8], "99999999")

    def test_status_qualifies_mixed_provider_rows(self):
        append_queue_item(
            self.queue_path,
            QueueItem(cwd="/tmp/c", session_id="same", provider="claude"),
        )
        append_queue_item(
            self.queue_path,
            QueueItem(cwd="/tmp/x", session_id="same", provider="codex"),
        )
        output = io.StringIO()
        with redirect_stdout(output):
            cli.cmd_status(self.args())
        self.assertRegex(output.getvalue(), r"PROVIDER\s+SESSION")
        self.assertIn("claude", output.getvalue())
        self.assertIn("codex", output.getvalue())

    def test_status_cli_rejects_unknown_queue_provider_without_traceback(self):
        self.queue_path.write_text(
            json.dumps({"cwd": "/tmp/repo", "sessionId": "abc", "provider": "unknown"}) + "\n"
        )
        output = io.StringIO()
        errors = io.StringIO()

        try:
            with patch("tokenmaxx.cli.launchd_state"), redirect_stdout(output), redirect_stderr(errors):
                code = cli.main(["status", "--queue", str(self.queue_path)])
        except ValueError as exc:
            self.fail(f"CLI must reject malformed persisted state without raising: {exc}")

        self.assertEqual(code, 1)
        self.assertIn("invalid queue line 1", errors.getvalue())
        self.assertIn("unsupported provider: unknown", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_status_orders_pending_before_blocked_and_done(self):
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/r", session_id="done-item", status="done"))
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/r", session_id="blocked-item", status="blocked"))
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/r", session_id="pending-item", status="pending"))

        output = io.StringIO()
        with redirect_stdout(output):
            cli.cmd_status(self.args())
        lines = [line.split()[0] for line in output.getvalue().splitlines() if line.startswith(("pending", "blocked", "done"))]
        self.assertEqual(lines, ["pending", "blocked", "done"])

    def test_status_marks_due_items(self):
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/r", session_id="due-item", status="pending", next_attempt_at=1))

        output = io.StringIO()
        with redirect_stdout(output):
            cli.cmd_status(self.args())
        self.assertIn("due now", output.getvalue())

    def test_drop_accepts_unique_session_prefix(self):
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/repo", session_id="abcdef12-3456-7890-abcd-ef1234567890"))
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/repo", session_id="99999999-aaaa-bbbb-cccc-dddddddddddd"))

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_drop(self.args(session_id="abcdef12"))

        self.assertEqual(code, 0)
        self.assertIn("Dropped abcdef12-3456-7890-abcd-ef1234567890", output.getvalue())
        by_id = {item.session_id[:8]: item for item in load_queue(self.queue_path)}
        self.assertEqual(by_id["abcdef12"].status, "blocked")
        self.assertEqual(by_id["99999999"].status, "pending")

    def test_drop_provider_filter_targets_one_matching_id(self):
        append_queue_item(
            self.queue_path,
            QueueItem(cwd="/tmp/c", session_id="same", provider="claude"),
        )
        append_queue_item(
            self.queue_path,
            QueueItem(cwd="/tmp/x", session_id="same", provider="codex"),
        )
        with redirect_stdout(io.StringIO()):
            code = cli.cmd_drop(self.args(session_id="same", provider="codex"))
        self.assertEqual(code, 0)
        rows = {item.provider: item for item in load_queue(self.queue_path)}
        self.assertEqual(rows["claude"].status, "pending")
        self.assertEqual(rows["codex"].status, "blocked")

    def test_drop_rejects_ambiguous_prefix(self):
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/repo", session_id="abc-one"))
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/repo", session_id="abc-two"))

        error = io.StringIO()
        with redirect_stderr(error):
            code = cli.cmd_drop(self.args(session_id="abc"))

        self.assertEqual(code, 1)
        self.assertIn("Ambiguous", error.getvalue())
        self.assertEqual({item.status for item in load_queue(self.queue_path)}, {"pending"})

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
            codex_bin="/opt/homebrew/bin/codex",
            queue_path=Path("/tmp/queue.jsonl"),
            log_path=Path("/tmp/tokenmaxx.log"),
            interval_seconds=300,
            sessions_dir=Path("/tmp/sessions"),
            codex_sessions_dir=Path("/tmp/codex-sessions"),
            codex_history_file=Path("/tmp/codex-history.jsonl"),
            codex_logs_db=Path("/tmp/codex-logs.sqlite"),
            projects_dir=Path("/tmp/projects"),
            lock_timeout_seconds=7,
        )
        self.assertIn("<string>/usr/local/bin/tokenmaxx</string>", plist)
        self.assertIn("<string>watch</string>", plist)
        self.assertIn("<string>--claude-bin</string>", plist)
        self.assertIn("<string>/usr/local/bin/claude</string>", plist)
        self.assertIn("<string>--codex-bin</string>", plist)
        self.assertIn("<string>/opt/homebrew/bin/codex</string>", plist)
        self.assertIn("<string>--queue</string>", plist)
        self.assertIn("<string>/tmp/queue.jsonl</string>", plist)
        self.assertIn("<string>--sessions-dir</string>", plist)
        self.assertIn("<string>/tmp/sessions</string>", plist)
        self.assertIn("<string>--codex-sessions-dir</string>", plist)
        self.assertIn("<string>/tmp/codex-sessions</string>", plist)
        self.assertIn("<string>--codex-history-file</string>", plist)
        self.assertIn("<string>/tmp/codex-history.jsonl</string>", plist)
        self.assertIn("<string>--codex-logs-db</string>", plist)
        self.assertIn(
            f"<string>{Path('/tmp/codex-logs.sqlite').resolve()}</string>", plist
        )
        self.assertIn("<string>--projects-dir</string>", plist)
        self.assertIn("<string>/tmp/projects</string>", plist)
        self.assertIn("<string>--lock-timeout-seconds</string>", plist)
        self.assertIn("<string>7</string>", plist)
        self.assertTrue(plistlib.loads(plist.encode())["RunAtLoad"])

    def test_build_launchd_plist_resolves_relative_codex_logs_database(self):
        original_cwd = Path.cwd()
        relative_logs_db = Path("state/codex-logs.sqlite")
        try:
            os.chdir(self.root)
            expected_logs_db = relative_logs_db.resolve()
            plist = launchd.build_launchd_plist(
                program="/usr/local/bin/tokenmaxx",
                claude_bin=None,
                codex_bin="/opt/homebrew/bin/codex",
                queue_path=Path("/tmp/queue.jsonl"),
                log_path=Path("/tmp/tokenmaxx.log"),
                interval_seconds=300,
                codex_logs_db=relative_logs_db,
            )
        finally:
            os.chdir(original_cwd)

        arguments = plistlib.loads(plist.encode())["ProgramArguments"]
        logs_path = Path(arguments[arguments.index("--codex-logs-db") + 1])
        self.assertTrue(logs_path.is_absolute())
        self.assertEqual(logs_path, expected_logs_db)

    def test_build_launchd_plist_embeds_path_environment(self):
        plist = launchd.build_launchd_plist(
            program="/usr/local/bin/tokenmaxx",
            claude_bin="/usr/local/bin/claude",
            codex_bin=None,
            queue_path=Path("/tmp/queue.jsonl"),
            log_path=Path("/tmp/tokenmaxx.log"),
            interval_seconds=300,
            path_env="/opt/custom/bin:/usr/bin:/bin",
        )
        self.assertIn("<key>EnvironmentVariables</key>", plist)
        self.assertIn("<key>PATH</key>", plist)
        self.assertIn("<string>/opt/custom/bin:/usr/bin:/bin</string>", plist)

    def test_drop_tombstones_queue_item(self):
        append_queue_item(
            self.queue_path,
            QueueItem(cwd="/tmp/repo", session_id="abc", lease_id="running", lease_pid=4321),
        )
        append_queue_item(self.queue_path, QueueItem(cwd="/tmp/repo", session_id="keep"))

        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_drop(self.args(session_id="abc"))

        self.assertEqual(code, 0)
        self.assertIn("Dropped abc", output.getvalue())
        dropped, kept = load_queue(self.queue_path)
        self.assertEqual(dropped.status, "blocked")
        self.assertEqual(dropped.blocked_reason, "dropped by user")
        self.assertEqual(dropped.lease_id, "")
        self.assertEqual(dropped.lease_pid, 0)
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
            updated.lease_id = item.lease_id
            with queue_module.resume_lock(self.queue_path) as resume_lock_acquired:
                observed["resume_lock_acquired"] = resume_lock_acquired
            with queue_module.queue_lock(self.queue_path, timeout_seconds=0):
                observed["queue_at_resume"] = load_queue(self.queue_path)
            return updated

        output = io.StringIO()
        with patch("tokenmaxx.cli.claude.run_due_item", side_effect=fake_run), redirect_stdout(output):
            code = cli.cmd_watch(self.args(dry_run=False, now=now, auto_queue=False))

        self.assertEqual(code, 0)
        self.assertTrue(is_due(observed["item"], now))
        self.assertTrue(observed["item"].lease_id)
        self.assertFalse(observed["resume_lock_acquired"])
        lease_row = observed["queue_at_resume"][0]
        self.assertEqual(lease_row.status, "pending")
        self.assertGreater(lease_row.next_attempt_at, now + 7_200)
        final = load_queue(self.queue_path)[0]
        self.assertEqual(final.status, "done")
        self.assertEqual(final.attempts, 1)

    def test_watch_defers_expired_lease_while_provider_process_is_alive(self):
        now = 1_000_000
        lease_path = self.root / "lease.lock"
        append_queue_item(
            self.queue_path,
            QueueItem(
                cwd="/tmp/repo",
                session_id="abc",
                status="pending",
                next_attempt_at=now - 1,
                lease_id="old-lease",
                lease_lock_path=str(lease_path),
            ),
        )
        args = self.args(now=now, auto_queue=False, dry_run=False)

        with queue_module.process_lease_lock(lease_path):
            with patch("tokenmaxx.cli.run_resume") as run_resume:
                processed = cli.run_watch_cycle(args, {"claude": "claude"})

        self.assertFalse(processed)
        run_resume.assert_not_called()
        item = load_queue(self.queue_path)[0]
        self.assertEqual(item.status, "pending")
        self.assertEqual(item.next_attempt_at, now + args.followup_delay_seconds)

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

    def test_stale_resume_cannot_overwrite_rearmed_queue_item(self):
        rearmed = [QueueItem(cwd="/tmp/repo", session_id="abc", status="pending", lease_id="fresh")]
        stale = QueueItem(cwd="/tmp/repo", session_id="abc", status="done", lease_id="stale")

        merge_resumed_item(rearmed, stale)

        self.assertEqual(rearmed[0].status, "pending")
        self.assertEqual(rearmed[0].lease_id, "fresh")

    def test_main_reports_locked_queue_without_traceback(self):
        error = io.StringIO()
        with patch("tokenmaxx.cli.queue_lock", side_effect=TimeoutError("locked")), redirect_stderr(error):
            code = cli.main(["drop", "--session-id", "x", "--queue", str(self.queue_path)])

        self.assertEqual(code, 1)
        self.assertIn("locked by another tokenmaxx process", error.getvalue())

    def test_launchd_install_dry_run_prints_plist_without_writing(self):
        output = io.StringIO()
        with patch(
            "tokenmaxx.cli.resolve_provider_bin", side_effect=["/usr/local/bin/claude", None]
        ), redirect_stdout(output):
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
            "tokenmaxx.cli.resolve_provider_bin", side_effect=["/usr/local/bin/claude", None]
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

    def test_start_writes_plist_when_only_codex_resolves(self):
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
            codex_bin=None,
            plist_path=self.root / "agents" / "com.local.tokenmaxx.plist",
        )
        with patch("tokenmaxx.cli.resolve_default_program", return_value="/usr/local/bin/tokenmaxx"), patch(
            "tokenmaxx.cli.resolve_provider_bin", side_effect=[None, "/opt/homebrew/bin/codex"]
        ), patch("tokenmaxx.launchd.subprocess.run", side_effect=fake_run), redirect_stdout(output):
            code = cli.cmd_start(args)

        self.assertEqual(code, 0)
        plist = args.plist_path.read_text()
        self.assertNotIn("<string>--claude-bin</string>", plist)
        self.assertIn("<string>--codex-bin</string>", plist)
        self.assertIn("<string>/opt/homebrew/bin/codex</string>", plist)
        self.assertIn("<string>--codex-logs-db</string>", plist)
        self.assertIn(f"<string>{self.codex_logs_db.resolve()}</string>", plist)
        self.assertIn(["launchctl", "load", str(args.plist_path)], calls)

    def test_start_requires_at_least_one_provider_executable(self):
        error = io.StringIO()

        with patch("tokenmaxx.cli.resolve_default_program", return_value="/usr/local/bin/tokenmaxx"), patch(
            "tokenmaxx.cli.resolve_provider_bin", return_value=None
        ), redirect_stderr(error):
            code = cli.cmd_start(self.args(claude_bin=None, codex_bin=None))

        self.assertEqual(code, 1)
        self.assertIn("Neither claude nor codex is on PATH", error.getvalue())

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
            "tokenmaxx.cli.resolve_provider_bin", side_effect=["/usr/local/bin/claude", None]
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
