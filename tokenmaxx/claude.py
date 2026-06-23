from __future__ import annotations

import json
import shlex
import subprocess
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


def transcript_tail(path: Path, max_lines: int = 80) -> str:
    return "\n".join(path.read_text(errors="replace").splitlines()[-max_lines:])


def session_hit_limit(session: dict, projects_dir: Path) -> bool:
    transcript = find_transcript(projects_dir, str(session.get("sessionId")))
    if transcript is None:
        return False
    return classify_output(transcript_tail(transcript)) == "limited"


def build_limited_queue_items(
    sessions: list[dict],
    existing_items: list[QueueItem],
    *,
    projects_dir: Path,
    now: int,
    max_session_age_hours: float,
) -> list[QueueItem]:
    existing_ids = {item.session_id for item in existing_items}
    max_age_seconds = int(float(max_session_age_hours) * 60 * 60)
    items: list[QueueItem] = []
    for session in sessions:
        session_id = str(session.get("sessionId") or "")
        if not session_id or session_id in existing_ids:
            continue
        updated_at = session_updated_at_seconds(session)
        if updated_at <= 0 or now - updated_at > max_age_seconds:
            continue
        if not session_hit_limit(session, projects_dir):
            continue
        item = QueueItem(cwd=str(session["cwd"]), session_id=session_id)
        items.append(item)
        existing_ids.add(session_id)
    return items


def build_resume_command(item: QueueItem, claude_bin: str, prompt: str = DEFAULT_PROMPT) -> list[str]:
    return [claude_bin, "--resume", item.session_id, "-p", prompt]


def dry_run_output(command: list[str]) -> str:
    return "DRY RUN: " + " ".join(shlex.quote(part) for part in command)


def run_due_item(
    item: QueueItem,
    *,
    now: int,
    claude_bin: str,
    dry_run: bool,
    retry_delay_seconds: int,
    followup_delay_seconds: int,
    max_attempts: int,
) -> QueueItem:
    if not is_due(item, now):
        return item
    command = build_resume_command(item, claude_bin, DEFAULT_PROMPT)
    if dry_run:
        item.last_output = dry_run_output(command)
        item.updated_at = now
        return item
    result = subprocess.run(
        command,
        cwd=item.cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if result.returncode != 0 and not output:
        output = f"claude exited with code {result.returncode}"
    return update_item_after_output(
        item,
        output,
        now=now,
        retry_delay_seconds=retry_delay_seconds,
        followup_delay_seconds=followup_delay_seconds,
        max_attempts=max_attempts,
    )
