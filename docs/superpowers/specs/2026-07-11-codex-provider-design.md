# Codex Provider Support

## Goal

Make tokenmaxx automatically discover and continue both Claude Code and Codex
sessions that stopped because of a retryable provider limit. A normal install
watches both providers when their CLIs are available, while either provider can
operate independently.

The feature must preserve tokenmaxx's core boundary: it waits for provider
limits to reset and resumes existing work, but never bypasses a provider limit.

## Non-goals

- General multi-agent orchestration.
- Retrying arbitrary Codex failures.
- Changing Codex sandbox, approval, model, or account settings.
- Running more than one continuation at a time.
- Adding a graphical interface.

## Architecture

Claude and Codex are first-class providers sharing one queue and scheduler.
Provider-specific code owns local-session discovery, limit detection,
active-session checks, guarded prompts, and resume commands. Queue persistence,
locking, due-item selection, retry timing, attempt limits, and output
classification remain shared.

`tokenmaxx/claude.py` remains responsible for Claude Code. A new
`tokenmaxx/codex.py` handles Codex JSONL rollouts and `codex exec resume`.
`tokenmaxx/cli.py` performs small, explicit provider dispatch rather than
introducing a plugin framework for two built-in providers.

## Queue Model

`QueueItem` gains a `provider` field with supported values `claude` and
`codex`. Existing JSONL rows without the field load as `claude`, preserving
the current queue across upgrades.

Queue identity is `(provider, session_id)`. Deduplication, re-arming, dropping,
and out-of-lock resume merging use that composite key. This prevents one
provider from affecting another even if their session identifiers happen to
match.

New rows serialize `provider`. `tokenmaxx status` shows a `PROVIDER` column.

## Codex Discovery

Codex sessions live under `~/.codex/sessions/**/*.jsonl`. Discovery considers
only files whose modification time is within `--max-session-age-hours`, then
reads:

- the `session_meta` record for the session ID and working directory;
- a bounded transcript tail for current turn state and limit events.

The scanner queues a session only when its latest unresolved turn contains an
`event_msg` error with an allowlisted retryable Codex error code. The initial
allowlist includes `usage_limit_exceeded`; other codes require a fixture and an
explicit classification before being added. For compatibility with Codex
versions that omit the structured code, an exact provider-authored usage-limit
error message may be recognized only inside an `event_msg` error record.

Ordinary user, assistant, tool, or file text that mentions limits never queues
a session. Generic errors never queue a session. A later `task_started` event
means the session has already moved past the old limit and suppresses it.

Codex activity is derived from transcript state. A recent `task_started`
without a later `task_complete` is active; stale activity eventually expires
so a crashed process cannot defer forever. Immediately before a due Codex
resume, tokenmaxx refreshes this state and defers an active session.

## Resume Flow

A due Codex item runs non-interactively in its recorded working directory:

```text
codex exec resume <session-id> <guarded-prompt>
```

The prompt tells Codex to inspect current repository and session state, stop
with `STATUS: DONE` when no work remains, continue only unfinished work, and
leave a checkpoint before lengthy work. tokenmaxx does not pass dangerous
sandbox or approval overrides; Codex inherits the user's normal configuration.

The existing subprocess timeout, process-group termination, output truncation,
and maximum-attempt behavior apply to both providers. Output enters the shared
`done`, `limited`, `blocked`, or `unknown` state machine.

Codex reset text such as `try again at 12:52 AM` is parsed using the local time
zone and receives the existing reset buffer. If no reset time can be parsed,
the configured retry delay applies.

## CLI And Launchd

`scan`, `autoqueue`, `watch`, and `status` operate on both providers by
default. Manual `add` accepts a provider selector for Codex while preserving
Claude-compatible defaults. `drop` resolves composite identities and reports
provider-qualified ambiguity.

`watch` accepts `--claude-bin` and `--codex-bin`. With neither supplied, a
foreground invocation auto-detects both. `tokenmaxx start` and
`launchd-install` resolve each executable to an absolute path and write every
available provider into the plist. A missing provider is skipped; startup
fails only when neither CLI is available. This preserves launchd reliability
with restricted PATH and version-manager shims.

The daemon keeps a single shared concurrency boundary: at most one due item is
resumed per cycle, regardless of provider.

## Failure Handling

- Missing or malformed Codex session files are skipped.
- Files removed during a scan are skipped.
- Missing one provider executable disables only that provider.
- Unknown queue providers fail closed and are shown as blocked or rejected;
  they are never dispatched to an arbitrary executable.
- Non-limit Codex errors remain outside the automatic queue.
- A resume that hits another limit is rescheduled using its reset time or the
  retry delay.
- A resume requiring unavailable interactive input follows the existing
  unknown-output retry and maximum-attempt policy.

## Testing

Tests use synthetic metadata and transcript records only. Coverage includes:

- loading legacy queue rows as Claude and round-tripping provider fields;
- composite provider/session identity in dedupe, drop, and merge behavior;
- Codex session metadata parsing and recent-file filtering;
- terminal structured usage-limit detection;
- fallback provider-authored error detection;
- rejection of generic errors and ordinary text mentioning limits;
- suppression after a later task start;
- active Codex session deferral;
- Codex reset-time parsing;
- exact dry-run resume commands for both providers;
- one-provider and two-provider launchd plists;
- startup behavior when one or neither executable is installed;
- mixed-provider autoqueue and one-at-a-time watch behavior.

## Documentation And Release

Update the README, product specification, architecture, security notes,
testing guide, development commands, and package metadata to describe Claude
Code and Codex equally. Ship the provider addition as the next minor alpha
release and reinstall/restart the local LaunchAgent so the running daemon uses
the new Codex arguments.
