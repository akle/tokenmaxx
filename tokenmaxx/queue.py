from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import fcntl
except ImportError:  # pragma: no cover - macOS/Linux path is covered.
    fcntl = None


LIMIT_MARKERS = (
    "usage limit",
    "credit limit",
    "out of credits",
    "out of usage credits",
    "ran out of credits",
    "rate limit",
    "rate limited",
    "temporarily limiting requests",
    "session limit",
    "hit your limit",
    "limit reached",
    "try again later",
)

NON_RETRYABLE_MARKERS = (
    "prompt is too long",
    "context is too long",
    "context length exceeded",
    "context window exceeded",
)

TEMPORARY_LIMIT_MARKERS = (
    "not your usage limit",
    "temporarily limiting requests",
)

RESET_BUFFER_SECONDS = 60
SUPPORTED_PROVIDERS = ("claude", "codex")


@dataclass
class QueueItem:
    cwd: str
    session_id: str
    provider: str = "claude"
    status: str = "pending"
    next_attempt_at: int = 0
    attempts: int = 0
    last_output: str = ""
    blocked_reason: str = ""
    created_at: int = 0
    updated_at: int = 0
    lease_id: str = ""

    def __post_init__(self) -> None:
        if self.provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"unsupported provider: {self.provider}")
        now = int(time.time())
        self.next_attempt_at = int(self.next_attempt_at or 0)
        self.attempts = int(self.attempts or 0)
        self.created_at = int(self.created_at or now)
        self.updated_at = int(self.updated_at or self.created_at)
        self.lease_id = str(self.lease_id or "")

    @classmethod
    def from_dict(cls, data: dict) -> "QueueItem":
        return cls(
            cwd=str(data["cwd"]),
            session_id=str(data.get("sessionId") or data.get("session_id")),
            provider=str(data.get("provider", "claude")),
            status=str(data.get("status", "pending")),
            next_attempt_at=int(data.get("nextAttemptAt") or data.get("next_attempt_at") or 0),
            attempts=int(data.get("attempts", 0)),
            last_output=str(data.get("lastOutput") or data.get("last_output") or ""),
            blocked_reason=str(data.get("blockedReason") or data.get("blocked_reason") or ""),
            created_at=int(data.get("createdAt") or data.get("created_at") or 0),
            updated_at=int(data.get("updatedAt") or data.get("updated_at") or 0),
            lease_id=str(data.get("leaseId") or data.get("lease_id") or ""),
        )

    def to_dict(self) -> dict:
        return {
            "cwd": self.cwd,
            "sessionId": self.session_id,
            "provider": self.provider,
            "status": self.status,
            "nextAttemptAt": self.next_attempt_at,
            "attempts": self.attempts,
            "lastOutput": self.last_output,
            "blockedReason": self.blocked_reason,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "leaseId": self.lease_id,
        }

    @property
    def key(self) -> tuple[str, str]:
        return self.provider, self.session_id


def classify_output(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in NON_RETRYABLE_MARKERS):
        return "blocked"
    # DONE wins over limit markers: a completed resume often *mentions* limits
    # in its summary, while a genuinely limited attempt never emits a DONE line.
    for line in text.splitlines():
        normalized = line.strip().upper().lstrip("*_` ")
        if normalized.startswith("STATUS: DONE"):
            return "done"
        if normalized == "DONE" or normalized.startswith(("DONE ", "DONE.", "DONE!", "DONE:", "DONE-", "DONE*")):
            return "done"
    if any(marker in lowered for marker in LIMIT_MARKERS):
        return "limited"
    return "unknown"


def is_temporary_limit(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in TEMPORARY_LIMIT_MARKERS)


MONTHS_BY_PREFIX = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Weekly limits reset within 7 days; a parsed reset further out than this is
# quoted/stale text in the output, not a real banner.
RESET_MAX_HORIZON_SECONDS = 8 * 86_400


def reset_time_from_output(text: str, now: int) -> int | None:
    # Dated banners use two separators: "resets Apr 23, 2pm" and
    # "resets May 12 at 2pm".
    match = re.search(
        r"(?:resets?|try\s+again\s+at)\s+(?:([A-Za-z]{3,9})\s+(\d{1,2})(?:,\s*|\s+at\s+))?(\d{1,2})(?::(\d{2}))?\s*(am|pm)(?:\s*\(([^)]+)\))?",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    month = MONTHS_BY_PREFIX.get((match.group(1) or "")[:3].lower())
    day = int(match.group(2)) if month and match.group(2) else None
    hour = int(match.group(3))
    minute = int(match.group(4) or 0)
    meridiem = match.group(5).lower()
    if hour < 1 or hour > 12 or minute > 59:
        return None
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0

    timezone_name = match.group(6)
    try:
        timezone = ZoneInfo(timezone_name) if timezone_name else datetime.fromtimestamp(now).astimezone().tzinfo
    except ZoneInfoNotFoundError:
        timezone = datetime.fromtimestamp(now).astimezone().tzinfo
    current = datetime.fromtimestamp(now, timezone)
    try:
        if month and day:
            candidate = current.replace(month=month, day=day, hour=hour, minute=minute, second=0, microsecond=0)
        else:
            candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        candidate += timedelta(seconds=RESET_BUFFER_SECONDS)
        if int(candidate.timestamp()) <= now:
            if month and day:
                candidate = candidate.replace(year=candidate.year + 1)
            else:
                candidate += timedelta(days=1)
    except ValueError:
        # Matched text carried an impossible date (e.g. "Feb 29" rolled into a
        # non-leap year); fall back to the plain retry delay.
        return None
    if int(candidate.timestamp()) - now > RESET_MAX_HORIZON_SECONDS:
        return None
    return int(candidate.timestamp())


def queue_lock_path(queue: Path) -> Path:
    queue = Path(queue).expanduser()
    return queue.with_name(queue.name + ".lock")


def resume_lock_path(queue: Path) -> Path:
    queue = Path(queue).expanduser()
    return queue.with_name(queue.name + ".resume.lock")


@contextmanager
def resume_lock(queue: Path):
    lock_path = resume_lock_path(queue)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        if fcntl is None:
            yield True
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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


def defer_item(item: QueueItem, now: int, delay_seconds: int, reason: str) -> QueueItem:
    item.updated_at = now
    item.next_attempt_at = now + int(delay_seconds)
    item.last_output = reason
    return item


def merge_resumed_item(items: list[QueueItem], updated: QueueItem) -> None:
    """Fold the result of an out-of-lock resume back into a reloaded queue.

    The result lands on the first still-pending row for the provider/session — the row
    that carried the same lease. Provider/session matching keeps identically named
    sessions isolated, while still skipping a blocked tombstone when the same
    provider session was re-added after a drop. No matching pending row means the
    item was resolved or re-armed mid-resume; that newer decision wins and the
    resume result is discarded.
    """
    for index, existing in enumerate(items):
        if (
            existing.key == updated.key
            and existing.status == "pending"
            and existing.lease_id == updated.lease_id
        ):
            items[index] = updated
            return


def apply_limit_event(
    items: list[QueueItem],
    *,
    provider: str,
    session_id: str,
    cwd: str,
    hit_at: int,
    now: int,
) -> QueueItem | None:
    key = (provider, session_id)
    existing = next((item for item in reversed(items) if item.key == key), None)
    if existing is not None and (
        existing.status == "pending" or existing.blocked_reason == "dropped by user"
    ):
        return None
    if existing is None:
        item = QueueItem(cwd=cwd, session_id=session_id, provider=provider)
        items.append(item)
        return item
    if hit_at <= existing.updated_at:
        return None
    existing.status = "pending"
    existing.attempts = 0
    existing.next_attempt_at = 0
    existing.blocked_reason = ""
    existing.last_output = ""
    existing.updated_at = now
    existing.lease_id = ""
    return existing


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
    elif verdict == "blocked":
        item.status = "blocked"
        item.next_attempt_at = 0
        item.blocked_reason = "not retryable: " + one_line(output)
    elif int(max_attempts) > 0 and item.attempts >= int(max_attempts):
        item.status = "blocked"
        item.next_attempt_at = 0
        item.blocked_reason = f"max attempts ({max_attempts}) reached after {verdict} output"
    elif verdict == "limited":
        item.status = "pending"
        reset_at = reset_time_from_output(output, now)
        if reset_at is not None:
            item.next_attempt_at = reset_at
        elif is_temporary_limit(output):
            item.next_attempt_at = now + int(followup_delay_seconds)
        else:
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
