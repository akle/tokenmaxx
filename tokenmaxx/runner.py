from __future__ import annotations

import os
import shlex
import signal
import subprocess

from .queue import QueueItem, is_due, update_item_after_output


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


def run_resume_command(
    command: list[str],
    *,
    cwd: str,
    timeout_seconds: int,
    provider_name: str,
    on_process_start=None,
) -> tuple[int, str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        if on_process_start is not None:
            on_process_start(process.pid)
        stdout, stderr = process.communicate(timeout=timeout_seconds if timeout_seconds > 0 else None)
    except subprocess.TimeoutExpired as exc:
        terminate_process_group(process)
        partial = "\n".join(
            part.decode(errors="replace") if isinstance(part, bytes) else part
            for part in (exc.stdout, exc.stderr)
            if part
        )
        message = f"tokenmaxx: {provider_name} resume timed out after {timeout_seconds} seconds"
        return 124, "\n".join(part for part in (partial, message) if part)
    except BaseException:
        try:
            terminate_process_group(process)
        except BaseException:
            pass
        raise
    return process.returncode or 0, "\n".join(part for part in (stdout, stderr) if part)


def run_due_command(
    item: QueueItem,
    command: list[str],
    *,
    provider_name: str,
    now: int,
    dry_run: bool,
    retry_delay_seconds: int,
    followup_delay_seconds: int,
    max_attempts: int,
    resume_timeout_seconds: int,
    on_process_start=None,
) -> QueueItem:
    if not is_due(item, now):
        return item
    if dry_run:
        item.last_output = dry_run_output(command)
        item.updated_at = now
        return item
    returncode, output = run_resume_command(
        command,
        cwd=item.cwd,
        timeout_seconds=resume_timeout_seconds,
        provider_name=provider_name,
        on_process_start=on_process_start,
    )
    if returncode != 0 and not output:
        output = f"{provider_name} exited with code {returncode}"
    return update_item_after_output(
        item,
        output,
        now=now,
        retry_delay_seconds=retry_delay_seconds,
        followup_delay_seconds=followup_delay_seconds,
        max_attempts=max_attempts,
    )
