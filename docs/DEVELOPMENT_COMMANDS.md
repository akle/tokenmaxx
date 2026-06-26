# Development Commands

Run all commands from the repository root.

## Environment

tokenmaxx has no runtime dependencies. It requires Python 3.10 or newer.

```bash
python3 -m tokenmaxx --version
```

Optional editable-style local command using uv:

```bash
uv tool install --force .
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Syntax

Use a bytecode cache outside the repository:

```bash
PYTHONPYCACHEPREFIX=/tmp/tokenmaxx-pycache python3 -m py_compile tokenmaxx/*.py tests/test_tokenmaxx.py
```

## CLI Smoke

```bash
python3 -m tokenmaxx --help
python3 -m tokenmaxx scan --help
python3 -m tokenmaxx autoqueue --help
python3 -m tokenmaxx watch --once --dry-run --no-auto-queue --queue /tmp/tokenmaxx-smoke-queue.jsonl
python3 -m tokenmaxx start --help
python3 -m tokenmaxx stop --help
python3 -m tokenmaxx logs --help
```

## Install Smoke

```bash
python3 -m venv /tmp/tokenmaxx-venv
/tmp/tokenmaxx-venv/bin/pip install .
/tmp/tokenmaxx-venv/bin/tokenmaxx --version
```

## Daemon Checks On macOS

These commands inspect the user's actual local daemon and should not run in CI:

```bash
tokenmaxx status
tokenmaxx logs --lines 40
launchctl print gui/$(id -u)/com.local.tokenmaxx
```

## Git Hygiene

```bash
git status --short
git diff --check
```

## Not Configured Yet

- No formatter command is configured.
- No linter command is configured.
- No static type-check command is configured.
- No build backend command is needed beyond normal `pip install .` package
  installation smoke testing.
