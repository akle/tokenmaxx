# Architecture

## Shape

tokenmaxx is a Python package and CLI with no runtime dependencies. The public
entry point is the `tokenmaxx` console script defined in `pyproject.toml`, and
`python3 -m tokenmaxx` delegates to the same CLI.

```text
tokenmaxx/
|-- cli.py       # argparse commands and user-facing output
|-- claude.py    # Claude Code session discovery, transcript checks, resume calls
|-- queue.py     # QueueItem model, JSONL persistence, locking, classification
|-- launchd.py   # macOS LaunchAgent plist generation and launchctl wrappers
|-- config.py    # default paths, timings, and guarded resume prompt
|-- __main__.py  # python -m tokenmaxx entry point
`-- __init__.py  # package version
```

## Runtime Data Flow

1. `tokenmaxx autoqueue` or `tokenmaxx watch` reads session metadata from
   `~/.claude/sessions`.
2. For each recent session, `claude.find_transcript` looks for a matching JSONL
   transcript under `~/.claude/projects`.
3. `claude.session_hit_limit` reads the transcript tail and asks
   `queue.classify_output` whether it contains retryable limit output.
4. Matching sessions become `QueueItem` records in `~/.tokenmaxx/queue.jsonl`.
5. `tokenmaxx watch` processes one due queue item at a time under a queue lock.
6. `claude.run_due_item` runs `claude --resume <session-id> -p <guarded prompt>`
   unless the command is a dry run.
7. `queue.update_item_after_output` records the result and either marks the item
   `done`, blocks it, or schedules the next attempt.

## Queue Model

The queue is JSONL for inspectability and recovery. Each line maps to
`QueueItem` with:

- `cwd`
- `sessionId`
- `status`
- `nextAttemptAt`
- `attempts`
- `lastOutput`
- `blockedReason`
- `createdAt`
- `updatedAt`

Writes use a sibling `queue.jsonl.lock` file. On macOS and Linux the lock uses
`fcntl.flock`; on platforms without `fcntl`, the context manager still preserves
the write path but does not provide OS-level mutual exclusion.

## Output Classification

`queue.classify_output` is the central classification point. It recognizes:

- retryable limit output: usage, credit, rate, temporary, session, or "try again"
  limit text;
- non-retryable output: prompt/context length failures;
- completion output: `DONE`, `STATUS: DONE`, or Markdown-wrapped variants.

When Claude prints a reset time such as `resets 5:10pm
(America/Mexico_City)`, `reset_time_from_output` schedules the next attempt one
minute after that reset time.

## Daemon Model

macOS background operation uses launchd:

- `tokenmaxx launchd-install` writes or previews a plist but does not load it.
- `tokenmaxx launchd-uninstall` removes the plist but does not unload it.
- `tokenmaxx start` writes the plist and calls `launchctl load`.
- `tokenmaxx stop` calls `launchctl unload`.
- `tokenmaxx status` reports both launchd state and queue state.
- `tokenmaxx logs` prints or follows the configured log file.

The daemon command is a normal `tokenmaxx watch` invocation with queue, sessions,
projects, lock timeout, and interval arguments recorded in the plist.

## Failure Boundaries

- If no Claude session metadata exists, scans return an empty list.
- Invalid session JSON is skipped.
- Invalid queue JSON fails loudly with the queue line number.
- Resume subprocesses run in a new process group and are killed on timeout.
- Unknown Claude output is retried after the follow-up delay until attempts are
  exhausted.
