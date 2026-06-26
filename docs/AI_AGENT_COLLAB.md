# AI Agent Collaboration

## Ownership

tokenmaxx is a small public package. Keep work scoped and easy to review.

- One agent should own one behavior change at a time.
- Do not mix runtime fixes with documentation onboarding unless the user asks.
- Keep commits logical: one feature/fix/docs unit per commit.
- Do not rewrite unrelated files for formatting.

## Handoff Format

When handing off, include:

- current branch and latest commit;
- files changed;
- validation commands run and their result;
- open risks or blocked items;
- any live daemon state touched.

## Conflict Avoidance

- Check `git status --short` before editing.
- Treat unrecognized changes as user or other-agent work.
- If another agent changed a file you need, read the file and integrate rather
  than overwriting it.
- Do not remove `.dwp/` or `tmp/` contents unless they are clearly your own
  scratch artifacts.

## Progress Reporting

Report after significant milestones:

- recon complete;
- files written;
- tests pass;
- commit pushed.

Progress reporting must not block validation or fixing an active failure.
