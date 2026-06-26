# tokenmaxx Package Guide

This package contains the implementation behind the `tokenmaxx` console script.

## Modules

| Module | Responsibility |
| --- | --- |
| `cli.py` | argparse commands, stdout/stderr behavior, and command orchestration. |
| `claude.py` | Claude Code session metadata, transcript limit detection, and resume subprocess execution. |
| `queue.py` | QueueItem model, JSONL persistence, locking, output classification, and scheduling. |
| `launchd.py` | macOS LaunchAgent plist generation, launchctl wrappers, and daemon state. |
| `config.py` | Default paths, retry timings, session age, resume timeout, and guarded prompt. |
| `__main__.py` | `python3 -m tokenmaxx` entry point. |

## Change Guide

- Add or change a command in `cli.py`, then add tests in `tests/test_tokenmaxx.py`
  and update `README.md` plus `docs/DEVELOPMENT_COMMANDS.md`.
- Add or change queue classification in `queue.py`, then cover it with direct
  transition tests.
- Add or change Claude subprocess behavior in `claude.py`, then cover timeout,
  dry-run, and output-classification behavior.
- Add or change launchd behavior in `launchd.py`, then cover plist contents and
  launchctl wrapper behavior without calling the real system service.

## Validation

```bash
python3 -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=/tmp/tokenmaxx-pycache python3 -m py_compile tokenmaxx/*.py tests/test_tokenmaxx.py
```
