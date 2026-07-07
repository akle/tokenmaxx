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
3. `claude.session_limit_hit_at` walks the transcript tail records from the end
   and reports a limit (with the banner's timestamp) only when the last
   assistant activity is a synthetic limit banner
   (`message.model == "<synthetic>"`, classified by `queue.classify_output`).
   Regular messages that merely mention limit phrases never queue a session,
   and a real assistant record after the banner means the session already
   resumed.
4. Matching sessions become `QueueItem` records in `~/.tokenmaxx/queue.jsonl`.
   A session with an existing `done`/`blocked` row is re-armed (pending, fresh
   attempts) when its banner is newer than the row's last update — a new limit
   event after the row was resolved. Rows dropped by the user are never
   re-armed.
5. `tokenmaxx watch` picks one due queue item per cycle. If the session is still
   active in a live Claude Code process (busy, or updated within the last 30
   minutes, with an alive pid), the item is deferred by the follow-up delay
   without consuming an attempt.
6. Otherwise `watch` writes a lease on the item (its `nextAttemptAt` is pushed
   past the resume timeout), releases the queue lock, and
   `claude.run_due_item` runs `claude --resume <session-id> -p <guarded prompt>`
   outside the lock so `status`, `add`, and `drop` stay usable during a resume.
   Dry runs stay under the lock and mutate nothing but cosmetic fields.
7. `queue.update_item_after_output` records the result and
   `queue.merge_resumed_item` folds it back into a freshly loaded queue; a row
   resolved mid-resume (for example `tokenmaxx drop`) wins over the resume
   result. If the process dies mid-resume, the lease expires and the item
   resurfaces.

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
projects, lock timeout, interval, and `--claude-bin` arguments recorded in the
plist. The Claude executable is resolved at install/start time because launchd
does not inherit the user's interactive shell PATH. The invoking shell's `PATH`
is also embedded in the plist's `EnvironmentVariables`, because version-manager
shims (asdf, mise) exec their manager binary from PATH and die under launchd's
bare system PATH otherwise.

## Failure Boundaries

- If no Claude session metadata exists, scans return an empty list.
- Invalid session JSON is skipped.
- Invalid queue JSON fails loudly with the queue line number.
- Resume subprocesses run in a new process group and are killed on timeout.
- Unknown Claude output is retried after the follow-up delay until attempts are
  exhausted.
