# Ecosystem Context

tokenmaxx sits next to Claude Code. It does not replace Claude Code and does
not manage models, costs, or team workflows.

## Adjacent Tools

- `ccusage`: usage and cost analysis.
- `Claude-Code-Usage-Monitor`: usage monitor with prediction and warnings.
- `claude-code-router`: provider/model routing.
- `claude-squad`: tmux/worktree management for multiple AI coding agents.

tokenmaxx has a narrower job: keep unfinished Claude Code sessions from being
forgotten after limit windows.

## Local Data Sources

tokenmaxx depends on Claude Code's local session and project transcript layout:

- `~/.claude/sessions`
- `~/.claude/projects`

If Claude Code changes those locations or file shapes, tokenmaxx should fail
clearly and update the scanner in `tokenmaxx/claude.py`.

## Public Package Context

The repository is intended to be shareable as an open-source CLI package. Keep
the README, security policy, agent docs, and packaging metadata suitable for a
public GitHub repository.
