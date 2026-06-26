# Performance Guide

tokenmaxx is intentionally small, but it runs as a local background tool, so it
must stay quiet and bounded.

## Budgets

- `watch` should process at most one due queue item per loop.
- `autoqueue` should scan recent sessions and transcript tails without reading
  entire long histories more than necessary.
- Resume subprocesses must have a timeout. The default is four hours, but tests
  should keep timeout behavior fast and mocked.
- Status output should stay readable even when many queue items exist.

## Hot Paths

### Session Scan

`claude.load_claude_sessions` reads `*.json` files under the sessions directory
and sorts by `updatedAt`. It skips malformed JSON and records without `cwd` or
`sessionId`.

### Transcript Tail

`claude.transcript_tail` reads the last 80 lines of a transcript by splitting the
file. This is simple and acceptable for current transcript sizes. If transcript
files become very large, replace this with a bounded tail reader and add tests.

### Queue Writes

`queue.write_queue` rewrites the whole JSONL queue. That is acceptable because
the queue is expected to hold a small number of local sessions. If queue size
grows into hundreds or thousands of records, revisit the persistence model.

### Launchd Loop

The launchd job runs `tokenmaxx watch` with a `StartInterval`. Avoid tight
polling. The default interval is 300 seconds.

## Performance Review Triggers

- Adding recursive scans beyond `~/.claude/sessions` or `~/.claude/projects`.
- Increasing transcript tail size.
- Processing multiple due items per loop.
- Adding continuous file watchers.
- Adding network calls or telemetry.
