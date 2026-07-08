from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
from datetime import datetime
from pathlib import Path

from .config import DEFAULT_PROMPT
from .queue import QueueItem, classify_output, is_due, update_item_after_output


def load_claude_sessions(sessions_dir: Path) -> list[dict]:
    sessions_dir = Path(sessions_dir).expanduser()
    if not sessions_dir.exists():
        return []
    sessions: list[dict] = []
    for path in sessions_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            # Claude Code deletes session files on exit; this one vanished
            # between the glob and the read.
            continue
        except json.JSONDecodeError:
            continue
        if not data.get("sessionId") or not data.get("cwd"):
            continue
        data["_path"] = str(path)
        sessions.append(data)
    return sorted(sessions, key=lambda item: int(item.get("updatedAt") or 0), reverse=True)


def session_updated_at_seconds(session: dict) -> int:
    return int(session.get("updatedAt") or 0) // 1000


def find_transcript(projects_dir: Path, session_id: str) -> Path | None:
    projects_dir = Path(projects_dir).expanduser()
    matches = sorted(projects_dir.glob(f"*/{session_id}.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


SYNTHETIC_MODEL = "<synthetic>"


def transcript_tail_records(path: Path, max_lines: int = 80) -> list[dict]:
    records: list[dict] = []
    for line in path.read_text(errors="replace").splitlines()[-max_lines:]:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def record_timestamp(record: dict) -> int:
    raw = record.get("timestamp")
    if isinstance(raw, str):
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return 0


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
    latest_by_id: dict[str, QueueItem] = {item.session_id: item for item in items}
    max_age_seconds = int(float(max_session_age_hours) * 60 * 60)
    affected: list[QueueItem] = []
    for session in sessions:
        session_id = str(session.get("sessionId") or "")
        if not session_id:
            continue
        updated_at = session_updated_at_seconds(session)
        if updated_at <= 0 or now - updated_at > max_age_seconds:
            continue
        existing = latest_by_id.get(session_id)
        if existing is not None and (existing.status == "pending" or existing.blocked_reason == "dropped by user"):
            continue
        hit_at = session_limit_hit_at(session, projects_dir)
        if hit_at is None:
            continue
        if existing is None:
            item = QueueItem(cwd=str(session["cwd"]), session_id=session_id)
            items.append(item)
            latest_by_id[session_id] = item
            affected.append(item)
            continue
        if hit_at <= existing.updated_at:
            continue
        existing.status = "pending"
        existing.attempts = 0
        existing.next_attempt_at = 0
        existing.blocked_reason = ""
        existing.last_output = ""
        existing.updated_at = now
        affected.append(existing)
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


def dry_run_output(command: list[str]) -> str:
    return "DRY RUN: " + " ".join(shlex.quote(part) for part in command)


def terminate_process_group(process: subprocess.Popen[str], *, grace_seconds: int = 5) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (AttributeError, ProcessLookupError, PermissionError, OSError):
        process.terminate()
    try:
        process.communicate(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (AttributeError, ProcessLookupError, PermissionError, OSError):
        process.kill()
    process.communicate()


def run_resume_command(command: list[str], *, cwd: str, timeout_seconds: int) -> tuple[int, str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds if timeout_seconds > 0 else None)
    except subprocess.TimeoutExpired as exc:
        terminate_process_group(process)
        partial = "\n".join(
            part.decode(errors="replace") if isinstance(part, bytes) else part
            for part in (exc.stdout, exc.stderr)
            if part
        )
        message = f"tokenmaxx: claude resume timed out after {timeout_seconds} seconds"
        return 124, "\n".join(part for part in (partial, message) if part)
    return process.returncode or 0, "\n".join(part for part in (stdout, stderr) if part)


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
) -> QueueItem:
    if not is_due(item, now):
        return item
    command = build_resume_command(item, claude_bin, DEFAULT_PROMPT)
    if dry_run:
        item.last_output = dry_run_output(command)
        item.updated_at = now
        return item
    returncode, output = run_resume_command(command, cwd=item.cwd, timeout_seconds=resume_timeout_seconds)
    if returncode != 0 and not output:
        output = f"claude exited with code {returncode}"
    return update_item_after_output(
        item,
        output,
        now=now,
        retry_delay_seconds=retry_delay_seconds,
        followup_delay_seconds=followup_delay_seconds,
        max_attempts=max_attempts,
    )
