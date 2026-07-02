# tokenmaxx Agent Guide

`tokenmaxx` is a Python CLI package that watches local Claude Code session
metadata and resumes only sessions whose transcripts show a usage, rate, credit,
or session limit. It does not bypass provider limits. It waits, retries later,
and stops after bounded attempts.

This file is the first stop for agents. Keep it current when commands,
packaging, queue behavior, or the daemon model changes.

## Repository Structure

```text
.
|-- tokenmaxx/              # Python package and CLI implementation
|   |-- cli.py              # argparse commands and user-facing output
|   |-- claude.py           # Claude session metadata, transcripts, resume calls
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
- Preserve the tool's safety boundary: tokenmaxx must never bypass Claude,
  Anthropic, or provider limits. It can only detect limit output, wait, retry,
  and stop after configured attempts.
- Keep queue state appendable and inspectable. The queue format is JSONL at
  `~/.tokenmaxx/queue.jsonl` by default, and writes must stay protected by the
  sibling lock file. Never DELETE queue rows to cancel a session — auto-queue
  dedupes against rows still present, so deletion re-arms it; `drop` tombstones
  (status `blocked`, reason "dropped by user") for exactly this reason.
- Auto-queue decides "limited" only from synthetic assistant records
  (`"model": "<synthetic>"`) in the transcript, never from raw transcript text —
  regular messages, tool output, and file contents routinely *mention* limit
  phrases without the session being limited.
- `watch` must never resume a session that is still active in a live Claude
  Code process (busy, or recently updated with an alive pid); it defers instead.
- Keep daemon behavior explicit. `launchd-install` and `launchd-uninstall`
  remain review-first helpers; `start` and `stop` are the commands that load or
  unload launchd. Launchd plists must record an explicit `--claude-bin` path
  AND embed the invoking shell's `PATH` in `EnvironmentVariables`, because
  launchd starts agents with a bare system PATH and version-manager shims
  (asdf, mise) exec their manager binary from PATH.
- Do not add runtime dependencies without a clear packaging reason. The current
  package has no runtime dependencies and should stay easy to install.
- Do not store secrets in this repo, docs, logs, tests, or `.dwp/`. Claude
  transcripts and local session metadata can contain sensitive project context.
- Use conventional commits: `feat(cli): ...`, `fix(launchd): ...`,
  `docs(dwp): ...`, `test(queue): ...`, `chore(package): ...`.
- Add or update tests for behavior changes. The current test convention is
  `unittest` in `tests/test_tokenmaxx.py`; split files only when the suite
  becomes hard to scan.
- Run the validation gate before claiming completion:
  `python3 -m unittest discover -s tests -v` and
  `PYTHONPYCACHEPREFIX=/tmp/tokenmaxx-pycache python3 -m py_compile tokenmaxx/*.py tests/test_tokenmaxx.py`.
- Keep temporary output in `tmp/`. Keep structured DWP plans in `.dwp/`.
  Both directories are gitignored and must not become release artifacts.

## Quick Commands

| Task | Command |
| --- | --- |
| Show package version | `python3 -m tokenmaxx --version` |
| Run tests | `python3 -m unittest discover -s tests -v` |
| Syntax check without repo-local bytecode | `PYTHONPYCACHEPREFIX=/tmp/tokenmaxx-pycache python3 -m py_compile tokenmaxx/*.py tests/test_tokenmaxx.py` |
| Dry-run queue processing | `python3 -m tokenmaxx watch --once --dry-run --no-auto-queue --queue /tmp/tokenmaxx-smoke-queue.jsonl` |
| Show CLI help | `python3 -m tokenmaxx --help` |
| Preview launchd plist | `python3 -m tokenmaxx launchd-install --dry-run --claude-bin /usr/local/bin/claude` |
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
- Ignore unrelated local Claude queues, logs, and launchd state unless the task
  explicitly asks for runtime diagnosis.
- If a task touches the installed user daemon, verify live state with
  `tokenmaxx status`, `tokenmaxx logs`, and `launchctl print`.
