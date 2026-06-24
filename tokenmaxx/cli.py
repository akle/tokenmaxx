from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

from . import __version__
from .claude import build_limited_queue_items, load_claude_sessions, run_due_item
from .config import (
    DEFAULT_FOLLOWUP_DELAY_SECONDS,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_SESSION_AGE_HOURS,
    DEFAULT_RETRY_DELAY_SECONDS,
    DEFAULT_RESUME_TIMEOUT_SECONDS,
    default_log_path,
    default_plist_path,
    default_projects_dir,
    default_queue_path,
    default_sessions_dir,
)
from .launchd import build_launchd_plist
from .queue import (
    QueueItem,
    append_queue_item,
    is_due,
    load_queue,
    one_line,
    queue_lock,
    write_queue,
)


def find_session(args) -> dict | None:
    sessions = load_claude_sessions(args.sessions_dir)
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


def print_sessions(sessions: list[dict]) -> None:
    print("PID     STATUS  UPDATED              CWD                                           SESSION")
    for session in sessions:
        pid = str(session.get("pid", "-"))[:7].ljust(7)
        status = str(session.get("status") or "-")[:7].ljust(7)
        updated_ms = int(session.get("updatedAt") or 0)
        updated = format_time(updated_ms // 1000 if updated_ms else 0)
        cwd = str(session.get("cwd", "-"))
        if len(cwd) > 43:
            cwd = "..." + cwd[-40:]
        print(f"{pid} {status} {updated:<19} {cwd:<45} {session.get('sessionId')}")


def cmd_scan(args) -> int:
    sessions = load_claude_sessions(args.sessions_dir)
    print_sessions(sessions)
    return 0


def cmd_add(args) -> int:
    session = find_session(args)
    if not session:
        print("No matching Claude session found.", file=sys.stderr)
        return 1
    item = QueueItem(cwd=args.cwd or session["cwd"], session_id=session["sessionId"])
    with queue_lock(args.queue, args.lock_timeout_seconds):
        append_queue_item(args.queue, item)
    print(f"Queued {item.session_id} in {item.cwd}")
    return 0


def summarize_queue(items: list[QueueItem], queue: Path) -> None:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    print(f"Queue: {queue}")
    print(f"pending={counts.get('pending', 0)} done={counts.get('done', 0)} blocked={counts.get('blocked', 0)} total={len(items)}")
    for item in items:
        due = "due" if is_due(item) else f"next={format_time(item.next_attempt_at)}"
        detail = item.blocked_reason or one_line(item.last_output)
        suffix = f" last={detail}" if detail else ""
        print(f"{item.status:<8} attempts={item.attempts:<3} {due:<24} {item.cwd} {item.session_id}{suffix}")


def cmd_status(args) -> int:
    summarize_queue(load_queue(args.queue), args.queue)
    return 0


def autoqueue_limited_sessions(args, items: list[QueueItem], now: int) -> list[QueueItem]:
    sessions = load_claude_sessions(args.sessions_dir)
    queued = build_limited_queue_items(
        sessions,
        items,
        projects_dir=args.projects_dir,
        now=now,
        max_session_age_hours=args.max_session_age_hours,
    )
    if queued:
        items.extend(queued)
    return queued


def print_autoqueued(count: int) -> None:
    noun = "session" if count == 1 else "sessions"
    print(f"Auto-queued {count} {noun}.")


def cmd_autoqueue(args) -> int:
    now = int(args.now or time.time())
    with queue_lock(args.queue, args.lock_timeout_seconds):
        items = load_queue(args.queue)
        queued = autoqueue_limited_sessions(args, items, now)
        if queued:
            write_queue(args.queue, items)
    print_autoqueued(len(queued))
    return 0


def cmd_watch(args) -> int:
    while True:
        now = int(args.now or time.time())
        with queue_lock(args.queue, args.lock_timeout_seconds):
            items = load_queue(args.queue)
            if args.auto_queue:
                queued = autoqueue_limited_sessions(args, items, now)
                if queued:
                    print_autoqueued(len(queued))
            processed = False
            for index, item in enumerate(items):
                if is_due(item, now):
                    items[index] = run_due_item(
                        item,
                        now=now,
                        claude_bin=args.claude_bin,
                        dry_run=args.dry_run,
                        retry_delay_seconds=args.retry_delay_seconds,
                        followup_delay_seconds=args.followup_delay_seconds,
                        max_attempts=args.max_attempts,
                        resume_timeout_seconds=args.resume_timeout_seconds,
                    )
                    write_queue(args.queue, items)
                    if items[index].last_output:
                        print(items[index].last_output)
                    processed = True
                    break
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
    plist = build_launchd_plist(
        program=program,
        queue_path=args.queue,
        log_path=args.log_path,
        interval_seconds=args.interval_seconds,
    )
    if args.dry_run:
        print(plist)
        return 0
    args.plist_path.parent.mkdir(parents=True, exist_ok=True)
    args.plist_path.write_text(plist)
    print(f"Wrote {args.plist_path}")
    print("Not loaded. Run `launchctl load <plist>` yourself after reviewing it.")
    return 0


def resolve_default_program() -> str | None:
    return shutil.which("tokenmaxx")


def cmd_launchd_uninstall(args) -> int:
    if args.dry_run:
        print(f"Would remove {args.plist_path}")
        return 0
    if args.plist_path.exists():
        args.plist_path.unlink()
        print(f"Removed {args.plist_path}")
    else:
        print(f"No plist at {args.plist_path}")
    print("Not unloaded. Run `launchctl unload <plist>` first if it is currently loaded.")
    return 0


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--queue", type=Path, default=default_queue_path())
    parser.add_argument("--sessions-dir", type=Path, default=default_sessions_dir())
    parser.add_argument("--projects-dir", type=Path, default=default_projects_dir())
    parser.add_argument("--lock-timeout-seconds", type=int, default=10)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Queue and resume Claude Code sessions after limit windows reset.")
    parser.add_argument("--version", action="version", version=f"tokenmaxx {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="list local Claude Code sessions")
    add_common_args(scan)
    scan.set_defaults(func=cmd_scan)

    add = subparsers.add_parser("add", help="add a Claude session to the retry queue")
    add_common_args(add)
    selector = add.add_mutually_exclusive_group(required=True)
    selector.add_argument("--pid", type=int)
    selector.add_argument("--session-id")
    add.add_argument("--cwd", help="override working directory for resume")
    add.set_defaults(func=cmd_add)

    status = subparsers.add_parser("status", help="show tokenmaxx queue status")
    add_common_args(status)
    status.set_defaults(func=cmd_status)

    autoqueue = subparsers.add_parser("autoqueue", help="queue recent Claude sessions whose transcripts show a limit error")
    add_common_args(autoqueue)
    autoqueue.add_argument("--max-session-age-hours", type=float, default=DEFAULT_MAX_SESSION_AGE_HOURS)
    autoqueue.add_argument("--now", type=int, default=0, help=argparse.SUPPRESS)
    autoqueue.set_defaults(func=cmd_autoqueue)

    watch = subparsers.add_parser("watch", help="resume due sessions")
    add_common_args(watch)
    watch.add_argument("--claude-bin", default="claude")
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

    launchd_install = subparsers.add_parser(
        "launchd-install",
        help="write or print a launchd plist, but do not load it",
    )
    add_common_args(launchd_install)
    launchd_install.add_argument("--program", default=None, help="absolute tokenmaxx executable path for launchd")
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
    return args.func(args)
