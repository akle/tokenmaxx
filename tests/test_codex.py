import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tokenmaxx import codex
from tokenmaxx.queue import QueueItem
from tokenmaxx.transcript import tail_records


def event(event_type, timestamp, **payload):
    return {"type": "event_msg", "timestamp": timestamp, "payload": {"type": event_type, **payload}}


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write_rollout(self, session_id="codex-1", cwd="/tmp/repo", records=(), mtime=1000):
        path = self.root / "2026" / "07" / "11" / f"rollout-{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {"type": "session_meta", "timestamp": "1970-01-01T00:16:40Z", "payload": {"id": session_id, "cwd": cwd}}
        path.write_text("\n".join(json.dumps(row) for row in (meta, *records)) + "\n")
        os.utime(path, (mtime, mtime))
        return path

    def test_tail_records_returns_only_requested_valid_dicts(self):
        path = self.root / "tail.jsonl"
        path.write_text('{"n":1}\nnot-json\n{"n":2}\n{"n":3}\n')
        self.assertEqual(tail_records(path, max_lines=2), [{"n": 2}, {"n": 3}])

    def test_tail_records_skips_unreadable_file(self):
        with patch("tokenmaxx.transcript.Path.open", side_effect=PermissionError):
            self.assertEqual(tail_records(self.root / "unreadable.jsonl"), [])

    def test_load_codex_sessions_reads_recent_metadata(self):
        recent = self.write_rollout()
        self.write_rollout(session_id="old", mtime=900)
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=0.02)
        self.assertEqual([row["sessionId"] for row in sessions], ["codex-1"])
        self.assertEqual(sessions[0]["cwd"], "/tmp/repo")
        self.assertEqual(sessions[0]["updatedAt"], 1_000_000)
        self.assertEqual(sessions[0]["_path"], str(recent))

    def test_load_codex_sessions_skips_non_object_records(self):
        path = self.root / "rollout.jsonl"
        meta = {"type": "session_meta", "payload": {"id": "codex-1", "cwd": "/tmp/repo"}}
        path.write_text("\n".join(json.dumps(row) for row in (None, [], meta)) + "\n")
        os.utime(path, (1000, 1000))

        try:
            sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)
        except AttributeError as exc:
            self.fail(f"non-object JSONL records must be skipped: {exc}")

        self.assertEqual([row["sessionId"] for row in sessions], ["codex-1"])

    def test_load_codex_sessions_skips_invalid_utf8_rollout(self):
        path = self.root / "invalid-utf8.jsonl"
        path.write_bytes(b"\xff\xfe\n")
        os.utime(path, (1000, 1000))

        self.assertEqual(
            codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1),
            [],
        )

    def test_load_codex_sessions_rejects_invalid_id_and_cwd_values(self):
        cases = (
            ("empty-id", "", "/tmp/repo"),
            ("non-string-id", 123, "/tmp/repo"),
            ("empty-cwd", "codex-1", ""),
            ("non-string-cwd", "codex-1", ["/tmp/repo"]),
        )
        for name, session_id, cwd in cases:
            case_dir = self.root / name
            path = case_dir / "rollout.jsonl"
            path.parent.mkdir()
            meta = {"type": "session_meta", "payload": {"id": session_id, "cwd": cwd}}
            path.write_text(json.dumps(meta) + "\n")
            os.utime(path, (1000, 1000))

            with self.subTest(name=name):
                sessions = codex.load_codex_sessions(case_dir, now=1000, max_session_age_hours=1)
                self.assertEqual(sessions, [])

    def test_terminal_usage_limit_is_queued_but_generic_error_is_not(self):
        self.write_rollout(
            records=(
                event("task_started", "1970-01-01T00:15:00Z"),
                event(
                    "error",
                    "1970-01-01T00:16:20Z",
                    codex_error_info="usage_limit_exceeded",
                    message="You've hit your usage limit. Try again at 12:52 AM.",
                ),
                event("task_complete", "1970-01-01T00:16:21Z"),
            )
        )
        self.write_rollout(
            session_id="generic",
            records=(
                event(
                    "error",
                    "1970-01-01T00:16:20Z",
                    codex_error_info="bad_request",
                    message="usage limit mentioned by a file",
                ),
            ),
        )
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)
        limited = next(row for row in sessions if row["sessionId"] == "codex-1")
        generic = next(row for row in sessions if row["sessionId"] == "generic")

        self.assertEqual(codex.session_limit_hit_at(limited), 980)
        self.assertIsNone(codex.session_limit_hit_at(generic))

    def test_exact_uncoded_limit_message_is_accepted(self):
        self.write_rollout(
            records=(
                event(
                    "error",
                    "1970-01-01T00:16:20Z",
                    message="You've hit your usage limit. Try again at 12:52 AM.",
                ),
            )
        )
        session = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)[0]

        self.assertEqual(codex.session_limit_hit_at(session), 980)

    def test_newer_task_started_suppresses_old_limit(self):
        self.write_rollout(
            records=(
                event("error", "1970-01-01T00:15:00Z", codex_error_info="usage_limit_exceeded"),
                event("task_started", "1970-01-01T00:16:20Z"),
            )
        )
        session = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)[0]

        self.assertIsNone(codex.session_limit_hit_at(session))

    def test_terminal_limit_error_is_not_an_active_session(self):
        self.write_rollout(
            records=(
                event("task_started", "1970-01-01T00:15:00Z"),
                event("error", "1970-01-01T00:16:20Z", codex_error_info="usage_limit_exceeded"),
            )
        )
        session = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)[0]

        self.assertFalse(codex.session_is_active(session, now=1000, grace_seconds=30))

    def test_active_session_uses_newest_task_state_and_expires_after_a_day(self):
        self.write_rollout(
            session_id="active",
            records=(event("task_started", "1970-01-01T00:16:20Z"),),
        )
        self.write_rollout(
            session_id="complete",
            records=(
                event("task_started", "1970-01-01T00:16:10Z"),
                event("task_complete", "1970-01-01T00:16:20Z"),
            ),
        )
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)

        self.assertEqual(codex.find_active_session(sessions, "active", 1000, 30)["sessionId"], "active")
        self.assertIsNone(codex.find_active_session(sessions, "complete", 1000, 30))
        self.assertIsNone(codex.find_active_session(sessions, "active", 1000 + 86_400, 30))

    def test_deleted_discovered_rollout_is_skipped_by_limit_and_activity_checks(self):
        path = self.write_rollout(
            records=(
                event("task_started", "1970-01-01T00:16:10Z"),
                event("error", "1970-01-01T00:16:20Z", codex_error_info="usage_limit_exceeded"),
            ),
        )
        session = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)[0]
        path.unlink()

        try:
            limit_hit_at = codex.session_limit_hit_at(session)
            is_active = codex.session_is_active(session, now=1000, grace_seconds=30)
        except FileNotFoundError as exc:
            self.fail(f"deleted rollout must be skipped: {exc}")

        self.assertIsNone(limit_hit_at)
        self.assertFalse(is_active)

    def test_build_limited_queue_items_sets_codex_provider(self):
        self.write_rollout(
            records=(event("error", "1970-01-01T00:16:20Z", codex_error_info="usage_limit_exceeded"),)
        )
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)
        items = []

        affected = codex.build_limited_queue_items(
            sessions,
            items,
            now=1000,
            max_session_age_hours=1,
        )

        self.assertEqual(affected, items)
        self.assertEqual(items[0].provider, "codex")
        self.assertEqual(items[0].session_id, "codex-1")

    def test_codex_dry_run_uses_exec_resume(self):
        item = QueueItem(cwd="/tmp/repo", session_id="codex-1", provider="codex")

        result = codex.run_due_item(
            item,
            now=1000,
            codex_bin="codex",
            dry_run=True,
            retry_delay_seconds=18_000,
            followup_delay_seconds=900,
            max_attempts=3,
            resume_timeout_seconds=7200,
        )

        self.assertIn("DRY RUN: codex exec resume --all codex-1", result.last_output)
