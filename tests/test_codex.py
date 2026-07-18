import json
import os
import sqlite3
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

    def write_history(self, records):
        path = self.root / "history.jsonl"
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        return path

    def write_logs_db(self, records):
        path = self.root / "logs_2.sqlite"
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                """
                CREATE TABLE logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    ts_nanos INTEGER NOT NULL,
                    target TEXT NOT NULL,
                    thread_id TEXT,
                    feedback_log_body TEXT
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO logs (ts, ts_nanos, target, thread_id, feedback_log_body)
                VALUES (?, ?, ?, ?, ?)
                """,
                records,
            )
            connection.commit()
        finally:
            connection.close()
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

    def test_remote_compact_disconnect_requires_the_exact_codex_history_message(self):
        message = (
            "\u25a0 Error running remote compact task: stream disconnected before completion: "
            "error sending request for url\n"
            "(https://chatgpt.com/backend-api/codex/responses)"
        )

        self.assertTrue(codex.is_remote_compact_disconnect(message))
        self.assertTrue(
            codex.is_remote_compact_disconnect(
                "\u25a0 stream disconnected before completion: error sending request for url "
                "(https://chatgpt.com/backend-api/codex/responses)"
            )
        )
        self.assertFalse(codex.is_remote_compact_disconnect("stream disconnected before completion"))
        self.assertFalse(codex.is_remote_compact_disconnect(message.replace("remote compact", "remote request")))

    def test_history_remote_compact_disconnect_is_queued(self):
        self.write_rollout()
        history = self.write_history(
            [
                {
                    "session_id": "codex-1",
                    "ts": 980,
                    "text": (
                        "\u25a0 stream disconnected before completion: error sending request for url "
                        "(https://chatgpt.com/backend-api/codex/responses)"
                    ),
                }
            ]
        )
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)
        items = []

        affected = codex.build_limited_queue_items(
            sessions,
            items,
            now=1000,
            max_session_age_hours=1,
            history_path=history,
        )

        self.assertEqual(affected, items)
        self.assertEqual(items[0].session_id, "codex-1")
        self.assertEqual(items[0].provider, "codex")

    def test_newer_rollout_task_activity_suppresses_history_remote_compact_disconnect(self):
        self.write_rollout(records=(event("task_started", "1970-01-01T00:16:30Z"),))
        history = self.write_history(
            [
                {
                    "session_id": "codex-1",
                    "ts": 980,
                    "text": (
                        "\u25a0 Error running remote compact task: stream disconnected before completion: "
                        "error sending request for url\n"
                        "(https://chatgpt.com/backend-api/codex/responses)"
                    ),
                }
            ]
        )
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)
        items = []

        codex.build_limited_queue_items(
            sessions,
            items,
            now=1000,
            max_session_age_hours=1,
            history_path=history,
        )

        self.assertEqual(items, [])

    def test_exact_model_capacity_log_queues_same_thread_after_five_minutes(self):
        self.write_rollout()
        logs = self.write_logs_db(
            [
                (
                    980,
                    10,
                    "codex_core::session::turn",
                    "codex-1",
                    "session_loop{thread_id=codex-1}: Turn error: "
                    "Selected model is at capacity. Please try a different model.",
                )
            ]
        )
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)
        items = []

        affected = codex.build_limited_queue_items(
            sessions,
            items,
            now=1000,
            max_session_age_hours=1,
            logs_path=logs,
        )

        self.assertEqual(affected, items)
        self.assertEqual(items[0].key, ("codex", "codex-1"))
        self.assertEqual(items[0].next_attempt_at, 1280)

    def test_newer_rollout_activity_suppresses_model_capacity_log(self):
        self.write_rollout(records=(event("task_started", "1970-01-01T00:16:30Z"),))
        logs = self.write_logs_db(
            [
                (
                    980,
                    0,
                    "codex_core::session::turn",
                    "codex-1",
                    "Turn error: Selected model is at capacity. Please try a different model.",
                )
            ]
        )
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)
        items = []

        codex.build_limited_queue_items(
            sessions,
            items,
            now=1000,
            max_session_age_hours=1,
            logs_path=logs,
        )

        self.assertEqual(items, [])

    def test_model_capacity_stop_makes_open_rollout_turn_inactive(self):
        self.write_rollout(records=(event("task_started", "1970-01-01T00:16:10Z"),))
        logs = self.write_logs_db(
            [
                (
                    980,
                    0,
                    "codex_core::session::turn",
                    "codex-1",
                    "Turn error: Selected model is at capacity. Please try a different model.",
                )
            ]
        )
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)

        active = codex.find_active_session(
            sessions,
            "codex-1",
            1000,
            30,
            logs_path=logs,
            max_session_age_hours=1,
        )

        self.assertIsNone(active)

    def test_model_capacity_loader_preserves_subsecond_ordering_time(self):
        logs = self.write_logs_db(
            [
                (
                    980,
                    889_999_000,
                    "codex_core::session::turn",
                    "codex-1",
                    codex.MODEL_CAPACITY_LOG_SUFFIX,
                )
            ]
        )

        capacity_event = codex.load_model_capacity_events(
            logs,
            {"codex-1"},
            now=1000,
            max_session_age_hours=1,
        )["codex-1"]

        self.assertEqual(capacity_event.hit_at, 980)
        self.assertEqual(capacity_event.ordering_at_ns, 980_889_999_000)

    def test_same_second_task_complete_after_capacity_suppresses_queue_discovery(self):
        self.write_rollout(
            records=(
                event("task_started", "1970-01-01T00:16:20.800Z"),
                event("task_complete", "1970-01-01T00:16:20.891Z"),
            )
        )
        logs = self.write_logs_db(
            [
                (
                    980,
                    889_999_000,
                    "codex_core::session::turn",
                    "codex-1",
                    codex.MODEL_CAPACITY_LOG_SUFFIX,
                )
            ]
        )
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)
        items = []

        codex.build_limited_queue_items(
            sessions,
            items,
            now=1000,
            max_session_age_hours=1,
            logs_path=logs,
        )

        self.assertEqual(items, [])

    def test_same_second_task_started_after_capacity_keeps_session_active(self):
        self.write_rollout(records=(event("task_started", "1970-01-01T00:16:20.891Z"),))
        logs = self.write_logs_db(
            [
                (
                    980,
                    889_999_000,
                    "codex_core::session::turn",
                    "codex-1",
                    codex.MODEL_CAPACITY_LOG_SUFFIX,
                )
            ]
        )
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)

        active = codex.find_active_session(
            sessions,
            "codex-1",
            1000,
            30,
            logs_path=logs,
            max_session_age_hours=1,
        )

        self.assertEqual(active["sessionId"], "codex-1")

    def test_same_second_task_started_before_capacity_still_queues(self):
        self.write_rollout(records=(event("task_started", "1970-01-01T00:16:20.888Z"),))
        logs = self.write_logs_db(
            [
                (
                    980,
                    889_999_000,
                    "codex_core::session::turn",
                    "codex-1",
                    codex.MODEL_CAPACITY_LOG_SUFFIX,
                )
            ]
        )
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)
        items = []

        affected = codex.build_limited_queue_items(
            sessions,
            items,
            now=1000,
            max_session_age_hours=1,
            logs_path=logs,
        )

        self.assertEqual(affected, items)
        self.assertEqual(items[0].next_attempt_at, 1280)

    def test_same_second_task_started_before_capacity_makes_session_inactive(self):
        self.write_rollout(records=(event("task_started", "1970-01-01T00:16:20.888Z"),))
        logs = self.write_logs_db(
            [
                (
                    980,
                    889_999_000,
                    "codex_core::session::turn",
                    "codex-1",
                    codex.MODEL_CAPACITY_LOG_SUFFIX,
                )
            ]
        )
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)

        active = codex.find_active_session(
            sessions,
            "codex-1",
            1000,
            30,
            logs_path=logs,
            max_session_age_hours=1,
        )

        self.assertIsNone(active)

    def test_same_second_capacity_orders_after_second_only_remote_compaction(self):
        self.write_rollout()
        history = self.write_history(
            [
                {
                    "session_id": "codex-1",
                    "ts": 980,
                    "text": (
                        "\u25a0 stream disconnected before completion: error sending request for url "
                        "(https://chatgpt.com/backend-api/codex/responses)"
                    ),
                }
            ]
        )
        logs = self.write_logs_db(
            [
                (
                    980,
                    889_999_000,
                    "codex_core::session::turn",
                    "codex-1",
                    codex.MODEL_CAPACITY_LOG_SUFFIX,
                )
            ]
        )
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)
        items = []

        affected = codex.build_limited_queue_items(
            sessions,
            items,
            now=1000,
            max_session_age_hours=1,
            history_path=history,
            logs_path=logs,
        )

        self.assertEqual(affected, items)
        self.assertEqual(items[0].next_attempt_at, 1280)

    def test_model_capacity_loader_ignores_untrusted_stale_and_partial_rows(self):
        logs = self.write_logs_db(
            [
                (980, 0, "codex_core::session::handlers", "codex-1", codex.MODEL_CAPACITY_LOG_SUFFIX),
                (980, 0, "codex_core::session::turn", None, codex.MODEL_CAPACITY_LOG_SUFFIX),
                (980, 0, "codex_core::session::turn", "codex-1", codex.MODEL_CAPACITY_ERROR),
                (980, 0, "codex_core::session::turn", "codex-1", codex.MODEL_CAPACITY_LOG_SUFFIX + " copied"),
                (1, 0, "codex_core::session::turn", "codex-1", codex.MODEL_CAPACITY_LOG_SUFFIX),
            ]
        )

        self.assertEqual(
            codex.load_model_capacity_events(
                logs,
                {"codex-1"},
                now=1000,
                max_session_age_hours=0.01,
            ),
            {},
        )

    def test_model_capacity_loader_tolerates_missing_malformed_locked_and_bad_schema(self):
        malformed = self.root / "malformed.sqlite"
        malformed.write_text("not sqlite")
        wrong_schema = self.root / "wrong.sqlite"
        connection = sqlite3.connect(wrong_schema)
        try:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
            connection.commit()
        finally:
            connection.close()

        for path in (self.root / "missing.sqlite", malformed, wrong_schema):
            with self.subTest(path=path):
                self.assertEqual(
                    codex.load_model_capacity_events(
                        path,
                        {"codex-1"},
                        now=1000,
                        max_session_age_hours=1,
                    ),
                    {},
                )

        for error in (PermissionError("unreadable"), sqlite3.OperationalError("database is locked")):
            with self.subTest(error=error), patch("tokenmaxx.codex.sqlite3.connect", side_effect=error):
                self.assertEqual(
                    codex.load_model_capacity_events(
                        self.root / "logs.sqlite",
                        {"codex-1"},
                        now=1000,
                        max_session_age_hours=1,
                    ),
                    {},
                )

    def test_model_capacity_loader_tolerates_path_preparation_failures(self):
        for error in (OSError("unreadable path"), RuntimeError("path resolution failed")):
            with self.subTest(error=error), patch(
                "tokenmaxx.codex.Path.resolve", side_effect=error
            ):
                self.assertEqual(
                    codex.load_model_capacity_events(
                        self.root / "logs.sqlite",
                        {"codex-1"},
                        now=1000,
                        max_session_age_hours=1,
                    ),
                    {},
                )

    def test_newer_model_capacity_event_rearms_resolved_item(self):
        self.write_rollout()
        logs = self.write_logs_db(
            [
                (
                    980,
                    0,
                    "codex_core::session::turn",
                    "codex-1",
                    codex.MODEL_CAPACITY_LOG_SUFFIX,
                )
            ]
        )
        items = [
            QueueItem(
                cwd="/tmp/repo",
                session_id="codex-1",
                provider="codex",
                status="blocked",
                attempts=5,
                blocked_reason="max attempts (5) reached",
                updated_at=900,
            )
        ]
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)

        codex.build_limited_queue_items(
            sessions,
            items,
            now=1000,
            max_session_age_hours=1,
            logs_path=logs,
        )

        self.assertEqual(items[0].status, "pending")
        self.assertEqual(items[0].attempts, 0)
        self.assertEqual(items[0].next_attempt_at, 1280)

    def test_model_capacity_event_sets_exact_pending_retry_without_resetting_attempts(self):
        self.write_rollout()
        logs = self.write_logs_db(
            [
                (
                    980,
                    0,
                    "codex_core::session::turn",
                    "codex-1",
                    codex.MODEL_CAPACITY_LOG_SUFFIX,
                )
            ]
        )
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)

        for existing_retry in (0, 1200, 1880):
            with self.subTest(existing_retry=existing_retry):
                items = [
                    QueueItem(
                        cwd="/tmp/repo",
                        session_id="codex-1",
                        provider="codex",
                        status="pending",
                        attempts=2,
                        next_attempt_at=existing_retry,
                        updated_at=980,
                    )
                ]

                affected = codex.build_limited_queue_items(
                    sessions,
                    items,
                    now=1000,
                    max_session_age_hours=1,
                    logs_path=logs,
                )

                self.assertEqual(affected, items)
                self.assertEqual(items[0].attempts, 2)
                self.assertEqual(items[0].updated_at, 980)
                self.assertEqual(items[0].next_attempt_at, 1280)

    def test_history_remote_compact_disconnect_ignores_stale_and_unrelated_records(self):
        self.write_rollout()
        history = self.write_history(
            [
                {
                    "session_id": "stale",
                    "ts": 1,
                    "text": (
                        "\u25a0 Error running remote compact task: stream disconnected before completion: "
                        "error sending request for url\n"
                        "(https://chatgpt.com/backend-api/codex/responses)"
                    ),
                },
                {
                    "session_id": "codex-1",
                    "ts": 980,
                    "text": "The user mentioned a remote compact error in a file.",
                },
            ]
        )

        self.assertEqual(
            codex.load_remote_compact_events(history, now=1000, max_session_age_hours=0.01),
            {},
        )

    def test_terminal_rate_limit_telemetry_is_queued_until_reset(self):
        self.write_rollout(
            records=(
                event(
                    "token_count",
                    "1970-01-01T00:16:20Z",
                    rate_limits={
                        "primary": {
                            "used_percent": 100.0,
                            "window_minutes": 10080,
                            "resets_at": 1100,
                        },
                        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                        "rate_limit_reached_type": None,
                    },
                ),
            )
        )
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)
        items = []

        codex.build_limited_queue_items(
            sessions,
            items,
            now=1000,
            max_session_age_hours=1,
        )

        self.assertEqual(codex.session_limit_hit_at(sessions[0]), 980)
        self.assertEqual(items[0].next_attempt_at, 1160)

    def test_stale_rate_limit_telemetry_is_ignored(self):
        self.write_rollout(
            records=(
                event(
                    "token_count",
                    "1970-01-01T00:16:20Z",
                    rate_limits={
                        "primary": {"used_percent": 100.0, "resets_at": 900},
                        "rate_limit_reached_type": None,
                    },
                ),
            )
        )
        session = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)[0]

        self.assertIsNone(codex.session_limit_hit_at(session))

    def test_completed_task_does_not_requeue_older_rate_limit_telemetry(self):
        self.write_rollout(
            records=(
                event(
                    "token_count",
                    "1970-01-01T00:16:20Z",
                    rate_limits={
                        "primary": {"used_percent": 100.0, "resets_at": 1100},
                        "rate_limit_reached_type": None,
                    },
                ),
                event("task_complete", "1970-01-01T00:16:21Z"),
            )
        )
        session = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)[0]

        self.assertIsNone(codex.session_limit_hit_at(session))

    def test_model_capacity_error_is_not_a_usage_limit(self):
        self.write_rollout(
            records=(
                event(
                    "error",
                    "1970-01-01T00:16:20Z",
                    message="Selected model is at capacity. Please try a different model.",
                ),
            )
        )
        session = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)[0]

        self.assertIsNone(codex.session_limit_hit_at(session))

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
