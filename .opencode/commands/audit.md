---
description: Comprehensive repository audit (architecture, security, quality, ops).
agent: orchestrator
---

Run a comprehensive repository audit using the orchestrator.

Scope: $ARGUMENTS (or whole repo if empty).

Delegate in parallel (max 3): Architect (structure/dependencies), QA & Security (tests/vulns), DevOps & Observability (config/deploy/logging). Integrate findings into a prioritized report with risks and recommended fixes. Do not modify code unless explicitly asked.
