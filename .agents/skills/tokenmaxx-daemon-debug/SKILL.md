---
name: tokenmaxx-daemon-debug
description: Use when tokenmaxx launchd start, stop, status, logs, or background resume behavior fails.
---

# tokenmaxx Daemon Debug

1. Capture direct evidence:

   ```bash
   tokenmaxx status
   tokenmaxx logs --lines 80
   launchctl print gui/$(id -u)/com.local.tokenmaxx
   ```

2. Compare launchd plist arguments against `tokenmaxx/launchd.py`.
3. Check whether launchd's environment differs from the interactive shell.
4. Reproduce with `python3 -m tokenmaxx watch --once --dry-run --no-auto-queue --queue /tmp/tokenmaxx-smoke-queue.jsonl`
   before a real resume when possible.
5. Add tests for plist arguments or launchctl wrapper behavior before fixing.
6. Run the full validation gate before reporting the daemon fixed.
