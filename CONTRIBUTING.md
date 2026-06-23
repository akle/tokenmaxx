# Contributing

Thanks for improving `tokenmaxx`.

## Local Development

Run the tests:

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest scripts/test_tokenmaxx.py -v
```

Run syntax checks:

```bash
python3 -m py_compile tokenmaxx/*.py scripts/tokenmaxx.py tests/test_package.py scripts/test_tokenmaxx.py
```

## Design Rules

- No runtime dependencies unless there is a clear operational reason.
- Do not bypass Claude or provider usage limits.
- Keep daemon setup explicit. Generate files, but do not call `launchctl load` automatically.
- Tests must not launch Claude, load launchd, or mutate real user state.
- Queue state must remain human-inspectable JSONL.

## Pull Requests

Include:

- What user problem changed.
- How you tested it.
- Any queue format changes.
- Any safety implications.
