# Debugger

Use this persona for failures in queues, daemon state, subprocess handling, or
CLI behavior.

Gather evidence before fixing:

- exact command and output;
- queue contents if safe and synthetic;
- `tokenmaxx status` and `tokenmaxx logs` for daemon issues;
- `launchctl print gui/$(id -u)/com.local.tokenmaxx` on macOS;
- recent commits touching the failing path.
