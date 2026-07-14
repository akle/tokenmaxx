from __future__ import annotations

import json
from pathlib import Path

from .config import CODEX_PROMPT
from .queue import RESET_BUFFER_SECONDS, QueueItem, apply_limit_event
from .runner import run_due_command
from .transcript import record_timestamp, tail_records


ACTIVE_STALENESS_SECONDS = 24 * 60 * 60


def is_usage_limit_error(payload: dict) -> bool:
    error_info = payload.get("codex_error_info")
    message = payload.get("message")
    return error_info == "usage_limit_exceeded" or (
        error_info is None
        and isinstance(message, str)
        and message.startswith("You've hit your usage limit.")
    )


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


def build_limited_queue_items(
    sessions: list[dict],
    items: list[QueueItem],
    *,
    now: int,
    max_session_age_hours: float,
) -> list[QueueItem]:
    max_age_seconds = int(float(max_session_age_hours) * 60 * 60)
    affected: list[QueueItem] = []
    for session in sessions:
        session_id = str(session.get("sessionId") or "")
        if not session_id:
            continue
        updated_at = session_updated_at_seconds(session)
        if updated_at <= 0 or now - updated_at > max_age_seconds:
            continue
        info = session_limit_info(session)
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
        if item is not None:
            if retry_at is not None and retry_at > now:
                item.next_attempt_at = retry_at + RESET_BUFFER_SECONDS
            affected.append(item)
    return affected


def session_is_active(session: dict, now: int, grace_seconds: int) -> bool:
    path = session.get("_path")
    if not path:
        return now - session_updated_at_seconds(session) < grace_seconds
    rollout_path = Path(path)
    if not rollout_path.is_file():
        return False
    records = tail_records(rollout_path)
    if not rollout_path.is_file():
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
) -> dict | None:
    for session in sessions:
        if str(session.get("sessionId")) == session_id and session_is_active(session, now, grace_seconds):
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
