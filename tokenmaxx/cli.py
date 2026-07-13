from __future__ import annotations

import argparse
from contextlib import nullcontext
import dataclasses
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import __version__, claude, codex
from .config import (
    DEFAULT_ACTIVE_GRACE_SECONDS,
    DEFAULT_FOLLOWUP_DELAY_SECONDS,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_SESSION_AGE_HOURS,
    DEFAULT_RETRY_DELAY_SECONDS,
    DEFAULT_RESUME_TIMEOUT_SECONDS,
    default_codex_sessions_dir,
    default_log_path,
    default_plist_path,
    default_projects_dir,
    default_queue_path,
    default_sessions_dir,
)
from .launchd import (
    LABEL,
    build_launchd_plist,
    launchctl_load,
    launchctl_unload,
    launchd_state,
)
from .queue import (
    QueueItem,
    defer_item,
    is_due,
    load_queue,
    merge_resumed_item,
    one_line,
    queue_lock,
    resume_lock,
    write_queue,
)


def load_provider_sessions(args, provider: str, now: int) -> list[dict]:
    if provider == "claude":
        return claude.load_claude_sessions(args.sessions_dir)
    if provider == "codex":
        return codex.load_codex_sessions(
            args.codex_sessions_dir,
            now=now,
            max_session_age_hours=args.max_session_age_hours,
        )
    raise ValueError(f"unsupported provider: {provider}")


def find_session(args) -> dict | None:
    provider = args.provider or "claude"
    now = int(args.now or time.time())
    sessions = load_provider_sessions(args, provider, now)
    for session in sessions:
        if args.pid is not None and int(session.get("pid", -1)) == int(args.pid):
            return session
        if args.session_id and session.get("sessionId") == args.session_id:
            return session
    return None


def format_time(epoch: int) -> str:
    if not epoch:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def log_line(message: str) -> None:
    # flush=True: the daemon's stdout goes to a file, where Python block-buffers
    # and `tokenmaxx logs` would lag hours behind the queue otherwise.
    print(f"[{format_time(int(time.time()))}] {message}", flush=True)


def print_sessions(sessions: list[dict]) -> None:
    print("PID     STATUS  UPDATED              CWD                                           PROVIDER SESSION")
    for session in sessions:
        pid = str(session.get("pid", "-"))[:7].ljust(7)
        status = str(session.get("status") or "-")[:7].ljust(7)
        updated_ms = int(session.get("updatedAt") or 0)
        updated = format_time(updated_ms // 1000 if updated_ms else 0)
        cwd = str(session.get("cwd", "-"))
        if len(cwd) > 43:
            cwd = "..." + cwd[-40:]
        print(
            f"{pid} {status} {updated:<19} {cwd:<45} "
            f"{session.get('_provider', 'claude'):<8} {session.get('sessionId')}"
        )


def cmd_scan(args) -> int:
    now = int(getattr(args, "now", 0) or time.time())
    sessions = []
    for provider in ("claude", "codex"):
        sessions.extend(dict(session, _provider=provider) for session in load_provider_sessions(args, provider, now))
    print_sessions(sessions)
    return 0


def cmd_add(args) -> int:
    provider = args.provider or "claude"
    session = find_session(args)
    if not session:
        print(f"No matching {provider.title()} session found.", file=sys.stderr)
        return 1
    item = QueueItem(cwd=args.cwd or session["cwd"], session_id=session["sessionId"], provider=provider)
    with queue_lock(args.queue, args.lock_timeout_seconds):
        items = load_queue(args.queue)
        matches = [existing for existing in items if existing.key == item.key]
        if matches:
            existing = next((row for row in matches if row.status == "pending"), matches[0])
            items = [row for row in items if row.key != item.key or row is existing]
            if existing.status != "pending":
                existing.cwd = item.cwd
                existing.status = "pending"
                existing.next_attempt_at = 0
                existing.attempts = 0
                existing.last_output = ""
                existing.blocked_reason = ""
                existing.updated_at = item.updated_at
                existing.lease_id = ""
        else:
            items.append(item)
        write_queue(args.queue, items)
    identity = item.session_id if item.provider == "claude" else f"{item.provider}:{item.session_id}"
    print(f"Queued {identity} in {item.cwd}")
    return 0


def display_path(path) -> str:
    text = str(path)
    home = str(Path.home())
    if text.startswith(home + "/") or text == home:
        return "~" + text[len(home):]
    return text


def truncate_left(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return "…" + text[-(width - 1):]


def truncate_right(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


STATUS_ORDER = {"pending": 0, "blocked": 1, "done": 2}


def summarize_queue(items: list[QueueItem], queue: Path) -> None:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    print(f"Queue:  {display_path(queue)}")
    print(
        f"{counts.get('pending', 0)} pending · {counts.get('done', 0)} done · "
        f"{counts.get('blocked', 0)} blocked · {len(items)} total"
    )
    if not items:
        return
    print()

    # Pending first (soonest attempt on top), then blocked, then done.
    rows = sorted(
        items,
        key=lambda item: (
            STATUS_ORDER.get(item.status, 3),
            item.next_attempt_at if item.status == "pending" else 0,
            -item.updated_at,
        ),
    )
    directories = [truncate_left(display_path(item.cwd), 36) for item in rows]
    dir_width = max(len(text) for text in directories + ["DIRECTORY"])
    template = f"{{:<8}} {{:>3}}  {{:<16}}  {{:<8}}  {{:<8}}  {{:<{dir_width}}}  {{}}"
    term_width = shutil.get_terminal_size(fallback=(120, 24)).columns
    last_width = max(20, term_width - (8 + 1 + 3 + 2 + 16 + 2 + 8 + 2 + 8 + 2 + dir_width + 2))

    print(template.format("STATUS", "ATT", "NEXT", "PROVIDER", "SESSION", "DIRECTORY", "LAST"))
    for item, directory in zip(rows, directories):
        if is_due(item):
            next_attempt = "due now"
        elif item.next_attempt_at:
            next_attempt = time.strftime("%Y-%m-%d %H:%M", time.localtime(item.next_attempt_at))
        else:
            next_attempt = "-"
        detail = item.blocked_reason or one_line(item.last_output)
        print(
            template.format(
                item.status,
                item.attempts,
                next_attempt,
                item.provider,
                item.session_id[:8],
                directory,
                truncate_right(detail, last_width),
            )
        )


def cmd_status(args) -> int:
    print_daemon_state(args)
    summarize_queue(load_queue(args.queue), args.queue)
    return 0


def print_daemon_state(args) -> None:
    state = launchd_state(args.plist_path)
    loaded = "unknown" if state.loaded is None else ("loaded" if state.loaded else "not loaded")
    installed = "installed" if state.installed else "not installed"
    print(f"Daemon: {loaded} ({installed})")
    print(f"Plist:  {args.plist_path}")
    print(f"Log:    {args.log_path}")


def autoqueue_limited_sessions(
    args,
    items: list[QueueItem],
    now: int,
    sessions: list[dict],
    provider: str = "claude",
) -> list[QueueItem]:
    if provider == "claude":
        return claude.build_limited_queue_items(
            sessions,
            items,
            projects_dir=args.projects_dir,
            now=now,
            max_session_age_hours=args.max_session_age_hours,
        )
    if provider == "codex":
        return codex.build_limited_queue_items(
            sessions,
            items,
            now=now,
            max_session_age_hours=args.max_session_age_hours,
        )
    raise ValueError(f"unsupported provider: {provider}")


def autoqueued_message(count: int) -> str:
    noun = "session" if count == 1 else "sessions"
    return f"Auto-queued {count} {noun}."


def cmd_autoqueue(args) -> int:
    now = int(args.now or time.time())
    with queue_lock(args.queue, args.lock_timeout_seconds):
        items = load_queue(args.queue)
        queued = []
        for provider in ("claude", "codex"):
            sessions = load_provider_sessions(args, provider, now)
            queued.extend(autoqueue_limited_sessions(args, items, now, sessions, provider))
        if queued:
            write_queue(args.queue, items)
    print(autoqueued_message(len(queued)))
    return 0


def cmd_drop(args) -> int:
    # Tombstone instead of deleting: auto-queue dedupes against session ids
    # still present in the queue, so a deleted row would be re-queued (and
    # resumed) on the daemon's next cycle while the transcript still ends on
    # a limit banner.
    now = int(time.time())
    with queue_lock(args.queue, args.lock_timeout_seconds):
        items = load_queue(args.queue)
        matches = sorted(
            {
                item.key
                for item in items
                if item.session_id.startswith(args.session_id)
                and (args.provider is None or item.provider == args.provider)
            }
        )
        if not matches:
            print(f"No queued item for session {args.session_id}.", file=sys.stderr)
            return 1
        if len(matches) > 1:
            qualified = ", ".join(f"{provider}:{session_id}" for provider, session_id in matches)
            print(f"Ambiguous session prefix {args.session_id}: matches {qualified}.", file=sys.stderr)
            return 1
        provider, session_id = matches[0]
        for item in items:
            if item.key == (provider, session_id):
                item.status = "blocked"
                item.blocked_reason = "dropped by user"
                item.next_attempt_at = 0
                item.updated_at = now
                item.lease_id = ""
        write_queue(args.queue, items)
    identity = session_id if args.provider is None and provider == "claude" else f"{provider}:{session_id}"
    print(f"Dropped {identity}. Kept as a blocked tombstone so auto-queue will not re-add it.")
    return 0


def resolve_provider_bin(provider: str, requested: str | None = None) -> str | None:
    if provider not in {"claude", "codex"}:
        raise ValueError(f"unsupported provider: {provider}")
    candidate = requested or provider
    expanded = Path(candidate).expanduser()
    return shutil.which(str(expanded) if expanded.is_absolute() else candidate)


def enabled_provider_bins(args) -> dict[str, str]:
    bins = {}
    for provider in ("claude", "codex"):
        resolved = resolve_provider_bin(provider, getattr(args, f"{provider}_bin", None))
        if resolved:
            bins[provider] = resolved
    return bins


def run_resume(args, item: QueueItem, now: int, bins: dict[str, str]) -> QueueItem:
    common = dict(
        now=now,
        dry_run=args.dry_run,
        retry_delay_seconds=args.retry_delay_seconds,
        followup_delay_seconds=args.followup_delay_seconds,
        max_attempts=args.max_attempts,
        resume_timeout_seconds=args.resume_timeout_seconds,
    )
    if item.provider == "claude":
        return claude.run_due_item(item, claude_bin=bins["claude"], **common)
    if item.provider == "codex":
        return codex.run_due_item(item, codex_bin=bins["codex"], **common)
    raise ValueError(f"unsupported provider: {item.provider}")


def find_active_provider_session(args, item: QueueItem, now: int) -> dict | None:
    sessions = load_provider_sessions(args, item.provider, now)
    if item.provider == "claude":
        return claude.find_active_session(sessions, item.session_id, now, DEFAULT_ACTIVE_GRACE_SECONDS)
    if item.provider == "codex":
        return codex.find_active_session(sessions, item.session_id, now, DEFAULT_ACTIVE_GRACE_SECONDS)
    raise ValueError(f"unsupported provider: {item.provider}")


def run_watch_cycle(args, bins: dict[str, str]) -> bool:
    now = int(args.now or time.time())
    resume_item = None
    processed = False
    with queue_lock(args.queue, args.lock_timeout_seconds):
        items = load_queue(args.queue)
        dirty = False
        if args.auto_queue:
            queued = []
            for provider in bins:
                sessions = load_provider_sessions(args, provider, now)
                queued.extend(autoqueue_limited_sessions(args, items, now, sessions, provider))
            if queued:
                dirty = True
                log_line(autoqueued_message(len(queued)))
        for index, item in enumerate(items):
            if not is_due(item, now):
                continue
            identity = f"{item.provider}:{item.session_id[:8]}"
            if item.provider not in bins:
                reason = f"{item.provider} executable unavailable"
                if args.dry_run:
                    log_line(f"Would defer {identity}: {reason}.")
                else:
                    items[index] = defer_item(item, now, args.followup_delay_seconds, reason)
                    dirty = True
                    log_line(f"Deferred {identity}: {reason}.")
                continue
            # Fresh session snapshot per due item: the auto-queue transcript
            # scan above can take seconds, and the guard should act on data
            # milliseconds old. The residual check-to-resume window is
            # irreducible in a poll-based design.
            owner = find_active_provider_session(args, item, now)
            if owner is not None:
                pid = owner.get("pid")
                reason = f"session active in pid {pid}" if pid is not None else "session active"
                if args.dry_run:
                    log_line(f"Would defer {identity}: {reason}.")
                else:
                    items[index] = defer_item(item, now, args.followup_delay_seconds, reason)
                    dirty = True
                    log_line(f"Deferred {identity}: {reason}.")
                continue
            if args.dry_run:
                items[index] = run_resume(args, item, now, bins)
                dirty = True
                if items[index].last_output:
                    log_line(f"{identity}: {items[index].last_output}")
                processed = True
                break
            # Claim with a lease and resume OUTSIDE the queue lock, so status,
            # add, and drop stay usable during a resume that can run for hours.
            # The queue-scoped resume lock still prevents another watcher from
            # claiming a different provider row concurrently.
            item.lease_id = uuid.uuid4().hex
            resume_item = dataclasses.replace(item)
            lease_seconds = args.resume_timeout_seconds if args.resume_timeout_seconds > 0 else 86_400
            item.next_attempt_at = now + lease_seconds + 300
            item.updated_at = now
            dirty = True
            processed = True
            break
        if dirty:
            write_queue(args.queue, items)
    if resume_item is not None:
        updated = run_resume(args, resume_item, now, bins)
        if updated.last_output:
            log_line(f"{updated.provider}:{updated.session_id[:8]}: {updated.last_output}")
        with queue_lock(args.queue, args.lock_timeout_seconds):
            items = load_queue(args.queue)
            merge_resumed_item(items, updated)
            write_queue(args.queue, items)
    return processed


def cmd_watch(args) -> int:
    bins = enabled_provider_bins(args)
    if not bins:
        print(
            "No Claude or Codex executable found. Pass --claude-bin or --codex-bin.",
            file=sys.stderr,
        )
        return 1
    if not args.once:
        # One line per daemon start so the log always answers "is it running,
        # and which build" even when no queue events ever fire.
        log_line(f"tokenmaxx {__version__} watching {args.queue} every {args.sleep_seconds}s")
    while True:
        guard = nullcontext(True) if args.dry_run else resume_lock(args.queue)
        with guard as acquired:
            if not acquired:
                log_line("Resume already in progress; skipping watch cycle.")
                return 0
            processed = run_watch_cycle(args, bins)
        if args.once:
            if not processed:
                print("No due items.")
            return 0
        time.sleep(args.sleep_seconds)


def cmd_launchd_install(args) -> int:
    program = args.program or resolve_default_program()
    if not program:
        print("tokenmaxx is not on PATH. Pass --program /absolute/path/to/tokenmaxx.", file=sys.stderr)
        return 1
    claude_bin = resolve_provider_bin("claude", args.claude_bin)
    codex_bin = resolve_provider_bin("codex", args.codex_bin)
    if not claude_bin and not codex_bin:
        print(
            "Neither claude nor codex is on PATH. Pass --claude-bin or --codex-bin with an absolute path.",
            file=sys.stderr,
        )
        return 1
    queue_path = args.queue.expanduser()
    log_path = args.log_path.expanduser()
    plist_path = args.plist_path.expanduser()
    plist = build_launchd_plist(
        program=program,
        claude_bin=claude_bin,
        codex_bin=codex_bin,
        queue_path=queue_path,
        log_path=log_path,
        interval_seconds=args.interval_seconds,
        sessions_dir=args.sessions_dir,
        codex_sessions_dir=args.codex_sessions_dir,
        projects_dir=args.projects_dir,
        lock_timeout_seconds=args.lock_timeout_seconds,
        path_env=os.environ.get("PATH"),
    )
    if args.dry_run:
        print(plist)
        return 0
    log_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist)
    print(f"Wrote {plist_path}")
    print("Not loaded. Run `launchctl load <plist>` yourself after reviewing it.")
    return 0


def resolve_default_program() -> str | None:
    return shutil.which("tokenmaxx")


def resolve_default_claude_bin(claude_bin: str | None = None) -> str | None:
    candidate = claude_bin or "claude"
    expanded = Path(candidate).expanduser()
    if expanded.is_absolute():
        return str(expanded)
    return shutil.which(candidate)


def cmd_start(args) -> int:
    program = args.program or resolve_default_program()
    if not program:
        print(
            "tokenmaxx is not on PATH. Run `pipx install .` or pass --program /absolute/path/to/tokenmaxx.",
            file=sys.stderr,
        )
        return 1
    claude_bin = resolve_provider_bin("claude", args.claude_bin)
    codex_bin = resolve_provider_bin("codex", args.codex_bin)
    if not claude_bin and not codex_bin:
        print(
            "Neither claude nor codex is on PATH. Pass --claude-bin or --codex-bin with an absolute path.",
            file=sys.stderr,
        )
        return 1

    queue_path = args.queue.expanduser()
    log_path = args.log_path.expanduser()
    plist_path = args.plist_path.expanduser()
    plist = build_launchd_plist(
        program=program,
        claude_bin=claude_bin,
        codex_bin=codex_bin,
        queue_path=queue_path,
        log_path=log_path,
        interval_seconds=args.interval_seconds,
        sessions_dir=args.sessions_dir,
        codex_sessions_dir=args.codex_sessions_dir,
        projects_dir=args.projects_dir,
        lock_timeout_seconds=args.lock_timeout_seconds,
        path_env=os.environ.get("PATH"),
    )
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    previous_plist = plist_path.read_text(errors="replace") if plist_path.exists() else None
    plist_path.write_text(plist)

    state = launchd_state(plist_path)
    if state.loaded:
        print(f"{LABEL} is already loaded.")
        if previous_plist != plist:
            print("Plist changed. Run `tokenmaxx stop` then `tokenmaxx start` to apply updated launchd arguments.")
        print(f"Log: {log_path}")
        return 0

    result = launchctl_load(plist_path)
    if result.returncode != 0:
        print((result.stderr or result.stdout or "launchctl load failed").strip(), file=sys.stderr)
        return result.returncode

    print(f"Started {LABEL}")
    print(f"Log: {log_path}")
    return 0


def cmd_stop(args) -> int:
    plist_path = args.plist_path.expanduser()
    state = launchd_state(plist_path)
    if state.loaded is False:
        print(f"{LABEL} is not loaded.")
        return 0
    if not plist_path.exists():
        print(f"No plist at {plist_path}", file=sys.stderr)
        return 1

    result = launchctl_unload(plist_path)
    if result.returncode != 0:
        print((result.stderr or result.stdout or "launchctl unload failed").strip(), file=sys.stderr)
        return result.returncode

    print(f"Stopped {LABEL}")
    return 0


def cmd_logs(args) -> int:
    log_path = args.log_path.expanduser()
    if not log_path.exists():
        print(f"No log at {log_path}")
        return 0
    if args.follow:
        return subprocess.call(["tail", "-f", str(log_path)])

    lines = log_path.read_text(errors="replace").splitlines()
    selected = lines[-args.lines :] if args.lines > 0 else []
    if selected:
        print("\n".join(selected))
    return 0


def cmd_launchd_uninstall(args) -> int:
    plist_path = args.plist_path.expanduser()
    if args.dry_run:
        print(f"Would remove {plist_path}")
        return 0
    if plist_path.exists():
        plist_path.unlink()
        print(f"Removed {plist_path}")
    else:
        print(f"No plist at {plist_path}")
    print("Not unloaded. Run `launchctl unload <plist>` first if it is currently loaded.")
    return 0


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--queue", type=Path, default=default_queue_path())
    parser.add_argument("--sessions-dir", type=Path, default=default_sessions_dir())
    parser.add_argument("--codex-sessions-dir", type=Path, default=default_codex_sessions_dir())
    parser.add_argument("--projects-dir", type=Path, default=default_projects_dir())
    parser.add_argument("--lock-timeout-seconds", type=int, default=10)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Queue and resume coding sessions after provider limit windows reset.")
    parser.add_argument("--version", action="version", version=f"tokenmaxx {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="list local Claude Code and Codex sessions")
    add_common_args(scan)
    scan.add_argument("--max-session-age-hours", type=float, default=DEFAULT_MAX_SESSION_AGE_HOURS)
    scan.set_defaults(func=cmd_scan)

    add = subparsers.add_parser("add", help="add a session to the retry queue")
    add_common_args(add)
    add.add_argument("--provider", choices=("claude", "codex"), default="claude")
    add.add_argument("--max-session-age-hours", type=float, default=DEFAULT_MAX_SESSION_AGE_HOURS)
    add.add_argument("--now", type=int, default=0, help=argparse.SUPPRESS)
    selector = add.add_mutually_exclusive_group(required=True)
    selector.add_argument("--pid", type=int)
    selector.add_argument("--session-id")
    add.add_argument("--cwd", help="override working directory for resume")
    add.set_defaults(func=cmd_add)

    status = subparsers.add_parser("status", help="show tokenmaxx queue status")
    add_common_args(status)
    status.add_argument("--plist-path", type=Path, default=default_plist_path())
    status.add_argument("--log-path", type=Path, default=default_log_path())
    status.set_defaults(func=cmd_status)

    drop = subparsers.add_parser(
        "drop",
        help="stop retrying a session (keeps a blocked tombstone so auto-queue will not re-add it)",
    )
    add_common_args(drop)
    drop.add_argument("--session-id", required=True)
    drop.add_argument("--provider", choices=("claude", "codex"))
    drop.set_defaults(func=cmd_drop)

    autoqueue = subparsers.add_parser("autoqueue", help="queue recent sessions whose transcripts show a limit error")
    add_common_args(autoqueue)
    autoqueue.add_argument("--max-session-age-hours", type=float, default=DEFAULT_MAX_SESSION_AGE_HOURS)
    autoqueue.add_argument("--now", type=int, default=0, help=argparse.SUPPRESS)
    autoqueue.set_defaults(func=cmd_autoqueue)

    watch = subparsers.add_parser("watch", help="resume due sessions")
    add_common_args(watch)
    watch.add_argument("--claude-bin", default=None)
    watch.add_argument("--codex-bin", default=None)
    watch.add_argument("--retry-delay-seconds", type=int, default=DEFAULT_RETRY_DELAY_SECONDS)
    watch.add_argument("--followup-delay-seconds", type=int, default=DEFAULT_FOLLOWUP_DELAY_SECONDS)
    watch.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    watch.add_argument("--resume-timeout-seconds", type=int, default=DEFAULT_RESUME_TIMEOUT_SECONDS)
    watch.add_argument("--sleep-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--dry-run", action="store_true")
    watch.add_argument("--max-session-age-hours", type=float, default=DEFAULT_MAX_SESSION_AGE_HOURS)
    watch.add_argument("--no-auto-queue", dest="auto_queue", action="store_false")
    watch.set_defaults(auto_queue=True)
    watch.add_argument("--now", type=int, default=0, help=argparse.SUPPRESS)
    watch.set_defaults(func=cmd_watch)

    start = subparsers.add_parser("start", help="install and load the macOS launchd background service")
    add_common_args(start)
    start.add_argument("--program", default=None, help="absolute tokenmaxx executable path for launchd")
    start.add_argument("--claude-bin", default=None, help="Claude executable path for launchd")
    start.add_argument("--codex-bin", default=None, help="Codex executable path for launchd")
    start.add_argument("--plist-path", type=Path, default=default_plist_path())
    start.add_argument("--log-path", type=Path, default=default_log_path())
    start.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    start.set_defaults(func=cmd_start)

    stop = subparsers.add_parser("stop", help="unload the macOS launchd background service")
    stop.add_argument("--plist-path", type=Path, default=default_plist_path())
    stop.set_defaults(func=cmd_stop)

    logs = subparsers.add_parser("logs", help="show tokenmaxx log output")
    logs.add_argument("--log-path", type=Path, default=default_log_path())
    logs.add_argument("--lines", type=int, default=80)
    logs.add_argument("--follow", action="store_true")
    logs.set_defaults(func=cmd_logs)

    launchd_install = subparsers.add_parser(
        "launchd-install",
        help="write or print a launchd plist, but do not load it",
    )
    add_common_args(launchd_install)
    launchd_install.add_argument("--program", default=None, help="absolute tokenmaxx executable path for launchd")
    launchd_install.add_argument("--claude-bin", default=None, help="Claude executable path for launchd")
    launchd_install.add_argument("--codex-bin", default=None, help="Codex executable path for launchd")
    launchd_install.add_argument("--plist-path", type=Path, default=default_plist_path())
    launchd_install.add_argument("--log-path", type=Path, default=default_log_path())
    launchd_install.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    launchd_install.add_argument("--dry-run", action="store_true")
    launchd_install.set_defaults(func=cmd_launchd_install)

    launchd_uninstall = subparsers.add_parser(
        "launchd-uninstall",
        help="remove tokenmaxx launchd plist, but do not unload it",
    )
    add_common_args(launchd_uninstall)
    launchd_uninstall.add_argument("--plist-path", type=Path, default=default_plist_path())
    launchd_uninstall.add_argument("--dry-run", action="store_true")
    launchd_uninstall.set_defaults(func=cmd_launchd_uninstall)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except TimeoutError:
        print(
            "Queue is locked by another tokenmaxx process. Retry in a moment, or run `tokenmaxx stop` first.",
            file=sys.stderr,
        )
        return 1
