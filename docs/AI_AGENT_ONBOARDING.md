# AI Agent Onboarding

## First Session Checklist

1. Confirm the repository and remote:

   ```bash
   pwd
   git remote -v
   git status --short
   ```

2. Read the core context:

   ```bash
   sed -n '1,220p' AGENTS.md
   sed -n '1,220p' README.md
   sed -n '1,220p' docs/ARCHITECTURE.md
   sed -n '1,220p' docs/DEVELOPMENT_COMMANDS.md
   ```

3. Run the baseline validation:

   ```bash
   python3 -m unittest discover -s tests -v
   PYTHONPYCACHEPREFIX=/tmp/tokenmaxx-pycache python3 -m py_compile tokenmaxx/*.py tests/test_tokenmaxx.py
   ```

4. Smoke the CLI without mutating user daemon state:

   ```bash
   python3 -m tokenmaxx --version
   python3 -m tokenmaxx watch --once --dry-run --no-auto-queue --queue /tmp/tokenmaxx-smoke-queue.jsonl
   ```

5. For daemon issues only, inspect live state:

   ```bash
   tokenmaxx status
   tokenmaxx logs --lines 40
   launchctl print gui/$(id -u)/com.local.tokenmaxx
   ```

## Where To Work

- Runtime code: `tokenmaxx/`.
- Tests: `tests/test_tokenmaxx.py`.
- User docs: `README.md`.
- Agent/process docs: `AGENTS.md`, `.agents/`, and `docs/`.
- Structured plan output: `.dwp/`.
- Throwaway artifacts: `tmp/`.

## Common Change Types

- CLI command change: update `tokenmaxx/cli.py`, tests, `README.md`,
  `docs/DEVELOPMENT_COMMANDS.md`, and help smoke checks.
- Queue behavior change: update `tokenmaxx/queue.py`, tests for transitions,
  and security/performance docs if persistence changes.
- Claude resume change: update `tokenmaxx/claude.py`, timeout/process tests,
  and the security guide.
- Launchd change: update `tokenmaxx/launchd.py`, CLI tests, README, and daemon
  commands docs.

## Stop Conditions

Stop and report before proceeding if:

- you find real user secrets or transcript content in tracked files;
- queue or transcript parsing would require copying private data into tests;
- a change would auto-load, auto-unload, or modify a user's launchd service
  without an explicit command;
- validation fails for a reason unrelated to your changes.
