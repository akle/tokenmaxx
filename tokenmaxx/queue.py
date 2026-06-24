from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - macOS/Linux path is covered.
    fcntl = None


LIMIT_MARKERS = (
    "usage limit",
    "credit limit",
    "out of credits",
    "ran out of credits",
    "rate limit",
    "rate limited",
    "temporarily limiting requests",
    "session limit",
    "limit reached",
    "try again later",
)


@dataclass
class QueueItem:
    cwd: str
    session_id: str
    status: str = "pending"
    next_attempt_at: int = 0
    attempts: int = 0
    last_output: str = ""
    blocked_reason: str = ""
    created_at: int = 0
    updated_at: int = 0

    def __post_init__(self) -> None:
        now = int(time.time())
        self.next_attempt_at = int(self.next_attempt_at or 0)
        self.attempts = int(self.attempts or 0)
        self.created_at = int(self.created_at or now)
        self.updated_at = int(self.updated_at or self.created_at)

    @classmethod
    def from_dict(cls, data: dict) -> "QueueItem":
        return cls(
            cwd=str(data["cwd"]),
            session_id=str(data.get("sessionId") or data.get("session_id")),
            status=str(data.get("status", "pending")),
            next_attempt_at=int(data.get("nextAttemptAt") or data.get("next_attempt_at") or 0),
            attempts=int(data.get("attempts", 0)),
            last_output=str(data.get("lastOutput") or data.get("last_output") or ""),
            blocked_reason=str(data.get("blockedReason") or data.get("blocked_reason") or ""),
            created_at=int(data.get("createdAt") or data.get("created_at") or 0),
            updated_at=int(data.get("updatedAt") or data.get("updated_at") or 0),
        )

    def to_dict(self) -> dict:
        return {
            "cwd": self.cwd,
            "sessionId": self.session_id,
            "status": self.status,
            "nextAttemptAt": self.next_attempt_at,
            "attempts": self.attempts,
            "lastOutput": self.last_output,
            "blockedReason": self.blocked_reason,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


def classify_output(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in LIMIT_MARKERS):
        return "limited"
    for line in text.splitlines():
        normalized = line.strip().upper().lstrip("*_` ")
        if normalized.startswith("STATUS: DONE"):
            return "done"
        if normalized == "DONE" or normalized.startswith(("DONE ", "DONE.", "DONE!", "DONE:", "DONE-", "DONE*")):
            return "done"
    return "unknown"


def queue_lock_path(queue: Path) -> Path:
    queue = Path(queue).expanduser()
    return queue.with_name(queue.name + ".lock")


@contextmanager
def queue_lock(queue: Path, timeout_seconds: int = 10):
    lock_path = queue_lock_path(queue)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        if fcntl is None:
            yield
            return
        deadline = time.time() + timeout_seconds
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise TimeoutError(f"timed out waiting for queue lock {lock_path}")
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_queue(path: Path) -> list[QueueItem]:
    path = Path(path).expanduser()
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return []
    items: list[QueueItem] = []
    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            items.append(QueueItem.from_dict(json.loads(stripped)))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid queue line {line_no} in {path}: {exc}") from exc
    return items


def write_queue(path: Path, items: list[QueueItem]) -> None:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(item.to_dict(), sort_keys=True) + "\n" for item in items)
    path.write_text(body)


def append_queue_item(path: Path, item: QueueItem) -> None:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(item.to_dict(), sort_keys=True) + "\n")


def is_due(item: QueueItem, now: int | None = None) -> bool:
    now = int(time.time() if now is None else now)
    return item.status == "pending" and int(item.next_attempt_at or 0) <= now


def truncate_output(text: str, max_chars: int = 2000) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def update_item_after_output(
    item: QueueItem,
    output: str,
    now: int,
    retry_delay_seconds: int,
    followup_delay_seconds: int,
    max_attempts: int,
) -> QueueItem:
    verdict = classify_output(output)
    item.attempts += 1
    item.updated_at = now
    item.last_output = truncate_output(output)
    if verdict == "done":
        item.status = "done"
        item.next_attempt_at = 0
        item.blocked_reason = ""
    elif int(max_attempts) > 0 and item.attempts >= int(max_attempts):
        item.status = "blocked"
        item.next_attempt_at = 0
        item.blocked_reason = f"max attempts ({max_attempts}) reached after {verdict} output"
    elif verdict == "limited":
        item.status = "pending"
        item.next_attempt_at = now + int(retry_delay_seconds)
    else:
        item.status = "pending"
        item.next_attempt_at = now + int(followup_delay_seconds)
    return item


def one_line(text: str, max_chars: int = 120) -> str:
    text = " ".join((text or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."
