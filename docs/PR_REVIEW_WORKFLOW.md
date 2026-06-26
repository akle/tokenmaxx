# PR Review Workflow

## Before Opening A PR

Run:

```bash
python3 -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=/tmp/tokenmaxx-pycache python3 -m py_compile tokenmaxx/*.py tests/test_tokenmaxx.py
git diff --check
```

For CLI or packaging changes, also run:

```bash
python3 -m tokenmaxx --help
python3 -m tokenmaxx watch --once --dry-run --no-auto-queue --queue /tmp/tokenmaxx-smoke-queue.jsonl
python3 -m venv /tmp/tokenmaxx-venv
/tmp/tokenmaxx-venv/bin/pip install .
/tmp/tokenmaxx-venv/bin/tokenmaxx --version
```

## Review Focus

- Does the change preserve the provider-limit boundary?
- Does it avoid unbounded background work?
- Does it keep queue state inspectable and recoverable?
- Does it update tests and docs for user-facing behavior?
- Does launchd behavior stay explicit and reviewable?

## Release Notes

Public-facing changes should call out:

- new or changed commands;
- queue format changes;
- daemon behavior changes;
- install or packaging changes;
- security-sensitive behavior changes.
