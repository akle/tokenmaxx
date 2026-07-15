# tokenmaxx

`tokenmaxx` is a limit-aware resume queue for Claude Code and Codex sessions.

It watches local Claude Code and Codex sessions, queues sessions that hit a
provider-authored limit, and resumes them later with a guarded prompt after the
reset window has passed.

It does **not** bypass Claude Code, Codex, Anthropic, OpenAI, or provider limits.
It only waits, retries later, and stops after a configured number of attempts.
It never passes sandbox, approval, permission, or other bypass flags to either
provider CLI.

## Why This Exists

Claude Code and Codex can do useful long-running work, but sessions sometimes
hit usage, rate, credit, or session limits before the work is finished. The
manual fix is boring: remember which terminal was doing what, wait for the
limit reset, and type "continue" later.

`tokenmaxx` turns that into a local queue:

1. Find recent Claude Code and Codex sessions.
2. Read bounded transcript tails and Codex history records for provider-authored stop events.
3. Queue only terminal Claude limit banners, structured Codex limit errors, or the exact Codex remote-compaction disconnect record.
4. Resume due sessions later with a prompt that first asks the provider to verify whether work remains.
5. Back off on limit output and block noisy sessions after repeated attempts.

## Install

From a checkout:

```bash
pipx install .
```

Or for local development:

```bash
python3 -m tokenmaxx --version
```

No runtime dependencies.

## Quickstart

List recent local sessions from both providers:

```bash
tokenmaxx scan
```

Queue sessions that hit a usage/rate/session limit:

```bash
tokenmaxx autoqueue
```

By default, `autoqueue` scans both provider data directories, `watch` handles
every provider whose CLI is available, and `start` records every available
provider CLI in the launchd service.

Inspect queue state:

```bash
tokenmaxx status
```

Status and scan output qualify every row by provider:

```text
STATUS    ATT  NEXT              PROVIDER  SESSION   DIRECTORY       LAST
pending     0  due now           claude    claude-d  /tmp/project-a
pending     1  2026-07-12 12:30  codex     codex-de  /tmp/project-b  retrying
```

Manual add and drop default to Claude for backward compatibility. Select Codex
explicitly:

```bash
tokenmaxx add --provider codex --session-id <session-id>
tokenmaxx drop --provider codex --session-id <session-id-or-unique-prefix>
```

Dry-run a resume:

```bash
tokenmaxx watch --once --dry-run
```

`watch` runs `autoqueue` first by default. To process only existing queue items:

```bash
tokenmaxx watch --once --no-auto-queue
```

Run one due resume:

```bash
tokenmaxx watch --once
```

Run it in the background on macOS:

```bash
tokenmaxx start
```

Check whether the daemon is loaded and what is queued:

```bash
tokenmaxx status
```

Read background logs:

```bash
tokenmaxx logs
```

Stop the background watcher:

```bash
tokenmaxx stop
```

Run as a loop:

```bash
tokenmaxx watch
```

## Safety Model

`tokenmaxx` is intentionally conservative.

- Queue state lives at `~/.tokenmaxx/queue.jsonl` by default.
- Queue writes use a sibling lock file: `queue.jsonl.lock`.
- Auto-queue reads Claude Code session metadata in `~/.claude/sessions`, Claude
  transcript tails in `~/.claude/projects`, and Codex rollout files under
  `~/.codex/sessions` plus the bounded `~/.codex/history.jsonl` history tail.
- Auto-queue only queues sessions whose transcript ends on a Claude limit banner
  (a synthetic assistant record). Sessions that merely *mention* limits in
  regular messages, tool output, or file contents are not queued.
- Codex auto-queue accepts a terminal provider-authored `event_msg` error with
  structured code `usage_limit_exceeded`, the exact provider-authored
  usage-limit error prefix when the code is omitted, or a `token_count` event
  whose rate-limit window is exhausted and has a future reset. It also accepts
  the exact `Error running remote compact task` disconnect record in
  `~/.codex/history.jsonl` when the rollout has no newer task activity. Generic
  errors, model-capacity errors, and limit text in user, assistant, tool, or
  file content are ignored.
- A session whose queue row is already `done` or `blocked` is re-armed with
  fresh attempts when it hits a *new* limit (banner newer than the row).
  Sessions you `drop` are never re-armed.
- `watch` defers a resume while the provider session is active. Claude Code uses
  process and metadata activity; Codex uses current rollout task state with a
  bounded stale-activity window.
- `watch` processes one due item at a time, and runs the resume outside the
  queue lock so `status`, `add`, and `drop` stay usable while a resume is in
  flight.
- The queue records the provider subprocess PID after launch and does not
  reclaim an expired lease while that process is still alive.
- A separate `queue.jsonl.resume.lock` is global to the shared queue and allows
  only one continuation at a time across both providers and concurrent watchers.
- `drop` stops retries by tombstoning the item (`blocked`, "dropped by user")
  instead of deleting it, so auto-queue does not silently re-add the session on
  the next cycle.
- Limit output is rescheduled with `--retry-delay-seconds`, default 5 hours.
- Unknown output is rescheduled with `--followup-delay-seconds`, default 15 minutes.
- Repeated attempts move a session to `blocked`, default after 5 attempts.
- launchd install writes a plist but does not call `launchctl load`.
- uninstall removes the plist but does not call `launchctl unload`.
- `start` and `stop` are explicit convenience commands that do call `launchctl load` and `launchctl unload`.

Each provider receives a guarded prompt. The commands are:

```text
claude --resume <id> -p <prompt>
codex exec resume --all <id> <prompt>
```

Neither command includes bypass flags. The Claude Code prompt is:

```text
Continue this Claude Code session only if unfinished.

First inspect the current repo/session state and decide whether work remains.
If the prior task is already complete, say DONE and stop.
If it hit a usage/rate/session limit before finishing, resume the remaining work.
Before long work, write or update a checkpoint with completed work and next steps.
Keep the response concise and operational.
```

The Codex prompt applies the same guard while preserving Codex's configured
sandbox and approval settings:

```text
Continue this Codex session only if unfinished.

First inspect the current repo/session state and decide whether work remains.
If the prior task is already complete, respond with exactly STATUS: DONE and stop.
If it hit a usage limit before finishing, resume the remaining work.
Do not change or bypass the configured sandbox or approval settings.
When the task is complete, end with exactly STATUS: DONE.
```

## launchd On macOS

Easy background mode:

```bash
tokenmaxx start
tokenmaxx status
tokenmaxx logs
tokenmaxx stop
```

`start` and `launchd-install` resolve the installed `claude` and `codex`
executables and record every available absolute path in the launchd plist. At
least one provider CLI must resolve. They also record your current `PATH`
in the plist's `EnvironmentVariables`, because launchd starts agents with a bare
system PATH and version-manager shims (asdf, mise) fail without yours. If a
provider is not on your shell PATH, pass its executable explicitly:

```bash
tokenmaxx start --claude-bin /absolute/path/to/claude
tokenmaxx start --codex-bin /absolute/path/to/codex
```

If the service is already loaded and daemon arguments change, run `tokenmaxx stop`
then `tokenmaxx start` so launchd reloads the updated plist.

Review-first mode:

Preview the plist:

```bash
tokenmaxx launchd-install --dry-run
```

Write the plist:

```bash
tokenmaxx launchd-install
```

Review it, then load it yourself:

```bash
launchctl load ~/Library/LaunchAgents/com.local.tokenmaxx.plist
```

Unload and remove:

```bash
launchctl unload ~/Library/LaunchAgents/com.local.tokenmaxx.plist
tokenmaxx launchd-uninstall
```

## Commands

```bash
tokenmaxx scan
tokenmaxx autoqueue
tokenmaxx add --pid <pid>
tokenmaxx add --session-id <uuid>
tokenmaxx add --provider codex --session-id <uuid>
tokenmaxx drop --session-id <uuid-or-unique-prefix>
tokenmaxx drop --provider codex --session-id <uuid-or-unique-prefix>
tokenmaxx status
tokenmaxx watch --once --dry-run
tokenmaxx watch --once
tokenmaxx start
tokenmaxx logs
tokenmaxx stop
tokenmaxx launchd-install --dry-run
tokenmaxx launchd-install
tokenmaxx launchd-uninstall --dry-run
tokenmaxx launchd-uninstall
```

Common flags:

```bash
--queue ~/.tokenmaxx/queue.jsonl
--sessions-dir ~/.claude/sessions
--projects-dir ~/.claude/projects
--codex-sessions-dir ~/.codex/sessions
--max-session-age-hours 24
--retry-delay-seconds 18000
--followup-delay-seconds 900
--max-attempts 5
--resume-timeout-seconds 14400
--lock-timeout-seconds 10
--claude-bin /absolute/path/to/claude
--codex-bin /absolute/path/to/codex
```

## Adjacent Projects

`tokenmaxx` is not a usage dashboard, router, or multi-agent UI. It fits next to
provider CLIs and adjacent tools such as:

- `ccusage`: Claude Code usage and cost analyzer. https://github.com/ryoppippi/ccusage
- `Claude-Code-Usage-Monitor`: terminal usage monitor with prediction/warnings. https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor
- `claude-code-router`: model/provider router for Claude Code. https://github.com/musistudio/claude-code-router
- `claude-squad`: tmux/worktree manager for multiple AI coding agents. https://github.com/smtg-ai/claude-squad

The job here is narrower: keep unfinished Claude Code and Codex sessions from
being forgotten after a limit window.

## Development

Run tests:

```bash
python3 -m unittest discover -s tests -v
```

Run syntax checks:

```bash
PYTHONPYCACHEPREFIX=/tmp/tokenmaxx-pycache python3 -m py_compile tokenmaxx/*.py tests/*.py
```

Smoke-test without launching either provider:

```bash
tokenmaxx watch --once --dry-run
```

A GitHub Actions workflow template lives at `docs/github-workflows/test.yml`.
Copy it to `.github/workflows/test.yml` in a repo where your GitHub credential has workflow-write permission.

## Status

Alpha. The queue format may change before `1.0`.
