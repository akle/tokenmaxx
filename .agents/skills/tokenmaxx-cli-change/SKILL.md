---
name: tokenmaxx-cli-change
description: Use when adding or changing a tokenmaxx CLI command, flag, help text, or output.
---

# tokenmaxx CLI Change

1. Read `AGENTS.md`, `docs/STANDARDS.md`, and `tokenmaxx/cli.py`.
2. Add or update tests in `tests/test_tokenmaxx.py` before changing behavior.
3. Keep command names and help text explicit about provider-limit boundaries.
4. Update `README.md`, `docs/DEVELOPMENT_COMMANDS.md`, and `AGENTS.md` if the
   command surface changes.
5. Run:

   ```bash
   python3 -m unittest discover -s tests -v
   PYTHONPYCACHEPREFIX=/tmp/tokenmaxx-pycache python3 -m py_compile tokenmaxx/*.py tests/test_tokenmaxx.py
   ```
