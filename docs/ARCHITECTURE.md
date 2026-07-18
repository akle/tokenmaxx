# Architecture

## Shape

tokenmaxx is a Python package and CLI with no runtime dependencies. The public
entry point is the `tokenmaxx` console script defined in `pyproject.toml`, and
`python3 -m tokenmaxx` delegates to the same CLI.

```text
tokenmaxx/
|-- cli.py        # argparse commands, provider dispatch, and user-facing output
|-- claude.py     # Claude discovery, limit/activity checks, resume command
|-- codex.py      # Codex discovery, limit/activity checks, resume command
|-- transcript.py # bounded JSONL tail reading and timestamp parsing
|-- runner.py     # shared subprocess execution, timeout, and result handling
|-- queue.py      # queue model, persistence, locking, and classification
|-- launchd.py    # macOS LaunchAgent plist generation and launchctl wrappers
|-- config.py     # provider paths, timings, and guarded prompts
|-- __main__.py   # python -m tokenmaxx entry point
`-- __init__.py   # package version
```

## Runtime Data Flow

1. `tokenmaxx autoqueue` scans both providers: Claude metadata under
   `~/.claude/sessions`, Codex rollouts under `~/.codex/sessions`, and the
   bounded Codex history tail in `~/.codex/history.jsonl`, plus exact provider
   turn failures from read-only `~/.codex/logs_2.sqlite`. When
   auto-queue is enabled, `tokenmaxx watch` scans only providers whose
   executables resolved for that invocation.
2. For each recent Claude session, `claude.find_transcript` looks for a matching
   JSONL transcript under `~/.claude/projects`. Codex metadata and events share
   one rollout file: discovery reads forward to its `session_meta`, then
   `transcript.tail_records` bounds the event-tail reads for detection and
   activity checks.
3. `claude.session_limit_hit_at` walks Claude transcript tail records from the end
   and reports a stop event (with its timestamp) only when the last assistant
   activity is a synthetic limit banner or the exact synthetic
   `API Error: Unable to connect to API (ConnectionRefused)` record
   (`message.model == "<synthetic>"`). Regular messages that merely mention
   limit or connection-error phrases never queue a session, and a real
   assistant record after the stop event means the session already resumed.
4. `codex.session_limit_hit_at` accepts a terminal provider-authored
   `event_msg` error with structured code `usage_limit_exceeded`, the exact
   provider usage-limit prefix when the structured code is absent, or an
   exhausted `token_count.rate_limits` window with a future reset. Generic and
   unrelated errors are ignored; a later task start suppresses an old limit.
   `codex.load_remote_compact_events` additionally reads the known
   remote-compaction stream-disconnect records from `history.jsonl`; a later
   rollout `task_started` or `task_complete` suppresses that history event.
   `codex.load_model_capacity_events` queries only discovered thread IDs in the
   read-only Codex logs database and accepts only
   `target == "codex_core::session::turn"` rows ending with the exact provider
   `Turn error: Selected model is at capacity. Please try a different model.`
   suffix. Newer rollout task activity suppresses both history and capacity
   events. A capacity row is due five minutes after its timestamp and resumes
   the same model.
   Telemetry-backed queue rows wait until the reported reset plus the normal
   reset buffer.
5. Matching sessions become `QueueItem` records in `~/.tokenmaxx/queue.jsonl`.
   A session with an existing `done`/`blocked` row is re-armed (pending, fresh
   attempts) when its banner is newer than the row's last update — a new limit
   event after the row was resolved. Rows dropped by the user are never
   re-armed.
6. `tokenmaxx watch` picks one due queue item per cycle. Provider-specific
   activity checks defer active sessions without consuming an attempt: Claude
   uses live process and metadata state, while Codex uses recent rollout task
   state.
7. A nonblocking `queue.jsonl.resume.lock` admits only one continuation across
   both providers and all watchers. `watch` then writes a lease on the item (its
   `nextAttemptAt` is pushed past the resume timeout), releases the queue lock,
   and
   the provider wrapper runs `claude --resume <session-id> -p <guarded prompt>`
   or `codex exec resume --all <session-id> <guarded prompt>` through `runner.py`
   outside the lock so `status`, `add`, and `drop` stay usable during a resume.
   Once spawned, the provider PID is persisted with the lease; an expired lease
   is deferred while that provider process is still alive. Dry runs stay under
   the lock and mutate nothing but cosmetic fields.
8. `queue.update_item_after_output` records the result and
   `queue.merge_resumed_item` folds it back into a freshly loaded queue; a row
   resolved mid-resume (for example `tokenmaxx drop`) wins over the resume
   result. If the process dies mid-resume, the lease expires and the item
   resurfaces.

## Queue Model

The queue is JSONL for inspectability and recovery. Each line maps to
`QueueItem` with:

- `cwd`
- `sessionId`
- `provider` (`claude` or `codex`)
- `status`
- `nextAttemptAt`
- `attempts`
- `lastOutput`
- `blockedReason`
- `createdAt`
- `updatedAt`

Identity is `(provider, sessionId)`. Existing rows without `provider` load as
Claude rows, preserving queues created before version 0.5.0. New rows always
serialize the provider, and status displays it.

Writes use a sibling `queue.jsonl.lock` file; continuations use the separate
global `queue.jsonl.resume.lock`. On macOS and Linux each lock uses
`fcntl.flock`; on platforms without `fcntl`, the context manager still preserves
the write path but does not provide OS-level mutual exclusion.

## Output Classification

`queue.classify_output` is the central classification point. It recognizes:

- retryable limit output: usage, credit, rate, temporary, session, or "try again"
  limit text;
- non-retryable output: prompt/context length failures;
- completion output: `DONE`, `STATUS: DONE`, or Markdown-wrapped variants.

When either provider prints a reset time such as `resets 5:10pm` or `try again
at 5:10pm
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

The daemon command is a normal `tokenmaxx watch` invocation with queue, Claude
sessions/projects, Codex sessions/history, lock timeout, interval, and all
available `--claude-bin` and `--codex-bin` arguments recorded in the plist. Provider
executables are resolved at install/start time because launchd
does not inherit the user's interactive shell PATH. The invoking shell's `PATH`
is also embedded in the plist's `EnvironmentVariables`, because version-manager
shims (asdf, mise) exec their manager binary from PATH and die under launchd's
bare system PATH otherwise.

## Failure Boundaries

- If one provider has no session data or executable, the other still operates.
- Invalid Claude metadata and malformed or disappearing Codex rollouts are
  skipped.
- Invalid queue JSON fails loudly with the queue line number.
- Resume subprocesses run in a new process group and are killed on timeout.
- Unknown provider output is retried after the follow-up delay until attempts are
  exhausted.
- Unknown queue providers fail closed and are never dispatched.
- Provider commands inherit normal user configuration; tokenmaxx passes no
  sandbox, approval, permission, or bypass flags.
