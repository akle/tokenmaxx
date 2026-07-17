from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .config import CODEX_PROMPT
from .queue import RESET_BUFFER_SECONDS, QueueItem, apply_limit_event
from .runner import run_due_command
from .transcript import record_timestamp, tail_records


ACTIVE_STALENESS_SECONDS = 24 * 60 * 60
HISTORY_SCAN_MAX_LINES = 2_000
ROLLOUT_ACTIVITY_SCAN_MAX_LINES = 2_000
REMOTE_COMPACT_ERROR_BODY = "stream disconnected before completion: error sending request for url"
REMOTE_COMPACT_ERROR_PREFIXES = (
    f"\u25a0 Error running remote compact task: {REMOTE_COMPACT_ERROR_BODY}",
    f"\u25a0 {REMOTE_COMPACT_ERROR_BODY}",
)
REMOTE_COMPACT_ERROR_URL = "https://chatgpt.com/backend-api/codex/responses"
MODEL_CAPACITY_LOG_TARGET = "codex_core::session::turn"
MODEL_CAPACITY_ERROR = "Selected model is at capacity. Please try a different model."
MODEL_CAPACITY_LOG_SUFFIX = f"Turn error: {MODEL_CAPACITY_ERROR}"
MODEL_CAPACITY_RETRY_SECONDS = 5 * 60


def is_usage_limit_error(payload: dict) -> bool:
    error_info = payload.get("codex_error_info")
    message = payload.get("message")
    return error_info == "usage_limit_exceeded" or (
        error_info is None
        and isinstance(message, str)
        and message.startswith("You've hit your usage limit.")
    )


def is_remote_compact_disconnect(text: object) -> bool:
    if not isinstance(text, str):
        return False
    normalized = text.strip()
    return (
        normalized.startswith(REMOTE_COMPACT_ERROR_PREFIXES)
        and REMOTE_COMPACT_ERROR_URL in normalized
    )


def history_record_timestamp(record: dict) -> int:
    try:
        return int(record.get("ts"))
    except (TypeError, ValueError):
        return 0


def load_remote_compact_events(
    history_path: Path | None,
    *,
    now: int,
    max_session_age_hours: float,
) -> dict[str, int]:
    """Return recent Codex remote-compaction failures keyed by session ID.

    Codex persists this particular failure in `~/.codex/history.jsonl`, not in
    the rollout's provider event stream. Keep the match exact so ordinary
    transport errors or user-copied text cannot auto-queue a session.
    """
    if history_path is None:
        return {}
    max_age_seconds = float(max_session_age_hours) * 60 * 60
    events: dict[str, int] = {}
    for record in tail_records(Path(history_path), max_lines=HISTORY_SCAN_MAX_LINES):
        session_id = record.get("session_id")
        hit_at = history_record_timestamp(record)
        if (
            not isinstance(session_id, str)
            or not session_id
            or hit_at <= 0
            or hit_at > now
            or now - hit_at > max_age_seconds
            or not is_remote_compact_disconnect(record.get("text"))
        ):
            continue
        events[session_id] = max(hit_at, events.get(session_id, 0))
    return events


def load_model_capacity_events(
    logs_path: Path | None,
    session_ids: set[str],
    *,
    now: int,
    max_session_age_hours: float,
) -> dict[str, int]:
    if logs_path is None or not session_ids:
        return {}
    oldest = now - int(float(max_session_age_hours) * 60 * 60)
    events: dict[str, int] = {}
    try:
        database_uri = Path(logs_path).expanduser().resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(database_uri, uri=True, timeout=0)
        try:
            for session_id in session_ids:
                rows = connection.execute(
                    """
                    SELECT ts, feedback_log_body
                    FROM logs
                    WHERE thread_id = ?
                      AND ts BETWEEN ? AND ?
                      AND target = ?
                    ORDER BY ts DESC, ts_nanos DESC, id DESC
                    """,
                    (session_id, oldest, now, MODEL_CAPACITY_LOG_TARGET),
                )
                for raw_hit_at, body in rows:
                    if isinstance(body, str) and body.endswith(MODEL_CAPACITY_LOG_SUFFIX):
                        events[session_id] = int(raw_hit_at)
                        break
        finally:
            connection.close()
    except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
        return {}
    return events


def rate_limit_telemetry(payload: dict, observed_at: int) -> tuple[bool, int | None] | None:
    """Return whether provider telemetry says a Codex rate limit is terminal.

    Recent Codex versions emit exhausted windows in `token_count` events rather
    than a separate `error` event. A stale 100% snapshot is ignored when its
    reset is already in the past; this prevents old rollout metadata from
    re-arming a completed session.
    """
    if payload.get("type") != "token_count":
        return None
    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return None

    reached_type = rate_limits.get("rate_limit_reached_type")
    exhausted = isinstance(reached_type, str) and bool(reached_type.strip())
    reset_times: list[int] = []
    for bucket_name in ("primary", "secondary"):
        bucket = rate_limits.get(bucket_name)
        if not isinstance(bucket, dict):
            continue
        try:
            used_percent = float(bucket.get("used_percent"))
        except (TypeError, ValueError):
            continue
        if used_percent < 100:
            continue
        raw_reset = bucket.get("resets_at")
        try:
            reset_at = int(raw_reset)
        except (TypeError, ValueError):
            reset_at = 0
        if reset_at <= 0 or reset_at > observed_at:
            exhausted = True
        if reset_at > observed_at:
            reset_times.append(reset_at)

    if not exhausted:
        return None
    return True, min(reset_times) if reset_times else None


def load_codex_sessions(
    sessions_dir: Path,
    *,
    now: int,
    max_session_age_hours: float,
) -> list[dict]:
    sessions_dir = Path(sessions_dir).expanduser()
    if not sessions_dir.exists():
        return []
    max_age_seconds = float(max_session_age_hours) * 60 * 60
    sessions: list[dict] = []
    for path in sessions_dir.glob("**/*.jsonl"):
        try:
            updated_at = path.stat().st_mtime
            if now - updated_at > max_age_seconds:
                continue
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        continue
                    if record.get("type") != "session_meta":
                        continue
                    payload = record.get("payload")
                    if not isinstance(payload, dict):
                        break
                    session_id = payload.get("id")
                    cwd = payload.get("cwd")
                    if not isinstance(session_id, str) or not session_id or not isinstance(cwd, str) or not cwd:
                        break
                    sessions.append(
                        {
                            "sessionId": session_id,
                            "cwd": cwd,
                            "updatedAt": int(updated_at * 1000),
                            "_path": str(path),
                        }
                    )
                    break
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(sessions, key=lambda item: item["updatedAt"], reverse=True)


def session_updated_at_seconds(session: dict) -> int:
    return int(session.get("updatedAt") or 0) // 1000


def session_limit_info(session: dict) -> tuple[int, int | None] | None:
    path = session.get("_path")
    if not path:
        return None
    saw_task_complete = False
    for record in reversed(tail_records(Path(path))):
        if record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        event_type = payload.get("type")
        if event_type == "task_started":
            return None
        if event_type == "task_complete":
            saw_task_complete = True
            continue
        if event_type == "token_count":
            if saw_task_complete:
                return None
            telemetry = rate_limit_telemetry(payload, record_timestamp(record))
            if telemetry is not None:
                return record_timestamp(record), telemetry[1]
            continue
        if event_type != "error":
            continue
        if is_usage_limit_error(payload):
            return record_timestamp(record), None
        return None
    return None


def session_limit_hit_at(session: dict) -> int | None:
    info = session_limit_info(session)
    return info[0] if info is not None else None


def session_limit_retry_at(session: dict) -> int | None:
    info = session_limit_info(session)
    return info[1] if info is not None else None


def session_has_newer_task_activity(session: dict, event_at: int) -> bool:
    path = session.get("_path")
    if not path:
        return False
    for record in tail_records(Path(path), max_lines=ROLLOUT_ACTIVITY_SCAN_MAX_LINES):
        if record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") not in {"task_started", "task_complete"}:
            continue
        if record_timestamp(record) > event_at:
            return True
    return False


def remote_compact_hit_at(session: dict, events: dict[str, int]) -> int | None:
    session_id = str(session.get("sessionId") or "")
    hit_at = events.get(session_id)
    if hit_at is None or session_has_newer_task_activity(session, hit_at):
        return None
    return hit_at


def reschedule_pending_capacity_item(
    items: list[QueueItem],
    *,
    session_id: str,
    hit_at: int,
    retry_at: int,
) -> QueueItem | None:
    for existing in reversed(items):
        if existing.key != ("codex", session_id):
            continue
        if existing.status != "pending" or hit_at < existing.updated_at:
            return None
        if existing.next_attempt_at == retry_at:
            return None
        existing.next_attempt_at = retry_at
        return existing
    return None


def build_limited_queue_items(
    sessions: list[dict],
    items: list[QueueItem],
    *,
    now: int,
    max_session_age_hours: float,
    history_path: Path | None = None,
    logs_path: Path | None = None,
) -> list[QueueItem]:
    max_age_seconds = int(float(max_session_age_hours) * 60 * 60)
    compact_events = load_remote_compact_events(
        history_path,
        now=now,
        max_session_age_hours=max_session_age_hours,
    )
    capacity_events = load_model_capacity_events(
        logs_path,
        {str(session.get("sessionId") or "") for session in sessions},
        now=now,
        max_session_age_hours=max_session_age_hours,
    )
    affected: list[QueueItem] = []
    for session in sessions:
        session_id = str(session.get("sessionId") or "")
        if not session_id:
            continue
        updated_at = session_updated_at_seconds(session)
        if updated_at <= 0 or now - updated_at > max_age_seconds:
            continue
        info = session_limit_info(session)
        compact_hit_at = remote_compact_hit_at(session, compact_events)
        if compact_hit_at is not None and (info is None or compact_hit_at > info[0]):
            info = (compact_hit_at, None)
        capacity_retry_at = None
        capacity_hit_at = remote_compact_hit_at(session, capacity_events)
        if capacity_hit_at is not None and (info is None or capacity_hit_at > info[0]):
            info = (capacity_hit_at, None)
            capacity_retry_at = capacity_hit_at + MODEL_CAPACITY_RETRY_SECONDS
        if info is None:
            continue
        hit_at, retry_at = info
        item = apply_limit_event(
            items,
            provider="codex",
            session_id=session_id,
            cwd=str(session["cwd"]),
            hit_at=hit_at,
            now=now,
        )
        if item is None and capacity_retry_at is not None:
            item = reschedule_pending_capacity_item(
                items,
                session_id=session_id,
                hit_at=hit_at,
                retry_at=capacity_retry_at,
            )
        if item is not None:
            if capacity_retry_at is not None and capacity_retry_at > now:
                item.next_attempt_at = capacity_retry_at
            elif retry_at is not None and retry_at > now:
                item.next_attempt_at = retry_at + RESET_BUFFER_SECONDS
            affected.append(item)
    return affected


def latest_external_stop_at(
    session: dict,
    *,
    history_path: Path | None,
    logs_path: Path | None,
    now: int,
    max_session_age_hours: float,
) -> int | None:
    session_id = str(session.get("sessionId") or "")
    compact_events = load_remote_compact_events(
        history_path,
        now=now,
        max_session_age_hours=max_session_age_hours,
    )
    capacity_events = load_model_capacity_events(
        logs_path,
        {session_id} if session_id else set(),
        now=now,
        max_session_age_hours=max_session_age_hours,
    )
    candidates = [
        hit_at
        for hit_at in (
            remote_compact_hit_at(session, compact_events),
            remote_compact_hit_at(session, capacity_events),
        )
        if hit_at is not None
    ]
    return max(candidates) if candidates else None


def session_is_active(
    session: dict,
    now: int,
    grace_seconds: int,
    external_stop_at: int | None = None,
) -> bool:
    path = session.get("_path")
    if not path:
        return now - session_updated_at_seconds(session) < grace_seconds
    rollout_path = Path(path)
    if not rollout_path.is_file():
        return False
    records = tail_records(rollout_path)
    if not rollout_path.is_file():
        return False
    if external_stop_at is not None and not session_has_newer_task_activity(
        session, external_stop_at
    ):
        return False
    for record in reversed(records):
        if record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        event_type = payload.get("type")
        if event_type == "task_complete":
            return False
        if event_type == "error" and is_usage_limit_error(payload):
            return False
        if event_type == "token_count" and rate_limit_telemetry(
            payload, record_timestamp(record)
        ):
            return False
        if event_type == "task_started":
            return now - record_timestamp(record) < ACTIVE_STALENESS_SECONDS
    return now - session_updated_at_seconds(session) < grace_seconds


def find_active_session(
    sessions: list[dict],
    session_id: str,
    now: int,
    grace_seconds: int,
    *,
    history_path: Path | None = None,
    logs_path: Path | None = None,
    max_session_age_hours: float = 24.0,
) -> dict | None:
    for session in sessions:
        if str(session.get("sessionId")) != session_id:
            continue
        external_stop_at = latest_external_stop_at(
            session,
            history_path=history_path,
            logs_path=logs_path,
            now=now,
            max_session_age_hours=max_session_age_hours,
        )
        if session_is_active(session, now, grace_seconds, external_stop_at):
            return session
    return None


def build_resume_command(item: QueueItem, codex_bin: str, prompt: str = CODEX_PROMPT) -> list[str]:
    return [codex_bin, "exec", "resume", "--all", item.session_id, prompt]


def run_due_item(
    item: QueueItem,
    *,
    now: int,
    codex_bin: str,
    dry_run: bool,
    retry_delay_seconds: int,
    followup_delay_seconds: int,
    max_attempts: int,
    resume_timeout_seconds: int,
    on_process_start=None,
    lease_lock_path=None,
) -> QueueItem:
    return run_due_command(
        item,
        build_resume_command(item, codex_bin, CODEX_PROMPT),
        provider_name="codex",
        now=now,
        dry_run=dry_run,
        retry_delay_seconds=retry_delay_seconds,
        followup_delay_seconds=followup_delay_seconds,
        max_attempts=max_attempts,
        resume_timeout_seconds=resume_timeout_seconds,
        on_process_start=on_process_start,
        lease_lock_path=lease_lock_path,
    )
