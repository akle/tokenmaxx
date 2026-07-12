from __future__ import annotations

import json
from pathlib import Path


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
            with path.open() as handle:
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
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return sorted(sessions, key=lambda item: item["updatedAt"], reverse=True)
