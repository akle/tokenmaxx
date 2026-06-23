from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from .config import DEFAULT_PROMPT
from .queue import QueueItem, is_due, update_item_after_output


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


def build_resume_command(item: QueueItem, claude_bin: str, prompt: str = DEFAULT_PROMPT) -> list[str]:
    return [claude_bin, "--resume", item.session_id, "-p", prompt]


def dry_run_output(command: list[str]) -> str:
    return "DRY RUN: " + " ".join(shlex.quote(part) for part in command)


def run_due_item(item: QueueItem, now: int, args) -> QueueItem:
    if not is_due(item, now):
        return item
    command = build_resume_command(item, args.claude_bin, DEFAULT_PROMPT)
    if args.dry_run:
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
        retry_delay_seconds=args.retry_delay_seconds,
        followup_delay_seconds=args.followup_delay_seconds,
        max_attempts=args.max_attempts,
    )
