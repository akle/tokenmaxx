# Security Policy

## Supported Versions

`tokenmaxx` is currently alpha software. Security fixes target the latest released version.

## Reporting A Vulnerability

Please do not open a public issue for a vulnerability that exposes tokens, local credentials, or private session content.

Report privately to the maintainers of the repository where you received this project. If this project moves to a public GitHub org, use GitHub private vulnerability reporting.

## Security Notes

`tokenmaxx` reads local Claude Code session metadata and recent transcript lines to detect usage/rate/session limit errors. It can execute `claude --resume <session-id> -p <prompt>` from the session working directory.

Review the queue before running a watcher:

```bash
tokenmaxx status
```

Review generated launchd plists before loading:

```bash
tokenmaxx launchd-install --dry-run
```

`tokenmaxx` does not ask for provider credentials and does not bypass provider limits.
