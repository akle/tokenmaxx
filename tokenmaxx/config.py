from __future__ import annotations

import os
from pathlib import Path


DEFAULT_RETRY_DELAY_SECONDS = 5 * 60 * 60
DEFAULT_FOLLOWUP_DELAY_SECONDS = 15 * 60
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_MAX_SESSION_AGE_HOURS = 24.0
DEFAULT_RESUME_TIMEOUT_SECONDS = 4 * 60 * 60
DEFAULT_ACTIVE_GRACE_SECONDS = 30 * 60

DEFAULT_PROMPT = """Continue this Claude Code session only if unfinished.

First inspect the current repo/session state and decide whether work remains.
If the prior task is already complete, say DONE and stop.
If it hit a usage/rate/session limit or a transient provider connection failure before finishing, resume the remaining work.
Before long work, write or update a checkpoint with completed work and next steps.
Keep the response concise and operational."""

CODEX_PROMPT = """Continue this Codex session only if unfinished.

First inspect the current repo/session state and decide whether work remains.
If the prior task is already complete, respond with exactly STATUS: DONE and stop.
If it hit a usage limit or a transient remote compaction/transport failure before finishing, resume the remaining work.
Do not change or bypass the configured sandbox or approval settings.
When the task is complete, end with exactly STATUS: DONE."""


def default_home() -> Path:
    return Path(os.environ.get("TOKENMAXX_HOME", Path.home() / ".tokenmaxx")).expanduser()


def default_queue_path() -> Path:
    return default_home() / "queue.jsonl"


def default_sessions_dir() -> Path:
    return Path.home() / ".claude" / "sessions"


def default_codex_sessions_dir() -> Path:
    return Path.home() / ".codex" / "sessions"


def default_codex_history_file() -> Path:
    return Path.home() / ".codex" / "history.jsonl"


def default_codex_logs_db() -> Path:
    return Path.home() / ".codex" / "logs_2.sqlite"


def default_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def default_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.local.tokenmaxx.plist"


def default_log_path() -> Path:
    return default_home() / "tokenmaxx.log"
