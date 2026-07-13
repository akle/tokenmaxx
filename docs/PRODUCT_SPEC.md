# Product Spec

## Product

tokenmaxx is a local command-line tool for developers who run Claude Code and
Codex sessions that sometimes stop mid-task because of provider limits.

The tool keeps those interrupted sessions from being forgotten. It scans local
Claude Code metadata and transcripts plus Codex rollout tails, queues only
sessions with a terminal provider-authored limit signal, and resumes them after
a delay with a guarded prompt that first checks whether work is unfinished.

## Users

- Developers running long Claude Code or Codex sessions on a local machine.
- Agent-heavy users who have sessions from both providers across several
  repositories.
- Maintainers who want a small, inspectable queue instead of a multi-agent UI or
  hosted service.

## Problem

When a Claude Code or Codex session hits a limit, the developer has to remember
which terminal was working on what, wait until the reset window, return to the
right repository, and ask the session to continue. With multiple sessions,
this turns into manual bookkeeping and unfinished work gets lost.

## Core Capabilities

- List Claude Code sessions from `~/.claude/sessions` and Codex rollouts from
  `~/.codex/sessions`.
- Read bounded transcript tails from `~/.claude/projects` and Codex rollout
  files, identifying only provider-authored terminal limit signals.
- Queue only sessions that look limited, not every idle or historical session.
- Resume one due session at a time globally with `claude --resume` or
  `codex exec resume --all`.
- Classify resume output as done, limited, blocked, or unknown.
- Back off until the reset time when either provider reports one.
- Run as a macOS launchd background service with explicit `start`, `stop`,
  `status`, and `logs` commands.

## Success Criteria

- A developer can install the package, run `tokenmaxx start`, and trust it to
  pick up future limited sessions without manual queue edits.
- The queue remains visible and editable as JSONL.
- Queue status and manual operations qualify identity by provider.
- The daemon never runs more than one continuation across both providers.
- The tool is safe to share as a public open-source package because its
  limitations and security boundaries are explicit.

## Non-Goals

- tokenmaxx does not bypass Claude Code, Codex, Anthropic, OpenAI, or provider
  limits and never passes sandbox, approval, permission, or bypass flags.
- tokenmaxx is not a usage dashboard, cost monitor, model router, or hosted
  orchestration service.
- tokenmaxx does not inspect or modify repository code except by resuming an
  existing Claude Code or Codex session.
- tokenmaxx does not upload telemetry, transcripts, queues, or logs.
