from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

from . import __version__
from .claude import build_resume_command, load_claude_sessions, run_due_item
from .config import (
    DEFAULT_FOLLOWUP_DELAY_SECONDS,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_PROMPT,
    DEFAULT_RETRY_DELAY_SECONDS,
    default_log_path,
    default_plist_path,
    default_queue_path,
    default_sessions_dir,
)
from .launchd import build_launchd_plist as _build_launchd_plist
from .queue import (
    QueueItem,
    append_queue_item,
    classify_output,
    is_due,
    load_queue,
    one_line,
    queue_lock,
    queue_lock_path,
    update_item_after_output as _update_item_after_output,
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


def cmd_watch(args) -> int:
    while True:
        now = int(args.now or time.time())
        with queue_lock(args.queue, args.lock_timeout_seconds):
            items = load_queue(args.queue)
            processed = False
            for index, item in enumerate(items):
                if is_due(item, now):
                    items[index] = run_due_item(item, now=now, args=args)
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


def build_launchd_plist(
    script_path: Path | None = None,
    queue_path: Path | None = None,
    log_path: Path | None = None,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    program: str | None = None,
) -> str:
    program = program or str(Path(script_path).expanduser())
    if queue_path is None or log_path is None:
        raise TypeError("queue_path and log_path are required")
    return _build_launchd_plist(
        program=program,
        queue_path=queue_path,
        log_path=log_path,
        interval_seconds=interval_seconds,
    )


def update_item_after_output(item: QueueItem, output: str, now: int, args) -> QueueItem:
    return _update_item_after_output(
        item,
        output,
        now=now,
        retry_delay_seconds=args.retry_delay_seconds,
        followup_delay_seconds=args.followup_delay_seconds,
        max_attempts=args.max_attempts,
    )


def cmd_install(args) -> int:
    program = getattr(args, "program", None) or resolve_default_program()
    plist = _build_launchd_plist(
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


def resolve_default_program() -> str:
    return shutil.which("tokenmaxx") or "tokenmaxx"


def cmd_uninstall(args) -> int:
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

    watch = subparsers.add_parser("watch", help="resume due sessions")
    add_common_args(watch)
    watch.add_argument("--claude-bin", default="claude")
    watch.add_argument("--retry-delay-seconds", type=int, default=DEFAULT_RETRY_DELAY_SECONDS)
    watch.add_argument("--followup-delay-seconds", type=int, default=DEFAULT_FOLLOWUP_DELAY_SECONDS)
    watch.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    watch.add_argument("--sleep-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--dry-run", action="store_true")
    watch.add_argument("--now", type=int, default=0, help=argparse.SUPPRESS)
    watch.set_defaults(func=cmd_watch)

    install = subparsers.add_parser("install", help="write or print a launchd plist, but do not load it")
    add_common_args(install)
    install.add_argument("--program", default=None, help="program path/name for launchd ProgramArguments")
    install.add_argument("--plist-path", type=Path, default=default_plist_path())
    install.add_argument("--log-path", type=Path, default=default_log_path())
    install.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    install.add_argument("--dry-run", action="store_true")
    install.set_defaults(func=cmd_install)

    uninstall = subparsers.add_parser("uninstall", help="remove tokenmaxx launchd plist, but do not unload it")
    add_common_args(uninstall)
    uninstall.add_argument("--plist-path", type=Path, default=default_plist_path())
    uninstall.add_argument("--dry-run", action="store_true")
    uninstall.set_defaults(func=cmd_uninstall)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


__all__ = [
    "QueueItem",
    "append_queue_item",
    "build_launchd_plist",
    "build_resume_command",
    "classify_output",
    "cmd_add",
    "cmd_install",
    "cmd_scan",
    "cmd_status",
    "cmd_uninstall",
    "cmd_watch",
    "is_due",
    "load_claude_sessions",
    "load_queue",
    "main",
    "queue_lock_path",
    "run_due_item",
    "update_item_after_output",
]
