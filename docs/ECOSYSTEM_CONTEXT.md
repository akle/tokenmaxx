# Ecosystem Context

tokenmaxx sits next to Claude Code and Codex. It does not replace either
provider CLI and does not manage models, costs, sandboxes, approvals, or team
workflows.

## Adjacent Tools

- `ccusage`: usage and cost analysis.
- `Claude-Code-Usage-Monitor`: usage monitor with prediction and warnings.
- `claude-code-router`: provider/model routing.
- `claude-squad`: tmux/worktree management for multiple AI coding agents.

tokenmaxx has a narrower job: keep unfinished Claude Code and Codex sessions
from being forgotten after limit windows.

## Local Data Sources

tokenmaxx depends on provider-owned local session layouts:

- `~/.claude/sessions`
- `~/.claude/projects`
- `~/.codex/sessions`

If a provider changes those locations or record shapes, tokenmaxx should fail
closed and update the corresponding scanner in `tokenmaxx/claude.py` or
`tokenmaxx/codex.py`. Codex limit detection remains deliberately narrower than
general output classification: only structured or exact provider-authored
terminal limit errors can enter the automatic queue.

Both providers share one inspectable queue and one global continuation lock.
Queue identity and status are provider-qualified, while old rows without a
provider migrate as Claude rows.

## Public Package Context

The repository is intended to be shareable as an open-source CLI package. Keep
the README, security policy, agent docs, and packaging metadata suitable for a
public GitHub repository.
