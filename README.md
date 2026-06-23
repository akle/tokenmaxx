# tokenmaxx

`tokenmaxx` is a limit-aware resume queue for Claude Code sessions.

It watches Claude Code's local session metadata, lets you queue unfinished sessions, and resumes them later with a guarded prompt after the reset window has passed.

It does **not** bypass Claude, Anthropic, or provider limits. It only waits, retries later, and stops after a configured number of attempts.

## Why This Exists

Claude Code can do useful long-running work, but sessions sometimes hit usage, rate, or session limits before the work is finished. The manual fix is boring: remember which terminal was doing what, wait for the limit reset, and type "continue" later.

`tokenmaxx` turns that into a local queue:

1. Find Claude Code sessions.
2. Add the unfinished one to a queue.
3. Resume due sessions later with a prompt that first asks Claude to verify whether work remains.
4. Back off on limit output.
5. Block noisy sessions after repeated attempts.

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

List local Claude Code sessions:

```bash
tokenmaxx scan
```

Queue a session:

```bash
tokenmaxx add --pid 23273
```

Inspect queue state:

```bash
tokenmaxx status
```

Dry-run a resume:

```bash
tokenmaxx watch --once --dry-run
```

Run one real due resume:

```bash
tokenmaxx watch --once
```

Run as a loop:

```bash
tokenmaxx watch
```

## Safety Model

`tokenmaxx` is intentionally conservative.

- Queue state lives at `~/.tokenmaxx/queue.jsonl` by default.
- Queue writes use a sibling lock file: `queue.jsonl.lock`.
- `watch` processes one due item at a time.
- Limit output is rescheduled with `--retry-delay-seconds`, default 5 hours.
- Unknown output is rescheduled with `--followup-delay-seconds`, default 15 minutes.
- Repeated attempts move a session to `blocked`, default after 5 attempts.
- launchd install writes a plist but does not call `launchctl load`.
- uninstall removes the plist but does not call `launchctl unload`.

The resume prompt is guarded:

```text
Continue this Claude Code session only if unfinished.

First inspect the current repo/session state and decide whether work remains.
If the prior task is already complete, say DONE and stop.
If it hit a usage/rate/session limit before finishing, resume the remaining work.
Before long work, write or update a checkpoint with completed work and next steps.
Keep the response concise and operational.
```

## launchd On macOS

Preview the plist:

```bash
tokenmaxx install --dry-run
```

Write the plist:

```bash
tokenmaxx install
```

Review it, then load it yourself:

```bash
launchctl load ~/Library/LaunchAgents/com.local.tokenmaxx.plist
```

Unload and remove:

```bash
launchctl unload ~/Library/LaunchAgents/com.local.tokenmaxx.plist
tokenmaxx uninstall
```

## Commands

```bash
tokenmaxx scan
tokenmaxx add --pid <pid>
tokenmaxx add --session-id <uuid>
tokenmaxx status
tokenmaxx watch --once --dry-run
tokenmaxx watch --once
tokenmaxx install --dry-run
tokenmaxx install
tokenmaxx uninstall --dry-run
tokenmaxx uninstall
```

Common flags:

```bash
--queue ~/.tokenmaxx/queue.jsonl
--sessions-dir ~/.claude/sessions
--retry-delay-seconds 18000
--followup-delay-seconds 900
--max-attempts 5
--lock-timeout-seconds 10
```

## Adjacent Projects

`tokenmaxx` is not a usage dashboard, router, or multi-agent UI. It fits next to these projects:

- `ccusage`: Claude Code usage and cost analyzer. https://github.com/ryoppippi/ccusage
- `Claude-Code-Usage-Monitor`: terminal usage monitor with prediction/warnings. https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor
- `claude-code-router`: model/provider router for Claude Code. https://github.com/musistudio/claude-code-router
- `claude-squad`: tmux/worktree manager for multiple AI coding agents. https://github.com/smtg-ai/claude-squad

The job here is narrower: keep unfinished Claude Code sessions from being forgotten after a limit window.

## Development

Run tests:

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest scripts/test_tokenmaxx.py -v
```

Run syntax checks:

```bash
python3 -m py_compile tokenmaxx/*.py scripts/tokenmaxx.py tests/test_package.py scripts/test_tokenmaxx.py
```

Smoke-test without launching Claude:

```bash
tokenmaxx watch --once --dry-run
```

A GitHub Actions workflow template lives at `docs/github-workflows/test.yml`.
Copy it to `.github/workflows/test.yml` in a repo where your GitHub credential has workflow-write permission.

## Status

Alpha. The queue format may change before `1.0`.
