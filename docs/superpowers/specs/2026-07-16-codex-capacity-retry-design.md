# Codex Model-Capacity Retry

> **Human ruling after task review (2026-07-16):** A qualifying fresh capacity
> event replaces an already-pending item's schedule with exactly
> `hit_at + 300`, whether its current retry is due, earlier, or later. It
> preserves `attempts` and `updated_at`. Logs-path expansion, resolution, and
> SQLite URI construction are part of the loader's guarded failure boundary.

## Goal

Make tokenmaxx automatically retry an unfinished Codex session when the
selected model is temporarily at capacity. The retry uses the same session and
model after five minutes; it does not silently switch models or change Codex
settings.

The feature must preserve tokenmaxx's trust boundary: only an exact,
provider-originated capacity failure may queue a session. User messages,
assistant text, tool output, and copied error banners remain untrusted input.

## Non-goals

- Switching to a fallback model.
- Retrying arbitrary Codex turn failures.
- Parsing capacity text from `history.jsonl` or rollout message content.
- Changing sandbox, approval, service-tier, account, or reasoning settings.
- Bypassing provider capacity or usage limits.

## Trusted Signal

Codex does not persist model-capacity failures as provider error records in the
rollout JSONL. It does persist them in `~/.codex/logs_2.sqlite` as a log row
whose:

- `target` is `codex_core::session::turn`;
- `thread_id` is the affected Codex session ID; and
- `feedback_log_body` ends with the exact provider text
  `Turn error: Selected model is at capacity. Please try a different model.`

tokenmaxx reads the database in SQLite read-only mode. The match is exact and
anchored to the expected target and `Turn error:` suffix. A user copying the
same banner into a prompt, `history.jsonl`, a source file, or tool output cannot
create a capacity event. Path expansion, resolution, URI construction, and
database access share the same fail-open boundary, so path-preparation failures
also produce no capacity events.

## Discovery And Data Flow

Codex discovery continues to start from recent rollout files. For each
discovered session, tokenmaxx evaluates three independent provider signals:

1. a terminal rollout usage-limit event or exhausted rate-limit telemetry;
2. a known remote-compaction disconnect from Codex history; or
3. an exact model-capacity turn error from the Codex logs database.

The capacity query is bounded to the discovered thread IDs and the configured
maximum session age. It returns the newest matching timestamp per thread rather
than loading arbitrary log content into memory.

The matching log timestamp becomes the event timestamp passed to the existing
queue re-arm logic. Existing safeguards still apply:

- a later rollout `task_started` or `task_complete` record suppresses the old
  capacity event because the session has already progressed;
- an existing row re-arms only for a newer event;
- a user-dropped tombstone never re-arms; and
- provider/session composite identity prevents cross-provider collisions.

If a new trusted capacity event arrives while its queue row is still pending,
tokenmaxx sets that row's next attempt to exactly the event's five-minute mark,
whether the current retry is already due, scheduled earlier, or scheduled
later. It does not reset the row's attempt count or change its last-outcome
timestamp, so bounded attempts and queue deduplication remain intact.

## Retry Behavior

A newly queued capacity event becomes due five minutes after the provider log
timestamp. If the timestamp is already more than five minutes old, it is due on
the next watch cycle. Before dispatch, the Codex activity check reloads the
trusted external stop sources. A capacity or remote-compaction stop newer than
the rollout's last `task_started` record makes that unfinished turn inactive;
rollout activity newer than the stop still defers the resume.

The resume command remains:

```text
codex exec resume --all <session-id> <guarded-prompt>
```

No `--model` override is added. Codex therefore resumes the same thread with
its existing model selection. A repeated capacity failure is discovered as a
new event, targets five minutes after that event, and preserves the existing
bounded-attempt count.

## Configuration And Launchd

Add a Codex logs database path alongside the existing sessions and history
paths:

- default: `~/.codex/logs_2.sqlite`;
- CLI: `--codex-logs-db` on discovery, autoqueue, watch, start, and launchd
  commands; and
- launchd: persist the absolute logs database path in `ProgramArguments`.

The new option follows the existing `--codex-history-file` plumbing so
foreground and daemon behavior stay identical.

## Failure Handling

- A missing database produces no capacity events and does not fail the watch
  cycle.
- A path-preparation `OSError` or `RuntimeError`, or an unreadable, locked,
  malformed, or schema-incompatible database, is treated as temporarily
  unavailable; tokenmaxx skips capacity discovery and continues processing
  rollout and history signals.
- Rows with a missing thread ID, non-matching target, or non-exact message are
  ignored.
- Stale rows outside `--max-session-age-hours` are ignored.
- User-authored capacity text remains ignored even when it is identical to the
  banner.

## Testing

Tests create synthetic SQLite databases and rollout records. Coverage includes:

- the exact provider log row queues the matching Codex session;
- the retry time is five minutes after the event;
- later rollout activity suppresses an older capacity row;
- a newer capacity row re-arms a resolved queue item;
- a repeated capacity row sets due, earlier, and later pending schedules to the
  exact five-minute mark without resetting attempts or changing `updated_at`;
- wrong target, missing thread ID, partial text, user/history text, and stale
  rows do not queue;
- missing, locked, malformed, and incompatible databases fail open for the
  daemon while producing no capacity event, including path-preparation
  `OSError` and `RuntimeError` failures;
- CLI parsing and launchd plists carry `--codex-logs-db`; and
- the existing model-capacity rollout test continues proving that ordinary
  rollout errors are not misclassified as usage limits.

Run the repository's complete unit-test and syntax-validation gates before
release.

## Documentation And Release

Update the README, architecture, security notes, package version, and guarded
prompt wording to distinguish three cases: usage limits wait for reset,
connection failures retry, and model-capacity failures retry the same model
after five minutes. Reinstall the package, reload launchd, and verify the live
plist, process arguments, startup log, and queue behavior before declaring the
release complete.
