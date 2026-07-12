# Performance Guide

tokenmaxx is intentionally small, but it runs as a local background tool, so it
must stay quiet and bounded.

## Budgets

- `watch` should process at most one due queue item per loop and one continuation
  globally across providers and concurrent watchers.
- `autoqueue` should scan recent sessions and transcript tails without reading
  entire long histories more than necessary.
- Resume subprocesses must have a timeout. The default is four hours, but tests
  should keep timeout behavior fast and mocked.
- Status output should stay readable even when many queue items exist.

## Hot Paths

### Session Scan

`claude.load_claude_sessions` reads `*.json` files under the Claude sessions
directory and sorts by `updatedAt`. It skips malformed JSON and records without
`cwd` or `sessionId`.

`codex.load_codex_sessions` recursively finds recent `*.jsonl` rollout files
and reads each from the beginning only until it finds `session_meta`. Event
detection remains bounded to the tail described below.

### Transcript Tail

`transcript.tail_records` seeks backward in chunks and JSON-parses only the last
80 lines, skipping malformed lines. Claude transcript checks and Codex limit
and activity checks share this bounded reader, so event-tail checks do not load
complete histories.

### Queue Writes

`queue.write_queue` rewrites the whole provider-qualified JSONL queue. That is
acceptable because the queue is expected to hold a small number of local
sessions. If queue size grows into hundreds or thousands of records, revisit
the persistence model.

### Launchd Loop

The launchd job runs `tokenmaxx watch` with a `StartInterval`. Avoid tight
polling. The default interval is 300 seconds.

## Performance Review Triggers

- Adding recursive scans beyond `~/.claude/sessions` or `~/.claude/projects`.
- Expanding recursive Codex scans beyond `~/.codex/sessions`.
- Increasing transcript tail size.
- Processing multiple due items per loop or weakening the global resume lock.
- Adding continuous file watchers.
- Adding network calls or telemetry.
