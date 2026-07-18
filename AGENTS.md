# tokenmaxx Agent Guide

`tokenmaxx` is a Python CLI package that watches local Claude Code and Codex
sessions and resumes only sessions with terminal provider-authored limit
signals. It does not bypass provider limits or pass bypass flags. It waits,
retries later, and stops after bounded attempts.

This file is the first stop for agents. Keep it current when commands,
packaging, queue behavior, or the daemon model changes.

## Repository Structure

```text
.
|-- tokenmaxx/              # Python package and CLI implementation
|   |-- cli.py              # argparse commands and user-facing output
|   |-- claude.py           # Claude session metadata, transcripts, resume calls
|   |-- codex.py            # Codex rollouts, structured limits, resume calls
|   |-- transcript.py       # bounded JSONL tail and timestamp helpers
|   |-- runner.py           # shared subprocess timeout and result handling
|   |-- queue.py            # JSONL queue model, locking, output classification
|   |-- launchd.py          # macOS LaunchAgent plist and launchctl helpers
|   |-- config.py           # defaults and guarded resume prompt
|   |-- __main__.py         # python -m tokenmaxx entry point
|   `-- README.md           # package-level module guide
|-- tests/                  # unittest coverage for queue, CLI, launchd, resume
|-- docs/                   # product, architecture, testing, security, agent docs
|-- .agents/                # cross-agent skills, commands, personas, catalogs
|-- .dwp/                   # gitignored Deep Work Plan output
|-- tmp/                    # gitignored scratch space
|-- pyproject.toml          # setuptools package metadata and console script
|-- README.md               # user-facing project documentation
|-- SECURITY.md             # public vulnerability reporting policy
`-- CONTRIBUTING.md         # contributor workflow
```

## Documentation Index

| File | Purpose |
| --- | --- |
| [README.md](README.md) | User-facing overview, install, safety model, and command reference. |
| [docs/README.md](docs/README.md) | Index for the repository documentation set. |
| [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md) | Non-technical product purpose, users, success criteria, and non-goals. |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, data flow, package layout, and daemon shape. |
| [docs/STANDARDS.md](docs/STANDARDS.md) | Coding, CLI, persistence, error handling, and documentation standards. |
| [docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md) | Test framework, test patterns, coverage expectations, and validation gates. |
| [docs/DEVELOPMENT_COMMANDS.md](docs/DEVELOPMENT_COMMANDS.md) | Exact local commands for install, test, syntax, smoke checks, and release prep. |
| [docs/SECURITY.md](docs/SECURITY.md) | Local data boundaries, secrets rules, launchd risks, and agent security rules. |
| [docs/PERFORMANCE.md](docs/PERFORMANCE.md) | Queue, transcript, subprocess, and daemon performance expectations. |
| [docs/AI_AGENT_ONBOARDING.md](docs/AI_AGENT_ONBOARDING.md) | First-session checklist for an agent new to tokenmaxx. |
| [docs/AI_AGENT_COLLAB.md](docs/AI_AGENT_COLLAB.md) | Handoff, ownership, and multi-agent collaboration rules. |
| [docs/PR_REVIEW_WORKFLOW.md](docs/PR_REVIEW_WORKFLOW.md) | Review gates and PR expectations for this package. |
| [docs/ECOSYSTEM_CONTEXT.md](docs/ECOSYSTEM_CONTEXT.md) | How tokenmaxx fits next to Claude Code and adjacent usage tools. |
| [tokenmaxx/README.md](tokenmaxx/README.md) | Package module guide for implementation work. |
| [.agents/docs/skills_agents_catalog.md](.agents/docs/skills_agents_catalog.md) | Catalog of installed skills and agent personas. |
| [.agents/docs/COMMANDS_REFERENCE.md](.agents/docs/COMMANDS_REFERENCE.md) | Cross-agent command reference. |

## Mandatory Rules

- Write code, comments, docs, commit messages, and CLI text in English.
- Preserve the tool's safety boundary: tokenmaxx must never bypass Claude Code,
  Codex, Anthropic, OpenAI, or provider limits, and must never pass sandbox,
  approval, permission, or other bypass flags. It can only detect limit output,
  wait, retry, and stop after configured attempts.
- Keep queue state appendable and inspectable. The queue format is JSONL at
  `~/.tokenmaxx/queue.jsonl` by default, and writes must stay protected by the
  sibling lock file. Never DELETE queue rows to cancel a session — auto-queue
  dedupes against rows still present, so deletion re-arms it; `drop` tombstones
  (status `blocked`, reason "dropped by user") for exactly this reason.
- Claude auto-queue decides "limited" only from synthetic assistant records
  (`"model": "<synthetic>"`) in the transcript, never from raw transcript text —
  regular messages, tool output, and file contents routinely *mention* limit
  phrases without the session being limited. When Claude ships a new limit
  banner wording, add it to `LIMIT_MARKERS` with a test using the exact text.
- Codex auto-queue trusts only terminal provider-authored usage-limit errors
  and exhausted rate-limit telemetry with a future reset from rollout
  `event_msg` records; known remote-compaction disconnect records from the
  bounded Codex history tail; and thread-scoped rows from the read-only Codex
  logs database with `target == "codex_core::session::turn"` and bodies ending
  exactly
  `Turn error: Selected model is at capacity. Please try a different model.`
  Newer rollout task activity suppresses history and capacity events.
  Generic failures and matching text in user, assistant, tool, history, or file
  content remain untrusted for model-capacity inference.
- Existing `done`/`blocked` rows re-arm only on a limit banner NEWER than the
  row's `updatedAt`; rows with `blocked_reason` "dropped by user" never re-arm.
- `watch` must never resume an active provider session; it uses Claude process
  state or Codex rollout task state and defers instead.
- Preserve the shared `queue.jsonl.resume.lock`: only one continuation may run
  globally across providers and concurrent watchers. Provider/session identity
  is the composite `(provider, sessionId)`; legacy rows without `provider` load
  as Claude.
- Keep daemon behavior explicit. `launchd-install` and `launchd-uninstall`
  remain review-first helpers; `start` and `stop` are the commands that load or
  unload launchd. Launchd plists must record every available explicit
  `--claude-bin` and `--codex-bin` path AND embed the invoking shell's `PATH` in
  `EnvironmentVariables`, because
  launchd starts agents with a bare system PATH and version-manager shims
  (asdf, mise) exec their manager binary from PATH.
- Do not add runtime dependencies without a clear packaging reason. The current
  package has no runtime dependencies and should stay easy to install.
- Do not store secrets in this repo, docs, logs, tests, or `.dwp/`. Claude
  transcripts, Codex rollouts, and local session metadata can contain sensitive
  project context. Codex fixtures must use synthetic records, IDs, and paths.
- Use conventional commits: `feat(cli): ...`, `fix(launchd): ...`,
  `docs(dwp): ...`, `test(queue): ...`, `chore(package): ...`.
- Add or update tests for behavior changes. The test convention is `unittest`
  in `tests/test_tokenmaxx.py` and provider-focused `tests/test_codex.py`.
- Run the validation gate before claiming completion:
  `python3 -m unittest discover -s tests -v` and
  `PYTHONPYCACHEPREFIX=/tmp/tokenmaxx-pycache python3 -m py_compile tokenmaxx/*.py tests/*.py`.
- Keep temporary output in `tmp/`. Keep structured DWP plans in `.dwp/`.
  Both directories are gitignored and must not become release artifacts.

## Quick Commands

| Task | Command |
| --- | --- |
| Show package version | `python3 -m tokenmaxx --version` |
| Run tests | `python3 -m unittest discover -s tests -v` |
| Syntax check without repo-local bytecode | `PYTHONPYCACHEPREFIX=/tmp/tokenmaxx-pycache python3 -m py_compile tokenmaxx/*.py tests/*.py` |
| Dry-run queue processing | `python3 -m tokenmaxx watch --once --dry-run --no-auto-queue --queue /tmp/tokenmaxx-smoke-queue.jsonl` |
| Show CLI help | `python3 -m tokenmaxx --help` |
| Preview launchd plist | `python3 -m tokenmaxx launchd-install --dry-run --claude-bin /usr/local/bin/claude --codex-bin /opt/homebrew/bin/codex` |
| Install from checkout with uv | `uv tool install --force .` |
| Build/install smoke in a venv | `python3 -m venv /tmp/tokenmaxx-venv && /tmp/tokenmaxx-venv/bin/pip install . && /tmp/tokenmaxx-venv/bin/tokenmaxx --version` |
| Check git diff hygiene | `git diff --check` |
| DWP conformance check | `bash .agents/skills/deepworkplan/verify/conformance.sh --repo-only` |

No dedicated lint, formatter, or type-check command is configured yet. Until
one is added, use the syntax check plus unit tests as the required gate.

## Deep Work Plan Usage

Use the installed DeepWorkPlan skill for structured work:

- `/dwp-create <goal>` to create a gated plan.
- `/dwp-execute` to run the next plan task.
- `/dwp-status` to report progress without changing files.
- `/dwp-resume` to continue an interrupted plan.
- `/dwp-verify` to check repository conformance.

Plans and drafts live under `.dwp/`, which is intentionally ignored by git.

## Working Boundaries

- This repository is the standalone public package at
  `https://github.com/akle/tokenmaxx`.
- Do not push tokenmaxx work to any Pulpo organization remote.
- Ignore unrelated local provider queues, transcripts, rollouts, logs, and
  launchd state unless the task explicitly asks for runtime diagnosis.
- If a task touches the installed user daemon, verify live state with
  `tokenmaxx status`, `tokenmaxx logs`, and `launchctl print`.
