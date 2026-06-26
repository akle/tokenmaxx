# tokenmaxx Documentation

This directory is the durable operating context for tokenmaxx. It explains what
the tool is for, how it is built, how to validate it, and how agents should work
inside the repository.

| Guide | Purpose |
| --- | --- |
| [PRODUCT_SPEC.md](PRODUCT_SPEC.md) | Non-technical product purpose, users, success criteria, and non-goals. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Package layout, data flow, queue model, launchd integration, and runtime boundaries. |
| [STANDARDS.md](STANDARDS.md) | Coding, CLI, queue, daemon, docs, and release standards. |
| [TESTING_GUIDE.md](TESTING_GUIDE.md) | Test framework, file conventions, validation gates, and coverage expectations. |
| [DEVELOPMENT_COMMANDS.md](DEVELOPMENT_COMMANDS.md) | Exact commands for local development, packaging checks, and daemon smoke tests. |
| [SECURITY.md](SECURITY.md) | Local data boundaries, secrets handling, launchd posture, and security review triggers. |
| [PERFORMANCE.md](PERFORMANCE.md) | Performance-sensitive paths and budgets for scans, queue operations, and resumes. |
| [AI_AGENT_ONBOARDING.md](AI_AGENT_ONBOARDING.md) | First-session checklist for agents. |
| [AI_AGENT_COLLAB.md](AI_AGENT_COLLAB.md) | Handoff and collaboration rules for humans and agents. |
| [PR_REVIEW_WORKFLOW.md](PR_REVIEW_WORKFLOW.md) | Review gates and PR expectations. |
| [ECOSYSTEM_CONTEXT.md](ECOSYSTEM_CONTEXT.md) | Adjacent tools and ecosystem boundaries. |
| [github-workflows/test.yml](github-workflows/test.yml) | GitHub Actions workflow template for tests and syntax checks. |

Start with `PRODUCT_SPEC.md`, then `ARCHITECTURE.md`, then
`DEVELOPMENT_COMMANDS.md` before making code changes.
