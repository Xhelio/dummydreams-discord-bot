---
description: Summarize repository and development status.
agent: orchestrator
---

Summarize repository and development status: $ARGUMENTS.

Use git status and recent log to report branch, uncommitted changes, open PR context, and next steps. Keep concise.

!`git status --short && git log --oneline -5`
