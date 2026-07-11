# Codex Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically discover and resume terminally rate-limited Codex sessions alongside Claude Code sessions.

**Architecture:** Keep one provider-neutral queue and scheduler, with explicit Claude and Codex modules for discovery, activity checks, prompts, and commands. Add small shared helpers for queue identity, bounded JSONL tails, and subprocess execution; dispatch providers explicitly in the CLI rather than adding a plugin framework.

**Tech Stack:** Python 3.10+, standard library only, `unittest`, JSONL state, macOS launchd.

## Global Constraints

- Existing queue rows without `provider` must load as `claude`.
- Queue identity is exactly `(provider, session_id)`.
- Only provider-authored terminal limit events may be auto-queued.
- The Codex structured allowlist initially contains only `usage_limit_exceeded`.
- Never pass Codex sandbox or approval bypass flags.
- At most one continuation runs at a time across all providers.
- Either provider may be absent; startup fails only when both CLIs are absent.
- No runtime dependencies may be added.

---

### Task 1: Provider-aware queue identity

**Files:**
- Modify: `tokenmaxx/queue.py`
- Modify: `tokenmaxx/claude.py`
- Test: `tests/test_tokenmaxx.py`

**Interfaces:**
- Produces: `SUPPORTED_PROVIDERS: tuple[str, ...]`
- Produces: `QueueItem.provider: str`, default `"claude"`
- Produces: `QueueItem.key -> tuple[str, str]`
- Produces: `apply_limit_event(items, *, provider, session_id, cwd, hit_at, now) -> QueueItem | None`

- [ ] **Step 1: Write failing migration and composite-key tests**

Add tests that load a legacy row, round-trip a Codex row, reject an unknown
provider, and merge only the matching provider:

```python
def test_queue_provider_migration_and_composite_identity(self):
    self.queue_path.write_text('{"cwd":"/tmp/r","sessionId":"same"}\n')
    legacy = load_queue(self.queue_path)[0]
    self.assertEqual(legacy.provider, "claude")
    self.assertEqual(legacy.key, ("claude", "same"))

    rows = [
        QueueItem(cwd="/tmp/r", session_id="same", provider="codex"),
        QueueItem(cwd="/tmp/r", session_id="same", provider="claude"),
    ]
    merge_resumed_item(
        rows,
        QueueItem(cwd="/tmp/r", session_id="same", provider="claude", status="done"),
    )
    self.assertEqual([row.status for row in rows], ["pending", "done"])

    self.queue_path.write_text('{"cwd":"/tmp/r","sessionId":"x","provider":"other"}\n')
    with self.assertRaisesRegex(ValueError, "unsupported provider"):
        load_queue(self.queue_path)
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `python3 -m unittest tests.test_tokenmaxx.TokenmaxxTests.test_queue_provider_migration_and_composite_identity -v`

Expected: FAIL because `QueueItem` has no `provider` or `key`.

- [ ] **Step 3: Add provider validation, serialization, and shared limit-event application**

Implement the queue contract:

```python
SUPPORTED_PROVIDERS = ("claude", "codex")

@dataclass
class QueueItem:
    cwd: str
    session_id: str
    provider: str = "claude"
    status: str = "pending"
    next_attempt_at: int = 0
    attempts: int = 0
    last_output: str = ""
    blocked_reason: str = ""
    created_at: int = 0
    updated_at: int = 0

    def __post_init__(self) -> None:
        if self.provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"unsupported provider: {self.provider}")
        now = int(time.time())
        self.next_attempt_at = int(self.next_attempt_at or 0)
        self.attempts = int(self.attempts or 0)
        self.created_at = int(self.created_at or now)
        self.updated_at = int(self.updated_at or self.created_at)

    @property
    def key(self) -> tuple[str, str]:
        return self.provider, self.session_id
```

Read `provider` with a default of `claude`, write it in `to_dict`, compare
`existing.key == updated.key` in `merge_resumed_item`, and add:

```python
def apply_limit_event(
    items: list[QueueItem],
    *,
    provider: str,
    session_id: str,
    cwd: str,
    hit_at: int,
    now: int,
) -> QueueItem | None:
    key = (provider, session_id)
    existing = next((item for item in reversed(items) if item.key == key), None)
    if existing is not None and (
        existing.status == "pending" or existing.blocked_reason == "dropped by user"
    ):
        return None
    if existing is None:
        item = QueueItem(cwd=cwd, session_id=session_id, provider=provider)
        items.append(item)
        return item
    if hit_at <= existing.updated_at:
        return None
    existing.status = "pending"
    existing.attempts = 0
    existing.next_attempt_at = 0
    existing.blocked_reason = ""
    existing.last_output = ""
    existing.updated_at = now
    return existing
```

Update `claude.build_limited_queue_items` to call `apply_limit_event` with
`provider="claude"`, preserving its age and transcript checks.

- [ ] **Step 4: Run queue and Claude autoqueue tests**

Run: `python3 -m unittest tests.test_tokenmaxx -v`

Expected: all existing tests plus the new provider tests PASS.

- [ ] **Step 5: Commit**

```bash
git add tokenmaxx/queue.py tokenmaxx/claude.py tests/test_tokenmaxx.py
git commit -m "feat(queue): add provider-aware session identity"
```

---

### Task 2: Bounded transcript reading and Codex session discovery

**Files:**
- Create: `tokenmaxx/transcript.py`
- Create: `tokenmaxx/codex.py`
- Create: `tests/test_codex.py`
- Modify: `tokenmaxx/claude.py`
- Modify: `tokenmaxx/config.py`

**Interfaces:**
- Produces: `transcript.tail_records(path: Path, max_lines: int = 80) -> list[dict]`
- Produces: `transcript.record_timestamp(record: dict) -> int`
- Produces: `codex.load_codex_sessions(sessions_dir: Path, *, now: int, max_session_age_hours: float) -> list[dict]`
- Produces: `config.default_codex_sessions_dir() -> Path`

- [ ] **Step 1: Write failing bounded-tail and Codex metadata tests**

Create `tests/test_codex.py` with synthetic JSONL only:

```python
import json
import os
import tempfile
import unittest
from pathlib import Path

from tokenmaxx import codex
from tokenmaxx.transcript import tail_records


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write_rollout(self, session_id="codex-1", cwd="/tmp/repo", records=(), mtime=1000):
        path = self.root / "2026" / "07" / "11" / f"rollout-{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {"type": "session_meta", "timestamp": "1970-01-01T00:16:40Z", "payload": {"id": session_id, "cwd": cwd}}
        path.write_text("\n".join(json.dumps(row) for row in (meta, *records)) + "\n")
        os.utime(path, (mtime, mtime))
        return path

    def test_tail_records_returns_only_requested_valid_dicts(self):
        path = self.root / "tail.jsonl"
        path.write_text('{"n":1}\nnot-json\n{"n":2}\n{"n":3}\n')
        self.assertEqual(tail_records(path, max_lines=2), [{"n": 2}, {"n": 3}])

    def test_load_codex_sessions_reads_recent_metadata(self):
        recent = self.write_rollout()
        self.write_rollout(session_id="old", mtime=900)
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=0.02)
        self.assertEqual([row["sessionId"] for row in sessions], ["codex-1"])
        self.assertEqual(sessions[0]["cwd"], "/tmp/repo")
        self.assertEqual(sessions[0]["updatedAt"], 1_000_000)
        self.assertEqual(sessions[0]["_path"], str(recent))
```

- [ ] **Step 2: Run tests and verify import failure**

Run: `python3 -m unittest tests.test_codex -v`

Expected: ERROR because `tokenmaxx.codex` and `tokenmaxx.transcript` do not exist.

- [ ] **Step 3: Implement the bounded JSONL helper and move Claude's generic helpers**

Create `tokenmaxx/transcript.py` with a backward seek so large rollouts are not
fully loaded:

```python
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path


def tail_records(path: Path, max_lines: int = 80) -> list[dict]:
    if max_lines <= 0:
        return []
    chunks: list[bytes] = []
    newline_count = 0
    with Path(path).open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        while position > 0 and newline_count <= max_lines:
            size = min(8192, position)
            position -= size
            handle.seek(position)
            chunk = handle.read(size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
    lines = b"".join(reversed(chunks)).decode(errors="replace").splitlines()[-max_lines:]
    records: list[dict] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def record_timestamp(record: dict) -> int:
    raw = record.get("timestamp")
    if isinstance(raw, str):
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return 0
```

Import these functions in `claude.py` and remove its duplicate implementations.

- [ ] **Step 4: Implement recent Codex metadata discovery**

Create `tokenmaxx/codex.py` with `load_codex_sessions`. For each recent
`**/*.jsonl`, read records from the beginning until `session_meta`, require
`payload.id` and `payload.cwd`, attach `sessionId`, `cwd`, `updatedAt` from
`stat().st_mtime`, and `_path`. Skip `FileNotFoundError`, malformed JSON, and
invalid metadata. Sort descending by `updatedAt`.

Add to `config.py`:

```python
def default_codex_sessions_dir() -> Path:
    return Path.home() / ".codex" / "sessions"
```

- [ ] **Step 5: Run provider discovery and regression tests**

Run: `python3 -m unittest tests.test_codex tests.test_tokenmaxx -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tokenmaxx/transcript.py tokenmaxx/codex.py tokenmaxx/claude.py tokenmaxx/config.py tests/test_codex.py
git commit -m "feat(codex): discover recent local sessions"
```

---

### Task 3: Conservative Codex limit detection and headless resume

**Files:**
- Create: `tokenmaxx/runner.py`
- Modify: `tokenmaxx/codex.py`
- Modify: `tokenmaxx/claude.py`
- Modify: `tokenmaxx/config.py`
- Modify: `tokenmaxx/queue.py`
- Modify: `tests/test_codex.py`
- Modify: `tests/test_tokenmaxx.py`

**Interfaces:**
- Produces: `codex.session_limit_hit_at(session: dict) -> int | None`
- Produces: `codex.build_limited_queue_items(sessions, items, *, now, max_session_age_hours) -> list[QueueItem]`
- Produces: `codex.find_active_session(sessions, session_id, now, grace_seconds) -> dict | None`
- Produces: `codex.build_resume_command(item, codex_bin, prompt=CODEX_PROMPT) -> list[str]`
- Produces: `codex.run_due_item(...) -> QueueItem`
- Produces: `runner.run_due_command(...) -> QueueItem`

- [ ] **Step 1: Write failing terminal-signal tests**

Add synthetic event constructors and tests:

```python
def event(event_type, timestamp, **payload):
    return {"type": "event_msg", "timestamp": timestamp, "payload": {"type": event_type, **payload}}

def test_terminal_usage_limit_is_queued_but_generic_error_is_not(self):
    limited_path = self.write_rollout(records=(
        event("task_started", "1970-01-01T00:15:00Z"),
        event("error", "1970-01-01T00:16:20Z", codex_error_info="usage_limit_exceeded", message="You've hit your usage limit. Try again at 12:52 AM."),
        event("task_complete", "1970-01-01T00:16:21Z"),
    ))
    limited = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)[0]
    self.assertEqual(codex.session_limit_hit_at(limited), 980)

    generic_path = self.write_rollout(session_id="generic", records=(
        event("error", "1970-01-01T00:16:20Z", codex_error_info="bad_request", message="usage limit mentioned by a file"),
    ))
    generic = next(row for row in codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1) if row["sessionId"] == "generic")
    self.assertIsNone(codex.session_limit_hit_at(generic))
```

Add a second test where `task_started` occurs after the old usage error and
assert `None`, plus an active-state test where the newest terminal state is
`task_started` without `task_complete`.

- [ ] **Step 2: Write failing resume-command and Codex reset-time tests**

```python
def test_codex_dry_run_uses_exec_resume(self):
    item = QueueItem(cwd="/tmp/repo", session_id="codex-1", provider="codex")
    result = codex.run_due_item(
        item,
        now=1000,
        codex_bin="codex",
        dry_run=True,
        retry_delay_seconds=18_000,
        followup_delay_seconds=900,
        max_attempts=3,
        resume_timeout_seconds=7200,
    )
    self.assertIn("DRY RUN: codex exec resume codex-1", result.last_output)
```

In `test_classify_output_and_retry_updates`, add a local-time assertion for
`"You've hit your usage limit. Try again at 12:52 AM."` and expect 12:53 AM
with the existing 60-second buffer.

- [ ] **Step 3: Run focused tests and verify failure**

Run: `python3 -m unittest tests.test_codex tests.test_tokenmaxx.TokenmaxxTests.test_classify_output_and_retry_updates -v`

Expected: FAIL because Codex limit/resume functions and `try again at` parsing
are absent.

- [ ] **Step 4: Implement terminal detection and activity checks**

In `codex.py`, recognize only error records with
`codex_error_info == "usage_limit_exceeded"`, or an absent code plus a message
starting exactly with `"You've hit your usage limit."`. Walk event records
backward; ignore `task_complete`, return the error timestamp for the allowlisted
error, return `None` for another error, and stop with `None` at a newer
`task_started`.

Derive activity from the newest `task_started`/`task_complete` event and expire
it after 24 hours. Build limited rows through `apply_limit_event` with
`provider="codex"`.

- [ ] **Step 5: Extract shared subprocess execution and add Codex command construction**

Move Claude's `dry_run_output`, process-group termination, and subprocess
execution into `runner.py`. Add:

```python
def run_due_command(
    item: QueueItem,
    command: list[str],
    *,
    provider_name: str,
    now: int,
    dry_run: bool,
    retry_delay_seconds: int,
    followup_delay_seconds: int,
    max_attempts: int,
    resume_timeout_seconds: int,
) -> QueueItem:
    if not is_due(item, now):
        return item
    if dry_run:
        item.last_output = dry_run_output(command)
        item.updated_at = now
        return item
    returncode, output = run_resume_command(
        command,
        cwd=item.cwd,
        timeout_seconds=resume_timeout_seconds,
        provider_name=provider_name,
    )
    if returncode != 0 and not output:
        output = f"{provider_name} exited with code {returncode}"
    return update_item_after_output(
        item,
        output,
        now=now,
        retry_delay_seconds=retry_delay_seconds,
        followup_delay_seconds=followup_delay_seconds,
        max_attempts=max_attempts,
    )
```

Claude and Codex wrappers build their commands and call this function. Codex's
command is `[codex_bin, "exec", "resume", item.session_id, prompt]`.

Add `CODEX_PROMPT` in `config.py`, requiring the exact completion marker
`STATUS: DONE` and no dangerous CLI flags.

- [ ] **Step 6: Extend reset parsing for Codex wording**

Change the reset regex prefix in `queue.reset_time_from_output` from only
`resets?` to:

```python
r"(?:resets?|try\s+again\s+at)\s+"
```

Keep existing date, time-zone, horizon, rollover, and reset-buffer behavior.

- [ ] **Step 7: Run all unit tests**

Run: `python3 -m unittest discover -s tests -v`

Expected: PASS, including the existing process-timeout regression test after
updating its patch target to `tokenmaxx.runner.subprocess.Popen`.

- [ ] **Step 8: Commit**

```bash
git add tokenmaxx/runner.py tokenmaxx/codex.py tokenmaxx/claude.py tokenmaxx/config.py tokenmaxx/queue.py tests/test_codex.py tests/test_tokenmaxx.py
git commit -m "feat(codex): detect and resume limited sessions"
```

---

### Task 4: Mixed-provider CLI and scheduler

**Files:**
- Modify: `tokenmaxx/cli.py`
- Modify: `tests/test_tokenmaxx.py`

**Interfaces:**
- Produces: `resolve_provider_bin(provider: str, requested: str | None = None) -> str | None`
- Produces: `enabled_provider_bins(args) -> dict[str, str]`
- Changes: `scan`, `add`, `autoqueue`, `drop`, `status`, and `watch` support both providers.

- [ ] **Step 1: Extend the test fixture and write mixed-provider CLI tests**

Add `codex_sessions_dir`, `codex_bin`, and `provider` to `self.args`. Add tests
that assert:

```python
def test_status_qualifies_mixed_provider_rows(self):
    append_queue_item(self.queue_path, QueueItem(cwd="/tmp/c", session_id="same", provider="claude"))
    append_queue_item(self.queue_path, QueueItem(cwd="/tmp/x", session_id="same", provider="codex"))
    output = io.StringIO()
    with redirect_stdout(output):
        cli.cmd_status(self.args())
    self.assertRegex(output.getvalue(), r"PROVIDER\s+SESSION")
    self.assertIn("claude", output.getvalue())
    self.assertIn("codex", output.getvalue())

def test_drop_provider_filter_targets_one_matching_id(self):
    append_queue_item(self.queue_path, QueueItem(cwd="/tmp/c", session_id="same", provider="claude"))
    append_queue_item(self.queue_path, QueueItem(cwd="/tmp/x", session_id="same", provider="codex"))
    with redirect_stdout(io.StringIO()):
        code = cli.cmd_drop(self.args(session_id="same", provider="codex"))
    self.assertEqual(code, 0)
    rows = {item.provider: item for item in load_queue(self.queue_path)}
    self.assertEqual(rows["claude"].status, "pending")
    self.assertEqual(rows["codex"].status, "blocked")
```

Add an integration test with one due Codex row and assert the dry-run output
contains `codex exec resume`, while an existing Claude dry-run still contains
`claude --resume`.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python3 -m unittest tests.test_tokenmaxx.TokenmaxxTests.test_status_qualifies_mixed_provider_rows tests.test_tokenmaxx.TokenmaxxTests.test_drop_provider_filter_targets_one_matching_id -v`

Expected: FAIL because the CLI has no provider column or filter.

- [ ] **Step 3: Add explicit provider dispatch**

Import `codex` as a module and keep Claude imports explicit. Implement provider
branches for session loading, limit-item building, activity checks, and resume:

```python
def run_resume(args, item: QueueItem, now: int, bins: dict[str, str]) -> QueueItem:
    common = dict(
        now=now,
        dry_run=args.dry_run,
        retry_delay_seconds=args.retry_delay_seconds,
        followup_delay_seconds=args.followup_delay_seconds,
        max_attempts=args.max_attempts,
        resume_timeout_seconds=args.resume_timeout_seconds,
    )
    if item.provider == "claude":
        return claude.run_due_item(item, claude_bin=bins["claude"], **common)
    if item.provider == "codex":
        return codex.run_due_item(item, codex_bin=bins["codex"], **common)
    raise ValueError(f"unsupported provider: {item.provider}")
```

Resolve missing executables with `shutil.which`, defer due rows whose provider
CLI is unavailable, and use only enabled providers for daemon autoqueue.
Standalone `autoqueue` scans both providers.

- [ ] **Step 4: Update command arguments and display**

Add `--codex-sessions-dir` to common arguments, `--provider
{claude,codex}` to `add` and `drop`, `--codex-bin` to `watch`, and a
`PROVIDER` column to scan/status. Preserve `provider="claude"` for manual add
when the flag is omitted.

Prefix resume/defer log identifiers as `<provider>:<8-char-session>` so mixed
logs remain unambiguous.

- [ ] **Step 5: Run all tests and a dry-run smoke command**

Run: `python3 -m unittest discover -s tests -v`

Expected: PASS.

Run: `python3 -m tokenmaxx watch --once --dry-run --no-auto-queue --queue /tmp/tokenmaxx-codex-plan-smoke.jsonl`

Expected: `No due items.`

- [ ] **Step 6: Commit**

```bash
git add tokenmaxx/cli.py tests/test_tokenmaxx.py
git commit -m "feat(cli): operate one queue across Claude and Codex"
```

---

### Task 5: Launchd executable pinning for both providers

**Files:**
- Modify: `tokenmaxx/launchd.py`
- Modify: `tokenmaxx/cli.py`
- Modify: `tests/test_tokenmaxx.py`

**Interfaces:**
- Changes: `build_launchd_plist(..., claude_bin: str | None, codex_bin: str | None, ...) -> str`
- Changes: `start` and `launchd-install` require at least one resolved provider executable.

- [ ] **Step 1: Write failing launchd and startup tests**

Extend the plist test with `codex_bin="/opt/homebrew/bin/codex"` and assert both
absolute paths appear. Add startup tests where only Codex resolves (success)
and neither resolves (exit 1 with `Neither claude nor codex is on PATH`).

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python3 -m unittest tests.test_tokenmaxx.TokenmaxxTests.test_build_launchd_plist_contains_watch_command tests.test_tokenmaxx.TokenmaxxTests.test_start_writes_plist_and_loads_service -v`

Expected: FAIL because `codex_bin` is unsupported.

- [ ] **Step 3: Make provider paths optional and write every available one**

Build arguments in this order:

```python
arguments = [program, "watch"]
if claude_bin:
    arguments.extend(["--claude-bin", str(Path(claude_bin).expanduser())])
if codex_bin:
    arguments.extend(["--codex-bin", str(Path(codex_bin).expanduser())])
arguments.extend(["--queue", str(Path(queue_path).expanduser()), "--sleep-seconds", str(interval_seconds)])
```

Also write `--codex-sessions-dir`. Keep the captured PATH environment because
version-manager shims may invoke their manager binaries.

- [ ] **Step 4: Resolve both CLIs in start/install and fail only when both are absent**

Use `resolve_provider_bin("claude", args.claude_bin)` and
`resolve_provider_bin("codex", args.codex_bin)`. Pass both optional paths to
the plist. Emit one direct error when both return `None`.

- [ ] **Step 5: Run tests and inspect a dry-run plist**

Run: `python3 -m unittest discover -s tests -v`

Expected: PASS.

Run: `python3 -m tokenmaxx launchd-install --dry-run --claude-bin /usr/local/bin/claude --codex-bin /opt/homebrew/bin/codex`

Expected: plist output contains both `--claude-bin` and `--codex-bin`.

- [ ] **Step 6: Commit**

```bash
git add tokenmaxx/launchd.py tokenmaxx/cli.py tests/test_tokenmaxx.py
git commit -m "feat(launchd): pin Claude and Codex executables"
```

---

### Task 6: Product documentation and alpha release metadata

**Files:**
- Modify: `README.md`
- Modify: `tokenmaxx/README.md`
- Modify: `docs/PRODUCT_SPEC.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/SECURITY.md`
- Modify: `docs/TESTING_GUIDE.md`
- Modify: `docs/PERFORMANCE.md`
- Modify: `docs/DEVELOPMENT_COMMANDS.md`
- Modify: `docs/ECOSYSTEM_CONTEXT.md`
- Modify: `AGENTS.md`
- Modify: `pyproject.toml`
- Modify: `tokenmaxx/__init__.py`

**Interfaces:**
- Produces: package version `0.5.0`
- Documents: default dual-provider behavior, Codex paths/signals/commands, queue migration, and launchd requirements.

- [ ] **Step 1: Update user-facing installation and operation documentation**

Change the one-line description to "Limit-aware resume queue for Claude Code
and Codex sessions." Document that normal `autoqueue`, `watch`, and `start`
cover both installed providers; show `PROVIDER` in status examples; add
`--provider codex` to manual add/drop examples; and document:

```text
~/.claude/sessions
~/.claude/projects
~/.codex/sessions
claude --resume <id> -p <prompt>
codex exec resume <id> <prompt>
```

State explicitly that only structured/provider-authored terminal limit errors
queue Codex sessions and that tokenmaxx never passes bypass flags.

- [ ] **Step 2: Update maintainer documentation and metadata**

Describe `codex.py`, `runner.py`, and `transcript.py` in architecture/testing
docs. Add Codex fixture guidance without private rollout data. Change
`pyproject.toml` description/keywords and both version declarations from
`0.4.0` to `0.5.0`.

- [ ] **Step 3: Run repository verification**

Run: `python3 -m unittest discover -s tests -v`

Expected: all tests PASS.

Run: `PYTHONPYCACHEPREFIX=/tmp/tokenmaxx-pycache python3 -m py_compile tokenmaxx/*.py tests/*.py`

Expected: no output and exit 0.

Run: `git diff --check`

Expected: no output and exit 0.

Run: `bash .agents/skills/deepworkplan/verify/conformance.sh --repo-only`

Expected: `CONFORMANT`.

- [ ] **Step 4: Commit**

```bash
git add README.md tokenmaxx/README.md docs AGENTS.md pyproject.toml tokenmaxx/__init__.py
git commit -m "docs: release dual-provider tokenmaxx 0.5.0"
```

---

### Task 7: Install and verify the live dual-provider daemon

**Files:**
- Runtime: `~/.local/bin/tokenmaxx`
- Runtime: `~/Library/LaunchAgents/com.local.tokenmaxx.plist`
- Runtime: `~/.tokenmaxx/queue.jsonl`
- Runtime: `~/.tokenmaxx/tokenmaxx.log`

**Interfaces:**
- Produces: a loaded LaunchAgent with absolute Claude and Codex executable paths.

- [ ] **Step 1: Confirm the source tree is clean and tests are green**

Run: `git status --short --branch`

Expected: clean `main` with local commits ahead of `origin/main`.

Run: `python3 -m unittest discover -s tests -v`

Expected: all tests PASS.

- [ ] **Step 2: Reinstall without cached build artifacts**

Run: `/Users/pep/.local/bin/uv tool install --force --reinstall --no-cache /Users/pep/dev/akle/tokenmaxx`

Expected: tokenmaxx 0.5.0 installed successfully.

- [ ] **Step 3: Reload launchd with the new arguments**

Run: `/Users/pep/.local/bin/tokenmaxx stop`

Expected: `Stopped com.local.tokenmaxx` or an already-stopped message.

Run: `/Users/pep/.local/bin/tokenmaxx start`

Expected: `Started com.local.tokenmaxx`.

- [ ] **Step 4: Verify live configuration and activity**

Run: `plutil -p /Users/pep/Library/LaunchAgents/com.local.tokenmaxx.plist`

Expected: `ProgramArguments` contains absolute `--claude-bin`, absolute
`--codex-bin`, and `--codex-sessions-dir`.

Run: `launchctl print gui/$(id -u)/com.local.tokenmaxx`

Expected: loaded job with `run interval = 300 seconds` and the same provider
arguments.

Run: `/Users/pep/.local/bin/tokenmaxx status`

Expected: loaded daemon and a provider-qualified queue table.

Run: `/Users/pep/.local/bin/tokenmaxx logs --lines 20`

Expected: a `tokenmaxx 0.5.0` startup line and no executable-not-found errors.

- [ ] **Step 5: Push only to the akle remote after final verification**

Run: `git remote -v`

Expected: push target is `https://github.com/akle/tokenmaxx.git`.

Run: `git push origin main`

Expected: `main` advances on `akle/tokenmaxx`.
