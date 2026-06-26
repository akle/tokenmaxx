# Commands Reference

| Command | File | Purpose |
| --- | --- | --- |
| `/dwp-create` | `.agents/commands/dwp-create.md` | Create a Deep Work Plan draft. |
| `/dwp-execute` | `.agents/commands/dwp-execute.md` | Execute the next plan task. |
| `/dwp-refine` | `.agents/commands/dwp-refine.md` | Modify an existing plan without losing completed work. |
| `/dwp-resume` | `.agents/commands/dwp-resume.md` | Resume interrupted plan work. |
| `/dwp-status` | `.agents/commands/dwp-status.md` | Report plan progress without edits. |
| `/dwp-verify` | `.agents/commands/dwp-verify.md` | Verify DWP conformance. |
| `/skill-create` | `.agents/commands/skill-create.md` | Create or update a repository skill. |
| `/agent-create` | `.agents/commands/agent-create.md` | Create or update an agent persona. |
| `/code-review` | `.agents/commands/code-review.md` | Review tokenmaxx changes. |
| `/commit` | `.agents/commands/commit.md` | Prepare a conventional commit. |
| `/pr` | `.agents/commands/pr.md` | Prepare a PR summary. |

All `dwp-*` commands are thin delegators to `.agents/skills/deepworkplan`.
