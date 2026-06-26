# Security Guide

This guide covers repository-specific security posture for tokenmaxx. The public
reporting policy remains in the root [SECURITY.md](../SECURITY.md).

## Local Data Boundaries

tokenmaxx reads local Claude Code data:

- `~/.claude/sessions` for session metadata;
- `~/.claude/projects` for transcript tails;
- `~/.tokenmaxx/queue.jsonl` for queue state;
- `~/.tokenmaxx/tokenmaxx.log` for daemon output by default.

These files can reveal private repository paths, task descriptions, prompt
content, and snippets of Claude output. Do not paste real queue or transcript
content into issues, docs, tests, or examples.

## Secrets

- Do not store API keys, session tokens, OAuth credentials, or private env files
  in this repository.
- Do not add examples that include real Claude session IDs or private local
  paths.
- Do not add telemetry or network upload behavior without a separate security
  review and explicit user consent.

## Provider Limits

The core safety invariant is that tokenmaxx does not bypass provider limits. It
only detects limit output, waits, retries later, and stops after configured
attempts. Any change that weakens that invariant is a security and trust risk.

## Subprocess Boundary

`claude.py` invokes `claude --resume` as a subprocess. The command:

- runs in the queued repository working directory;
- runs in a new process group;
- is terminated on timeout;
- captures stdout and stderr into the queue's `lastOutput`.

Agents changing this path must preserve timeout and process-group cleanup.

## Launchd Boundary

The launchd plist runs `tokenmaxx watch` on a schedule. Treat plist changes as
privileged local automation:

- keep generated plists readable;
- keep log paths explicit;
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
