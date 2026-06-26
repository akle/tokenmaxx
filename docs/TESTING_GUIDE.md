# Testing Guide

## Framework

tokenmaxx uses Python's standard `unittest` framework. Tests live in
`tests/test_tokenmaxx.py` and exercise the package modules directly.

## Required Gate

Run the full test suite:

```bash
python3 -m unittest discover -s tests -v
```

Run syntax checks without writing bytecode into the repository:

```bash
PYTHONPYCACHEPREFIX=/tmp/tokenmaxx-pycache python3 -m py_compile tokenmaxx/*.py tests/test_tokenmaxx.py
```

Run diff whitespace checks before committing:

```bash
git diff --check
```

## Smoke Checks

CLI version and help:

```bash
python3 -m tokenmaxx --version
python3 -m tokenmaxx --help
python3 -m tokenmaxx start --help
python3 -m tokenmaxx logs --help
```

Dry-run queue processing:

```bash
python3 -m tokenmaxx watch --once --dry-run --no-auto-queue --queue /tmp/tokenmaxx-smoke-queue.jsonl
```

Package install smoke:

```bash
python3 -m venv /tmp/tokenmaxx-venv
/tmp/tokenmaxx-venv/bin/pip install .
/tmp/tokenmaxx-venv/bin/tokenmaxx --version
```

## Test Conventions

- Tests should use `tempfile.TemporaryDirectory` for queues, sessions,
  transcripts, logs, and plists.
- Patch subprocess calls for launchd and Claude invocation tests.
- Prefer tests that assert user-facing behavior: status text, queue state,
  plist arguments, and returned exit codes.
- Add a failing test before changing queue classification, scheduling, launchd
  behavior, or resume subprocess handling.

## Coverage Expectations

Every behavior change should cover:

- the direct function or command behavior;
- one failure path when the behavior can fail;
- queue state transitions when the behavior changes scheduling or status.

There is no coverage tool configured yet. Until one is added, the minimum bar is
focused unit coverage plus the full test, syntax, and smoke gates above.

## Gaps To Consider

- Add a formatter/linter such as `ruff` once the project accepts a development
  dependency.
- Add CI from `docs/github-workflows/test.yml` when repository permissions allow
  workflow commits.
