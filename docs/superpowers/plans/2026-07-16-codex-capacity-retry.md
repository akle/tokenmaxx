# Codex Model-Capacity Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Human ruling after task review (2026-07-16):** A qualifying fresh capacity
> event sets an already-pending item's retry to exactly `hit_at + 300`, even
> when its current retry is due or scheduled sooner; `attempts` and `updated_at`
> remain unchanged. Logs-path expansion, resolution, and URI construction must
> remain inside the loader's guarded failure boundary.

**Goal:** Queue an unfinished Codex thread from an exact provider-authored model-capacity log event and retry the same model after five minutes.

**Architecture:** Add a bounded, read-only SQLite event loader to the Codex provider. Merge its thread-scoped events into the existing queue/re-arm path, then plumb the database path through the CLI and launchd exactly like the existing Codex history path.

**Tech Stack:** Python 3.11 standard library (`sqlite3`, `pathlib`, `unittest`), JSONL rollouts, SQLite Codex logs, macOS launchd.

## Global Constraints

- Match only `target == "codex_core::session::turn"` rows whose body ends with `Turn error: Selected model is at capacity. Please try a different model.`
- Read `~/.codex/logs_2.sqlite` in SQLite read-only mode; never create, migrate, or mutate the database.
- Retry the same thread and model five minutes after the provider event; never pass a model override.
- For a qualifying fresh capacity event, replace any different pending retry schedule with exactly `hit_at + 300`, preserving `attempts` and `updated_at`.
- Never infer model capacity from `history.jsonl`, rollout user/assistant/tool content, or copied error text.
- Suppress an event when the rollout has newer `task_started` or `task_complete` activity.
- Preserve queue deduplication, user-drop tombstones, bounded attempts, global resume locking, and normal active-session deferral.
- Path-preparation failures and missing, locked, malformed, unreadable, or schema-incompatible databases produce no capacity events and do not stop the daemon.
- Add no runtime dependency.
- Follow TDD: every production behavior starts with a failing test that is observed failing for the expected reason.

---

## File Map

- `tokenmaxx/codex.py`: exact provider-log matching, read-only SQLite loading, event freshness, and five-minute retry scheduling.
- `tests/test_codex.py`: synthetic SQLite fixtures and provider-level trust, timing, suppression, re-arm, and failure tests.
- `tokenmaxx/config.py`: default Codex logs path and guarded prompt wording.
- `tokenmaxx/cli.py`: `--codex-logs-db` parsing and provider dispatch plumbing.
- `tokenmaxx/launchd.py`: persist the logs database path in daemon arguments.
- `tests/test_tokenmaxx.py`: CLI, launchd, autoqueue integration, and package-version coverage.
- `README.md`: user-facing detection and retry behavior.
- `docs/ARCHITECTURE.md`: trusted signal and data-flow details.
- `docs/SECURITY.md`: provider-log trust boundary and fail-open database handling.
- `tokenmaxx/__init__.py`, `pyproject.toml`: patch release `0.5.6`.

---

### Task 1: Detect exact Codex capacity events and schedule retries

**Files:**
- Modify: `tests/test_codex.py`
- Modify: `tokenmaxx/codex.py`

**Interfaces:**
- Produces: `load_model_capacity_events(logs_path: Path | None, session_ids: set[str], *, now: int, max_session_age_hours: float) -> dict[str, int]`.
- Produces: `MODEL_CAPACITY_RETRY_SECONDS = 300`.
- Produces: `reschedule_pending_capacity_item(...)` without resetting attempts.
- Extends: `build_limited_queue_items(..., logs_path: Path | None = None)`.
- Extends: `find_active_session(..., history_path: Path | None = None, logs_path: Path | None = None, max_session_age_hours: float = 24.0)`.
- Consumes: existing `session_has_newer_task_activity`, `apply_limit_event`, and `QueueItem` behavior.

- [ ] **Step 1: Add synthetic SQLite fixture support**

Add `import sqlite3` to `tests/test_codex.py` and this helper to `CodexTests`:

```python
    def write_logs_db(self, records):
        path = self.root / "logs_2.sqlite"
        with sqlite3.connect(path) as connection:
            connection.execute(
                """
                CREATE TABLE logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    ts_nanos INTEGER NOT NULL,
                    target TEXT NOT NULL,
                    thread_id TEXT,
                    feedback_log_body TEXT
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO logs (ts, ts_nanos, target, thread_id, feedback_log_body)
                VALUES (?, ?, ?, ?, ?)
                """,
                records,
            )
        return path
```

- [ ] **Step 2: Write failing exact-signal and retry-time tests**

Add these tests to `CodexTests`:

```python
    def test_exact_model_capacity_log_queues_same_thread_after_five_minutes(self):
        self.write_rollout()
        logs = self.write_logs_db(
            [
                (
                    980,
                    10,
                    "codex_core::session::turn",
                    "codex-1",
                    "session_loop{thread_id=codex-1}: Turn error: "
                    "Selected model is at capacity. Please try a different model.",
                )
            ]
        )
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)
        items = []

        affected = codex.build_limited_queue_items(
            sessions,
            items,
            now=1000,
            max_session_age_hours=1,
            logs_path=logs,
        )

        self.assertEqual(affected, items)
        self.assertEqual(items[0].key, ("codex", "codex-1"))
        self.assertEqual(items[0].next_attempt_at, 1280)

    def test_newer_rollout_activity_suppresses_model_capacity_log(self):
        self.write_rollout(records=(event("task_started", "1970-01-01T00:16:30Z"),))
        logs = self.write_logs_db(
            [
                (
                    980,
                    0,
                    "codex_core::session::turn",
                    "codex-1",
                    "Turn error: Selected model is at capacity. Please try a different model.",
                )
            ]
        )
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)
        items = []

        codex.build_limited_queue_items(
            sessions,
            items,
            now=1000,
            max_session_age_hours=1,
            logs_path=logs,
        )

        self.assertEqual(items, [])

    def test_model_capacity_stop_makes_open_rollout_turn_inactive(self):
        self.write_rollout(records=(event("task_started", "1970-01-01T00:16:10Z"),))
        logs = self.write_logs_db(
            [
                (
                    980,
                    0,
                    "codex_core::session::turn",
                    "codex-1",
                    "Turn error: Selected model is at capacity. Please try a different model.",
                )
            ]
        )
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)

        active = codex.find_active_session(
            sessions,
            "codex-1",
            1000,
            30,
            logs_path=logs,
            max_session_age_hours=1,
        )

        self.assertIsNone(active)
```

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```bash
PYTHONPYCACHEPREFIX=/tmp/tokenmaxx-pycache python3 -m unittest \
  tests.test_codex.CodexTests.test_exact_model_capacity_log_queues_same_thread_after_five_minutes \
  tests.test_codex.CodexTests.test_newer_rollout_activity_suppresses_model_capacity_log \
  tests.test_codex.CodexTests.test_model_capacity_stop_makes_open_rollout_turn_inactive -v
```

Expected: the queue tests error because `build_limited_queue_items()` does not
accept `logs_path`, and the activity test errors because `find_active_session()`
does not accept it.

- [ ] **Step 4: Implement the exact read-only capacity loader**

Add `import sqlite3` and these constants/functions to `tokenmaxx/codex.py`:

```python
MODEL_CAPACITY_LOG_TARGET = "codex_core::session::turn"
MODEL_CAPACITY_ERROR = "Selected model is at capacity. Please try a different model."
MODEL_CAPACITY_LOG_SUFFIX = f"Turn error: {MODEL_CAPACITY_ERROR}"
MODEL_CAPACITY_RETRY_SECONDS = 5 * 60


def load_model_capacity_events(
    logs_path: Path | None,
    session_ids: set[str],
    *,
    now: int,
    max_session_age_hours: float,
) -> dict[str, int]:
    if logs_path is None or not session_ids:
        return {}
    oldest = now - int(float(max_session_age_hours) * 60 * 60)
    events: dict[str, int] = {}
    try:
        database_uri = Path(logs_path).expanduser().resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(database_uri, uri=True, timeout=0)
        try:
            for session_id in session_ids:
                rows = connection.execute(
                    """
                    SELECT ts, feedback_log_body
                    FROM logs
                    WHERE thread_id = ?
                      AND ts BETWEEN ? AND ?
                      AND target = ?
                    ORDER BY ts DESC, ts_nanos DESC, id DESC
                    """,
                    (session_id, oldest, now, MODEL_CAPACITY_LOG_TARGET),
                )
                for raw_hit_at, body in rows:
                    if isinstance(body, str) and body.endswith(MODEL_CAPACITY_LOG_SUFFIX):
                        events[session_id] = int(raw_hit_at)
                        break
        finally:
            connection.close()
    except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
        return {}
    return events
```

Extend `build_limited_queue_items()` with `logs_path: Path | None = None`, load
events once for the discovered IDs, and merge them after the existing rollout
and remote-compaction candidates:

```python
    capacity_events = load_model_capacity_events(
        logs_path,
        {str(session.get("sessionId") or "") for session in sessions},
        now=now,
        max_session_age_hours=max_session_age_hours,
    )
```

Inside the session loop, after `compact_hit_at` handling, add:

```python
        capacity_retry_at = None
        capacity_hit_at = remote_compact_hit_at(session, capacity_events)
        if capacity_hit_at is not None and (info is None or capacity_hit_at > info[0]):
            info = (capacity_hit_at, None)
            capacity_retry_at = capacity_hit_at + MODEL_CAPACITY_RETRY_SECONDS
```

After `apply_limit_event()`, schedule capacity directly instead of using the
usage-limit reset buffer:

```python
        if item is not None:
            if capacity_retry_at is not None and capacity_retry_at > now:
                item.next_attempt_at = capacity_retry_at
            elif retry_at is not None and retry_at > now:
                item.next_attempt_at = retry_at + RESET_BUFFER_SECONDS
            affected.append(item)
```

This explicit branch is required: the existing usage-reset path adds
`RESET_BUFFER_SECONDS`, which would turn a five-minute capacity retry into six
minutes.

Add a provider-owned external-stop helper and extend the activity functions:

```python
def latest_external_stop_at(
    session: dict,
    *,
    history_path: Path | None,
    logs_path: Path | None,
    now: int,
    max_session_age_hours: float,
) -> int | None:
    session_id = str(session.get("sessionId") or "")
    compact_events = load_remote_compact_events(
        history_path,
        now=now,
        max_session_age_hours=max_session_age_hours,
    )
    capacity_events = load_model_capacity_events(
        logs_path,
        {session_id} if session_id else set(),
        now=now,
        max_session_age_hours=max_session_age_hours,
    )
    candidates = [
        hit_at
        for hit_at in (
            remote_compact_hit_at(session, compact_events),
            remote_compact_hit_at(session, capacity_events),
        )
        if hit_at is not None
    ]
    return max(candidates) if candidates else None
```

Extend `session_is_active()` with `external_stop_at: int | None = None` and add
this check before reading the latest task state:

```python
    if external_stop_at is not None and not session_has_newer_task_activity(
        session, external_stop_at
    ):
        return False
```

Extend `find_active_session()` with keyword-only `history_path`, `logs_path`,
and `max_session_age_hours` defaults. For the matching session, calculate
`external_stop_at = latest_external_stop_at(...)` and pass it into
`session_is_active()`.

- [ ] **Step 5: Run the focused tests and verify GREEN**

Run the Step 3 command again.

Expected: 3 tests pass.

- [ ] **Step 6: Write failing trust-boundary, resilience, and re-arm tests**

Add tests covering exact target/body/thread matching, stale rows, unreadable
databases and path-preparation failures, a newer event re-arming a resolved
item, and a repeated failure setting due, earlier, and later pending schedules
to the exact five-minute mark:

```python
    def test_model_capacity_loader_ignores_untrusted_stale_and_partial_rows(self):
        logs = self.write_logs_db(
            [
                (980, 0, "codex_core::session::handlers", "codex-1", codex.MODEL_CAPACITY_LOG_SUFFIX),
                (980, 0, "codex_core::session::turn", None, codex.MODEL_CAPACITY_LOG_SUFFIX),
                (980, 0, "codex_core::session::turn", "codex-1", codex.MODEL_CAPACITY_ERROR),
                (980, 0, "codex_core::session::turn", "codex-1", codex.MODEL_CAPACITY_LOG_SUFFIX + " copied"),
                (1, 0, "codex_core::session::turn", "codex-1", codex.MODEL_CAPACITY_LOG_SUFFIX),
            ]
        )

        self.assertEqual(
            codex.load_model_capacity_events(
                logs,
                {"codex-1"},
                now=1000,
                max_session_age_hours=0.01,
            ),
            {},
        )

    def test_model_capacity_loader_tolerates_missing_malformed_locked_and_bad_schema(self):
        malformed = self.root / "malformed.sqlite"
        malformed.write_text("not sqlite")
        wrong_schema = self.root / "wrong.sqlite"
        with sqlite3.connect(wrong_schema) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")

        for path in (self.root / "missing.sqlite", malformed, wrong_schema):
            with self.subTest(path=path):
                self.assertEqual(
                    codex.load_model_capacity_events(
                        path,
                        {"codex-1"},
                        now=1000,
                        max_session_age_hours=1,
                    ),
                    {},
                )

        for error in (PermissionError("unreadable"), sqlite3.OperationalError("database is locked")):
            with self.subTest(error=error), patch("tokenmaxx.codex.sqlite3.connect", side_effect=error):
                self.assertEqual(
                    codex.load_model_capacity_events(
                        self.root / "logs.sqlite",
                        {"codex-1"},
                        now=1000,
                        max_session_age_hours=1,
                    ),
                    {},
                )

    def test_model_capacity_loader_tolerates_path_preparation_failures(self):
        for error in (OSError("unreadable path"), RuntimeError("path resolution failed")):
            with self.subTest(error=error), patch(
                "tokenmaxx.codex.Path.resolve", side_effect=error
            ):
                self.assertEqual(
                    codex.load_model_capacity_events(
                        self.root / "logs.sqlite",
                        {"codex-1"},
                        now=1000,
                        max_session_age_hours=1,
                    ),
                    {},
                )

    def test_newer_model_capacity_event_rearms_resolved_item(self):
        self.write_rollout()
        logs = self.write_logs_db(
            [
                (
                    980,
                    0,
                    "codex_core::session::turn",
                    "codex-1",
                    codex.MODEL_CAPACITY_LOG_SUFFIX,
                )
            ]
        )
        items = [
            QueueItem(
                cwd="/tmp/repo",
                session_id="codex-1",
                provider="codex",
                status="blocked",
                attempts=5,
                blocked_reason="max attempts (5) reached",
                updated_at=900,
            )
        ]
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)

        codex.build_limited_queue_items(
            sessions,
            items,
            now=1000,
            max_session_age_hours=1,
            logs_path=logs,
        )

        self.assertEqual(items[0].status, "pending")
        self.assertEqual(items[0].attempts, 0)
        self.assertEqual(items[0].next_attempt_at, 1280)

    def test_model_capacity_event_sets_exact_pending_retry_without_resetting_attempts(self):
        self.write_rollout()
        logs = self.write_logs_db(
            [
                (
                    980,
                    0,
                    "codex_core::session::turn",
                    "codex-1",
                    codex.MODEL_CAPACITY_LOG_SUFFIX,
                )
            ]
        )
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=1)

        for existing_retry in (0, 1200, 1880):
            with self.subTest(existing_retry=existing_retry):
                items = [
                    QueueItem(
                        cwd="/tmp/repo",
                        session_id="codex-1",
                        provider="codex",
                        status="pending",
                        attempts=2,
                        next_attempt_at=existing_retry,
                        updated_at=980,
                    )
                ]

                affected = codex.build_limited_queue_items(
                    sessions,
                    items,
                    now=1000,
                    max_session_age_hours=1,
                    logs_path=logs,
                )

                self.assertEqual(affected, items)
                self.assertEqual(items[0].attempts, 2)
                self.assertEqual(items[0].updated_at, 980)
                self.assertEqual(items[0].next_attempt_at, 1280)
```

- [ ] **Step 7: Implement pending-item rescheduling**

Add this narrow helper to `tokenmaxx/codex.py`:

```python
def reschedule_pending_capacity_item(
    items: list[QueueItem],
    *,
    session_id: str,
    hit_at: int,
    retry_at: int,
) -> QueueItem | None:
    for existing in reversed(items):
        if existing.key != ("codex", session_id):
            continue
        if existing.status != "pending" or hit_at < existing.updated_at:
            return None
        if existing.next_attempt_at == retry_at:
            return None
        existing.next_attempt_at = retry_at
        return existing
    return None
```

When `apply_limit_event()` returns `None` for a winning capacity event, assign
the helper result back to `item` before the single scheduling block from Step
4:

```python
        if item is None and capacity_retry_at is not None:
            item = reschedule_pending_capacity_item(
                items,
                session_id=session_id,
                hit_at=hit_at,
                retry_at=capacity_retry_at,
            )
```

The scheduling block then appends the item to `affected` exactly once. Do not
change `attempts` or `updated_at`; those continue to describe the last actual
resume outcome.

- [ ] **Step 8: Run provider tests and make them pass**

Run:

```bash
PYTHONPYCACHEPREFIX=/tmp/tokenmaxx-pycache python3 -m unittest tests.test_codex -v
```

Expected: all Codex tests pass, including the existing test proving capacity
text in rollout error payloads is not a usage-limit signal.

- [ ] **Step 9: Commit the provider behavior**

```bash
git add tokenmaxx/codex.py tests/test_codex.py
git commit -m "feat(codex): retry exact model-capacity failures"
```

---

### Task 2: Plumb the logs database through CLI and launchd

**Files:**
- Modify: `tests/test_tokenmaxx.py`
- Modify: `tokenmaxx/config.py`
- Modify: `tokenmaxx/cli.py`
- Modify: `tokenmaxx/launchd.py`

**Interfaces:**
- Consumes: Task 1's `build_limited_queue_items(..., logs_path=...)`.
- Produces: `default_codex_logs_db() -> Path`.
- Produces: `args.codex_logs_db: Path` on commands using `add_common_args()`.
- Extends: `build_launchd_plist(..., codex_logs_db: Path | None = None)`.
- Consumes: Task 1's external-stop-aware `find_active_session()` at dispatch time.

- [ ] **Step 1: Add the test fixture path and failing CLI integration test**

In `TokenmaxxTests.setUp()`, set:

```python
        self.codex_logs_db = self.root / "codex-logs.sqlite"
```

Add `"codex_logs_db": self.codex_logs_db` to `args()` defaults. Add this test:

```python
    def test_autoqueue_passes_codex_logs_database_to_provider(self):
        with patch("tokenmaxx.cli.codex.build_limited_queue_items", return_value=[]) as build:
            cli.autoqueue_limited_sessions(
                self.args(),
                [],
                1000,
                [],
                provider="codex",
            )

        self.assertEqual(build.call_args.kwargs["logs_path"], self.codex_logs_db)

    def test_active_codex_check_passes_external_stop_sources(self):
        self.write_codex_session("codex-1")
        item = QueueItem(cwd="/tmp/codex", session_id="codex-1", provider="codex")

        with patch("tokenmaxx.cli.codex.find_active_session", return_value=None) as find:
            cli.find_active_provider_session(self.args(), item, 1000)

        self.assertEqual(find.call_args.kwargs["history_path"], self.codex_history_file)
        self.assertEqual(find.call_args.kwargs["logs_path"], self.codex_logs_db)
        self.assertEqual(find.call_args.kwargs["max_session_age_hours"], 24)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
PYTHONPYCACHEPREFIX=/tmp/tokenmaxx-pycache python3 -m unittest \
  tests.test_tokenmaxx.TokenmaxxTests.test_autoqueue_passes_codex_logs_database_to_provider \
  tests.test_tokenmaxx.TokenmaxxTests.test_active_codex_check_passes_external_stop_sources -v
```

Expected: both tests fail because autoqueue and the active-session guard do not
pass the external stop paths.

- [ ] **Step 3: Add config and provider-dispatch plumbing**

Add to `tokenmaxx/config.py`:

```python
def default_codex_logs_db() -> Path:
    return Path.home() / ".codex" / "logs_2.sqlite"
```

Import it in `tokenmaxx/cli.py`, add this common argument:

```python
    parser.add_argument("--codex-logs-db", type=Path, default=default_codex_logs_db())
```

Pass the path in `autoqueue_limited_sessions()`:

```python
            logs_path=getattr(args, "codex_logs_db", None),
```

Extend the Codex branch in `find_active_provider_session()`:

```python
        return codex.find_active_session(
            sessions,
            item.session_id,
            now,
            DEFAULT_ACTIVE_GRACE_SECONDS,
            history_path=getattr(args, "codex_history_file", None),
            logs_path=getattr(args, "codex_logs_db", None),
            max_session_age_hours=args.max_session_age_hours,
        )
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the Step 2 command again.

Expected: 2 tests pass.

- [ ] **Step 5: Write failing parser and launchd tests**

Extend `test_parser_accepts_codex_provider_paths_and_selectors` so its watch
parse and assertions are exactly:

```python
        watch_args = parser.parse_args(
            [
                "watch",
                "--codex-bin",
                "/usr/local/bin/codex",
                "--codex-logs-db",
                str(self.codex_logs_db),
            ]
        )
        self.assertEqual(watch_args.codex_bin, "/usr/local/bin/codex")
        self.assertEqual(watch_args.codex_logs_db, self.codex_logs_db)
```

Extend
`test_build_launchd_plist_contains_watch_command` with
`codex_logs_db=Path("/tmp/codex-logs.sqlite")` and these assertions:

```python
        self.assertIn("<string>--codex-logs-db</string>", plist)
        self.assertIn("<string>/tmp/codex-logs.sqlite</string>", plist)
```

Extend `test_start_writes_plist_when_only_codex_resolves`:

```python
        self.assertIn("<string>--codex-logs-db</string>", plist)
        self.assertIn(f"<string>{self.codex_logs_db}</string>", plist)
```

- [ ] **Step 6: Run the three tests and verify RED**

Run:

```bash
PYTHONPYCACHEPREFIX=/tmp/tokenmaxx-pycache python3 -m unittest \
  tests.test_tokenmaxx.TokenmaxxTests.test_parser_accepts_codex_provider_paths_and_selectors \
  tests.test_tokenmaxx.TokenmaxxTests.test_build_launchd_plist_contains_watch_command \
  tests.test_tokenmaxx.TokenmaxxTests.test_start_writes_plist_when_only_codex_resolves -v
```

Expected: parser rejects the new option or the plist assertions fail.

- [ ] **Step 7: Add launchd argument plumbing**

Extend `build_launchd_plist()` in `tokenmaxx/launchd.py` with:

```python
    codex_logs_db: Path | None = None,
```

Append its daemon argument after `--codex-history-file`:

```python
    if codex_logs_db is not None:
        arguments.extend(["--codex-logs-db", str(Path(codex_logs_db).expanduser())])
```

Pass `codex_logs_db=args.codex_logs_db` from both `cmd_launchd_install()` and
`cmd_start()` in `tokenmaxx/cli.py`.

- [ ] **Step 8: Run Task 2 tests and verify GREEN**

Run:

```bash
PYTHONPYCACHEPREFIX=/tmp/tokenmaxx-pycache python3 -m unittest \
  tests.test_tokenmaxx.TokenmaxxTests.test_autoqueue_passes_codex_logs_database_to_provider \
  tests.test_tokenmaxx.TokenmaxxTests.test_active_codex_check_passes_external_stop_sources \
  tests.test_tokenmaxx.TokenmaxxTests.test_parser_accepts_codex_provider_paths_and_selectors \
  tests.test_tokenmaxx.TokenmaxxTests.test_build_launchd_plist_contains_watch_command \
  tests.test_tokenmaxx.TokenmaxxTests.test_start_writes_plist_when_only_codex_resolves -v
```

Expected: 5 tests pass.

- [ ] **Step 9: Commit CLI and daemon plumbing**

```bash
git add tokenmaxx/config.py tokenmaxx/cli.py tokenmaxx/launchd.py tests/test_tokenmaxx.py
git commit -m "feat(launchd): pass Codex capacity log database"
```

---

### Task 3: Document, release, install, and verify the feature

**Files:**
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/SECURITY.md`
- Modify: `tokenmaxx/config.py`
- Modify: `tokenmaxx/__init__.py`
- Modify: `pyproject.toml`
- Modify: `tests/test_tokenmaxx.py`

**Interfaces:**
- Consumes: Tasks 1-2 complete feature behavior and CLI surface.
- Produces: package version `0.5.6` and live launchd watcher carrying `--codex-logs-db`.

- [ ] **Step 1: Write the failing version test**

Change `test_package_version_is_patch_release` to:

```python
    def test_package_version_is_patch_release(self):
        self.assertEqual(cli.__version__, "0.5.6")
```

- [ ] **Step 2: Run the version test and verify RED**

Run:

```bash
PYTHONPYCACHEPREFIX=/tmp/tokenmaxx-pycache python3 -m unittest \
  tests.test_tokenmaxx.TokenmaxxTests.test_package_version_is_patch_release -v
```

Expected: FAIL with `0.5.5 != 0.5.6`.

- [ ] **Step 3: Update package version and guarded prompt**

Set `__version__ = "0.5.6"` in `tokenmaxx/__init__.py` and
`version = "0.5.6"` in `pyproject.toml`.

Change the Codex guarded prompt sentence in `tokenmaxx/config.py` to:

```text
If it hit a usage limit, a transient remote compaction/transport failure, or temporary model capacity before finishing, resume the remaining work.
```

- [ ] **Step 4: Update user and security documentation**

In `README.md`, replace the Codex source sentence in the Safety Model with:

```markdown
- Auto-queue reads Claude Code session metadata in `~/.claude/sessions`, Claude
  transcript tails in `~/.claude/projects`, Codex rollout files under
  `~/.codex/sessions`, the bounded `~/.codex/history.jsonl` history tail, and
  exact provider turn failures from read-only `~/.codex/logs_2.sqlite`.
```

Replace the Codex detection bullet with:

```markdown
- Codex auto-queue accepts a terminal provider-authored `event_msg` error with
  structured code `usage_limit_exceeded`, the exact provider-authored
  usage-limit error prefix when the code is omitted, or a `token_count` event
  whose rate-limit window is exhausted and has a future reset. It also accepts
  known remote-compaction disconnect records from `~/.codex/history.jsonl` and
  exact `codex_core::session::turn` model-capacity failures from read-only
  `~/.codex/logs_2.sqlite` when the rollout has no newer task activity. Generic
  failures and matching text in user, assistant, tool, history, or file content
  are ignored. Capacity retries keep the same model and become due five minutes
  after the provider event.
```

Add `--codex-logs-db ~/.codex/logs_2.sqlite` to the launchd argument example,
and replace the capacity-relevant Codex prompt line with the exact text from
Step 3.

In `docs/ARCHITECTURE.md`, extend discovery step 1 with read-only
`~/.codex/logs_2.sqlite`, then replace the capacity sentence in step 4 with:

```markdown
   `codex.load_model_capacity_events` queries only discovered thread IDs in the
   read-only Codex logs database and accepts only
   `target == "codex_core::session::turn"` rows ending with the exact provider
   `Turn error: Selected model is at capacity. Please try a different model.`
   suffix. Newer rollout task activity suppresses both history and capacity
   events. A capacity row is due five minutes after its timestamp and resumes
   the same model.
```

In `docs/SECURITY.md`, replace the capacity exclusion at the end of Detection
Boundary with:

```markdown
Model-capacity retry is accepted only from a thread-scoped
`codex_core::session::turn` row in the Codex logs database whose body ends with
the exact provider `Turn error` banner. Matching text in history, user prompts,
assistant text, tools, or files remains untrusted. The database is opened
read-only; missing, locked, malformed, unreadable, or incompatible databases
skip capacity discovery without stopping rollout or history detection.
```

- [ ] **Step 5: Run the complete repository validation gate**

Run:

```bash
PYTHONPYCACHEPREFIX=/tmp/tokenmaxx-pycache python3 -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=/tmp/tokenmaxx-pycache python3 -m py_compile tokenmaxx/*.py tests/*.py
git diff --check
```

Expected: every test passes, syntax compilation exits 0, and `git diff --check`
prints nothing.

- [ ] **Step 6: Commit the release changes**

```bash
git add README.md docs/ARCHITECTURE.md docs/SECURITY.md tokenmaxx/config.py \
  tokenmaxx/__init__.py pyproject.toml tests/test_tokenmaxx.py
git commit -m "release: add Codex model-capacity retry"
```

- [ ] **Step 7: Reinstall the feature build**

Run:

```bash
/Users/pep/.local/bin/uv tool install --force --reinstall --no-cache \
  /Users/pep/dev/pulpo/claude-code/.worktrees/tokenmaxx-capacity-retry
```

Expected: installation succeeds and `/Users/pep/.local/bin/tokenmaxx --version`
prints `tokenmaxx 0.5.6`.

- [ ] **Step 8: Reload the LaunchAgent with the new argument**

Run:

```bash
/Users/pep/.local/bin/tokenmaxx stop
/Users/pep/.local/bin/tokenmaxx start
```

Expected: `com.local.tokenmaxx` unloads and starts successfully.

- [ ] **Step 9: Verify the live binary, plist, process, and immediate cycle**

Run:

```bash
/Users/pep/.local/bin/tokenmaxx --version
/usr/bin/plutil -p /Users/pep/Library/LaunchAgents/com.local.tokenmaxx.plist
launchctl print gui/$(id -u)/com.local.tokenmaxx
/Users/pep/.local/bin/tokenmaxx autoqueue
/Users/pep/.local/bin/tokenmaxx watch --once
/Users/pep/.local/bin/tokenmaxx status
tail -20 /Users/pep/.tokenmaxx/tokenmaxx.log
```

Expected:

- version is `0.5.6`;
- plist and launchctl arguments contain `--codex-logs-db` and
  `/Users/pep/.codex/logs_2.sqlite`;
- launchd state is running with a 300-second interval;
- the immediate scan/watch cycle completes without traceback;
- an eligible exact capacity event is queued or stale/recovered capacity events
  are suppressed by newer rollout activity; and
- queue status remains inspectable with no duplicate provider/session rows.

- [ ] **Step 10: Push the verified branch**

```bash
git push -u origin codex/capacity-retry
```

Expected: the remote branch points to the final verified release commit.
