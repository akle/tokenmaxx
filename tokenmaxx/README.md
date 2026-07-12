# tokenmaxx Package Guide

This package contains the implementation behind the `tokenmaxx` console script.

## Modules

| Module | Responsibility |
| --- | --- |
| `cli.py` | argparse commands, stdout/stderr behavior, and command orchestration. |
| `claude.py` | Claude Code session discovery, limit detection, activity checks, and resume command construction. |
| `codex.py` | Codex rollout discovery, structured limit detection, activity checks, and resume command construction. |
| `queue.py` | QueueItem model, JSONL persistence, locking, output classification, and scheduling. |
| `runner.py` | Shared dry-run, subprocess timeout, process-group cleanup, and result handling. |
| `transcript.py` | Shared bounded JSONL tail reading and timestamp parsing. |
| `launchd.py` | macOS LaunchAgent plist generation, launchctl wrappers, and daemon state. |
| `config.py` | Default paths, retry timings, session age, resume timeout, and guarded prompt. |
| `__main__.py` | `python3 -m tokenmaxx` entry point. |

## Change Guide

- Add or change a command in `cli.py`, then add tests in `tests/test_tokenmaxx.py`
  and update `README.md` plus `docs/DEVELOPMENT_COMMANDS.md`.
- Add or change queue classification in `queue.py`, then cover it with direct
  transition tests.
- Add or change provider discovery or command construction in `claude.py` or
  `codex.py`, then cover provider-specific detection and activity behavior.
- Add or change shared subprocess behavior in `runner.py`, then cover timeout,
  dry-run, process cleanup, and output-classification behavior.
- Keep transcript parsing bounded through `transcript.py`. Codex fixtures must
  use synthetic JSONL records, IDs, and paths rather than private rollout data.
- Add or change launchd behavior in `launchd.py`, then cover plist contents and
  launchctl wrapper behavior without calling the real system service.

## Validation

```bash
python3 -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=/tmp/tokenmaxx-pycache python3 -m py_compile tokenmaxx/*.py tests/*.py
```
