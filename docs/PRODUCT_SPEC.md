# Product Spec

## Product

tokenmaxx is a local command-line tool for developers who run Claude Code
sessions that sometimes stop mid-task because of usage, rate, credit, or
session limits.

The tool keeps those interrupted sessions from being forgotten. It scans local
Claude Code session metadata and transcript tails, queues only sessions that
appear to have hit a limit, and resumes them after a delay with a guarded prompt
that first asks Claude to check whether work is actually unfinished.

## Users

- Developers running long Claude Code sessions on a local machine.
- Agent-heavy users who have multiple Claude Code sessions across several
  repositories.
- Maintainers who want a small, inspectable queue instead of a multi-agent UI or
  hosted service.

## Problem

When a Claude Code session hits a limit, the developer has to remember which
terminal was working on what, wait until the reset window, return to the right
repository, and ask the session to continue. With multiple sessions, this turns
into manual bookkeeping and unfinished work gets lost.

## Core Capabilities

- List local Claude Code sessions from `~/.claude/sessions`.
- Read transcript tails from `~/.claude/projects` and identify limit output.
- Queue only sessions that look limited, not every idle or historical session.
- Resume one due session at a time with `claude --resume`.
- Classify resume output as done, limited, blocked, or unknown.
- Back off until the reset time when Claude reports one.
- Run as a macOS launchd background service with explicit `start`, `stop`,
  `status`, and `logs` commands.

## Success Criteria

- A developer can install the package, run `tokenmaxx start`, and trust it to
  pick up future limited sessions without manual queue edits.
- The queue remains visible and editable as JSONL.
- The daemon never spawns unbounded Claude processes.
- The tool is safe to share as a public open-source package because its
  limitations and security boundaries are explicit.

## Non-Goals

- tokenmaxx does not bypass Claude, Anthropic, or provider limits.
- tokenmaxx is not a usage dashboard, cost monitor, model router, or hosted
  orchestration service.
- tokenmaxx does not inspect or modify repository code except by resuming an
  existing Claude Code session.
- tokenmaxx does not upload telemetry, transcripts, queues, or logs.
