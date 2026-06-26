# Standards

## Python Style

- Use Python standard library APIs before adding dependencies.
- Keep functions small and named around the behavior they own.
- Prefer structured parsing and serialization over string splitting when data is
  structured. Queue state is JSON, plist state is generated with `plistlib`.
- Keep path handling on `pathlib.Path`.
- Keep timestamps as integer epoch seconds in queue state.
- Do not add broad fallback logic that hides corrupted queue records. Invalid
  queue lines should fail with a useful error.

## CLI Style

- Use `argparse` for commands and options.
- Output should be plain text that works in a terminal and logs.
- Commands that mutate system state must be explicit. `start` and `stop` may
  call `launchctl`; `launchd-install` and `launchd-uninstall` remain review-first
  helpers.
- Exit non-zero when a requested action cannot complete.
- Help text must describe what the command does without implying provider-limit
  bypass.

## Queue and Resume Standards

- Process one due queue item at a time.
- Hold the queue lock while selecting and updating the item.
- Preserve `lastOutput` enough for status debugging, but keep it truncated so
  status output and queue files stay manageable.
- Any new retry or classification rule belongs in `queue.py` and needs tests.
- The guarded resume prompt in `config.py` must continue to ask Claude to stop
  if the prior task is already complete.

## Launchd Standards

- Generated plists must be reviewable XML.
- Log paths and queue paths must expand user home directories before writing.
- Plists must record an explicit Claude executable path with `--claude-bin`
  because launchd uses a restricted default PATH.
- `launchctl` errors should surface the actual stderr/stdout text.
- Non-macOS environments should fail clearly instead of pretending the daemon is
  running.

## Documentation Standards

- Keep public docs honest about limits and risks.
- Do not include local queue contents, Claude transcripts, session IDs from real
  users, or private repository paths unless they are synthetic examples.
- Update `README.md`, `docs/DEVELOPMENT_COMMANDS.md`, and `AGENTS.md` when CLI
  commands change.

## Commit Standards

Use conventional commits:

- `feat(cli): add command`
- `fix(queue): correct limit classification`
- `fix(launchd): preserve daemon environment`
- `docs(dwp): update agent onboarding`
- `test(claude): cover resume timeout`
- `chore(package): adjust metadata`
