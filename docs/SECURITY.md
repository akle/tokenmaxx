# Security Guide

This guide covers repository-specific security posture for tokenmaxx. The public
reporting policy remains in the root [SECURITY.md](../SECURITY.md).

## Local Data Boundaries

tokenmaxx reads local provider data:

- `~/.claude/sessions` for session metadata;
- `~/.claude/projects` for transcript tails;
- `~/.codex/sessions` for Codex rollout metadata and event tails;
- `~/.tokenmaxx/queue.jsonl` for queue state;
- `~/.tokenmaxx/tokenmaxx.log` for daemon output by default.

These files can reveal private repository paths, task descriptions, prompt
content, and snippets of provider output. Do not paste real queue, transcript,
or Codex rollout content into issues, docs, tests, or examples.

## Secrets

- Do not store API keys, session tokens, OAuth credentials, or private env files
  in this repository.
- Do not add examples that include real Claude or Codex session IDs or private
  local paths.
- Do not add telemetry or network upload behavior without a separate security
  review and explicit user consent.

## Provider Limits

The core safety invariant is that tokenmaxx does not bypass provider limits. It
only detects limit output, waits, retries later, and stops after configured
attempts. Any change that weakens that invariant is a security and trust risk.
Provider resume commands inherit the user's normal configuration. tokenmaxx
never adds sandbox, approval, permission, or other bypass flags.

## Subprocess Boundary

`claude.py` constructs `claude --resume <id> -p <prompt>` and `codex.py`
constructs `codex exec resume <id> <prompt>`. `runner.py` executes either
command. Each command:

- runs in the queued repository working directory;
- runs in a new process group;
- is terminated on timeout;
- captures stdout and stderr into the queue's `lastOutput`.

Agents changing this path must preserve timeout and process-group cleanup.
The shared resume lock must also continue to allow only one provider
continuation globally per queue.

## Detection Boundary

Claude Code auto-queue accepts only terminal synthetic assistant limit banners.
Codex auto-queue accepts only terminal provider-authored error events with the
structured `usage_limit_exceeded` code, plus the exact provider-authored
usage-limit prefix when a Codex version omits that code. User prompts, assistant
text, tool output, file content, and generic errors must never trigger a queue
entry merely because they mention a limit.

## Launchd Boundary

The launchd plist runs `tokenmaxx watch` on a schedule. Treat plist changes as
privileged local automation:

- keep generated plists readable;
- keep log paths explicit;
- pin every available Claude and Codex executable to an absolute path;
- surface `launchctl` failures;
- avoid hidden environment assumptions;
- document any new background behavior in `README.md` and
  `docs/DEVELOPMENT_COMMANDS.md`.

## Security Review Triggers

Run a security review when a change touches:

- transcript parsing;
- queue file format or locking;
- subprocess invocation;
- daemon installation/loading;
- file paths under a user's home directory;
- packaging metadata or release automation;
- any network call or telemetry proposal.
