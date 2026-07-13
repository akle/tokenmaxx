from __future__ import annotations

import json
import os
from pathlib import Path

from .config import DEFAULT_PROMPT
from .queue import QueueItem, apply_limit_event, classify_output
from .runner import run_due_command
from .transcript import record_timestamp, tail_records as transcript_tail_records


def load_claude_sessions(sessions_dir: Path) -> list[dict]:
    sessions_dir = Path(sessions_dir).expanduser()
    if not sessions_dir.exists():
        return []
    sessions: list[dict] = []
    for path in sessions_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            # Claude Code deletes session files on exit; this one vanished
            # between the glob and the read.
            continue
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if not isinstance(data.get("sessionId"), str) or not data.get("sessionId"):
            continue
        if not isinstance(data.get("cwd"), str) or not data.get("cwd"):
            continue
        try:
            int(data.get("updatedAt") or 0)
        except (TypeError, ValueError):
            continue
        data["_path"] = str(path)
        sessions.append(data)
    return sorted(sessions, key=lambda item: int(item.get("updatedAt") or 0), reverse=True)


def session_updated_at_seconds(session: dict) -> int:
    return int(session.get("updatedAt") or 0) // 1000


def find_transcript(projects_dir: Path, session_id: str) -> Path | None:
    projects_dir = Path(projects_dir).expanduser()
    matches = []
    for path in projects_dir.glob(f"*/{session_id}.jsonl"):
        try:
            matches.append((path.stat().st_mtime, path))
        except OSError:
            continue
    matches.sort(key=lambda entry: entry[0], reverse=True)
    return matches[0][1] if matches else None


SYNTHETIC_MODEL = "<synthetic>"


def message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def session_limit_hit_at(session: dict, projects_dir: Path) -> int | None:
    """Epoch of the limit banner ending the session, or None.

    Limit banners arrive as synthetic assistant records (model "<synthetic>").
    Matching only those — instead of raw transcript text — keeps sessions that
    merely *talk about* limits (tool output, file contents) out of the queue,
    and skips sessions that already resumed past a limit. A banner without a
    parseable timestamp reports epoch 0 (queued if new, never re-arms).
    """
    transcript = find_transcript(projects_dir, str(session.get("sessionId")))
    if transcript is None:
        return None
    for record in reversed(transcript_tail_records(transcript)):
        message = record.get("message")
        if record.get("type") != "assistant" or not isinstance(message, dict):
            continue
        if message.get("model") == SYNTHETIC_MODEL:
            if classify_output(message_text(message)) == "limited":
                return record_timestamp(record)
            continue
        return None
    return None


def build_limited_queue_items(
    sessions: list[dict],
    items: list[QueueItem],
    *,
    projects_dir: Path,
    now: int,
    max_session_age_hours: float,
) -> list[QueueItem]:
    """Queue limited sessions, appending new items to `items` in place.

    A session already in the queue is re-armed (reset to pending with fresh
    attempts) only when its limit banner is NEWER than the row's last update —
    a new limit event after the row was resolved. A user drop stays dropped.
    Returns the affected items, new and re-armed.
    """
    max_age_seconds = int(float(max_session_age_hours) * 60 * 60)
    affected: list[QueueItem] = []
    for session in sessions:
        session_id = str(session.get("sessionId") or "")
        if not session_id:
            continue
        updated_at = session_updated_at_seconds(session)
        if updated_at <= 0 or now - updated_at > max_age_seconds:
            continue
        hit_at = session_limit_hit_at(session, projects_dir)
        if hit_at is None:
            continue
        item = apply_limit_event(
            items,
            provider="claude",
            session_id=session_id,
            cwd=str(session["cwd"]),
            hit_at=hit_at,
            now=now,
        )
        if item is not None:
            affected.append(item)
    return affected


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, TypeError, ValueError, OverflowError):
        return False
    except PermissionError:
        # Alive but owned by another user: Claude Code always runs as the
        # session's user, so this pid was recycled by an unrelated process.
        return False
    return True


# A genuinely busy Claude bumps its session file constantly; a "busy" file
# frozen for a day is a crash leftover whose pid may have been recycled to an
# unrelated same-user process.
BUSY_STALENESS_SECONDS = 24 * 60 * 60


def session_is_active(session: dict, now: int, grace_seconds: int) -> bool:
    if not pid_alive(session.get("pid")):
        return False
    age = now - session_updated_at_seconds(session)
    if session.get("status") == "busy":
        return age < BUSY_STALENESS_SECONDS
    return age < grace_seconds


def find_active_session(sessions: list[dict], session_id: str, now: int, grace_seconds: int) -> dict | None:
    for session in sessions:
        if str(session.get("sessionId")) == session_id and session_is_active(session, now, grace_seconds):
            return session
    return None


def build_resume_command(item: QueueItem, claude_bin: str, prompt: str = DEFAULT_PROMPT) -> list[str]:
    return [claude_bin, "--resume", item.session_id, "-p", prompt]


def run_due_item(
    item: QueueItem,
    *,
    now: int,
    claude_bin: str,
    dry_run: bool,
    retry_delay_seconds: int,
    followup_delay_seconds: int,
    max_attempts: int,
    resume_timeout_seconds: int,
    on_process_start=None,
) -> QueueItem:
    command = build_resume_command(item, claude_bin, DEFAULT_PROMPT)
    return run_due_command(
        item,
        command,
        provider_name="claude",
        now=now,
        dry_run=dry_run,
        retry_delay_seconds=retry_delay_seconds,
        followup_delay_seconds=followup_delay_seconds,
        max_attempts=max_attempts,
        resume_timeout_seconds=resume_timeout_seconds,
        on_process_start=on_process_start,
    )
