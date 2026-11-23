# Agent Coordination Protocol

This document is the shared source of truth for work performed by multiple ChatGPT conversations ("agents") in this repository.

## 1. Operating model

- Each agent is a separate ChatGPT conversation instructed by the repository owner. Agents do not share chat history or coordinate with one another automatically.
- The repository, GitHub Issues, branches, pull requests, CI results, and this document are the shared coordination layer.
- The repository owner acts as the orchestrator: assigns tasks, starts each agent conversation, and routes reports between agents.
- Parallel work is allowed only for tasks with clear ownership and limited file overlap. Integration and merges are controlled and sequential.

## 2. Non-negotiable safety rules

1. Never commit directly to `main`.
2. Every task uses its own branch created from the latest `origin/main` unless the orchestrator explicitly specifies another base.
3. Never force-push, rewrite shared history, or delete another agent's branch.
4. Do not silently expand task scope. Report adjacent issues and propose a separate task.
5. Preserve existing behavior and public interfaces unless the task explicitly requires a change.
6. Inspect relevant code, tests, configuration, and recent history before editing.
7. Add or update regression tests for behavior changes and bug fixes.
8. Do not weaken, skip, or delete tests merely to obtain a passing CI result.
9. Never place secrets, tokens, credentials, personal data, or production values in commits, logs, fixtures, or PR descriptions.
10. Do not perform destructive production operations, irreversible data migrations, permission changes, or secret rotations without explicit owner approval.
11. If the working tree/branch has unexpected changes, stop and report before overwriting or resetting anything.
12. If another task has changed the same files or a dependency contract is unclear, coordinate through the orchestrator before proceeding.

## 3. Task assignment and ownership

Before work begins, the orchestrator should record:
- Task ID and concise objective
- Assigned agent/conversation
- Explicit in-scope and out-of-scope areas
- Expected files or modules, if known
- Dependencies and tasks that must land first
- Acceptance criteria and required tests
- Branch name and status

One task has one primary owner. Other agents must not edit its owned files unless the orchestrator explicitly coordinates a handoff.

Suggested task states: `queued`, `in-progress`, `blocked`, `review`, `merged`, `deployed`, `closed`.

Use GitHub Issues as the durable task ledger where practical. Use labels such as `agent:core`, `agent:features`, `agent:reliability`, `agent:performance`, `agent:security`, `agent:qa`, and `status:blocked`. Create labels before relying on them; do not assume they already exist.

## 4. Branch and commit conventions

Use one branch per task:
- `agent/core/<task-id>-<slug>`
- `agent/feature/<task-id>-<slug>`
- `agent/fix/<task-id>-<slug>`
- `agent/perf/<task-id>-<slug>`
- `agent/security/<task-id>-<slug>`
- `agent/qa/<task-id>-<slug>`
- `chore/<task-id>-<slug>`

Keep commits focused and use Conventional Commit prefixes where practical: `feat:`, `fix:`, `test:`, `refactor:`, `perf:`, `docs:`, `chore:`.

Before opening a PR:
- Fetch the latest base branch and integrate it safely.
- Resolve conflicts without discarding other work.
- Run formatting, linting, type/static checks, and relevant tests available in the project.
- Inspect the final diff for unrelated changes, secrets, generated artifacts, and accidental deletions.

## 5. Pull request contract

Every agent submits a PR targeting `main` unless explicitly instructed otherwise. PRs should:
- Link the task Issue (for example, `Closes #123` when appropriate).
- Explain the problem, approach, and behavior changes.
- List changed files/modules and compatibility considerations.
- Include exact commands run and their outcomes; distinguish passed, failed, skipped, and not-run checks.
- Describe regression coverage and any remaining risks.
- Identify dependencies on other PRs.
- Include deployment or migration notes when relevant.

Prefer small, independently reviewable PRs. Do not bundle unrelated fixes.

## 6. Integration and merge policy

The orchestrator/integrator owns integration order:
1. Check task ownership, scope, PR diff, and dependency order.
2. Confirm the PR is based on a current `main` and resolve conflicts.
3. Review the implementation and tests; request a separate review pass for high-risk changes.
4. Require all repository-required status checks to pass. If no checks are configured, run the available project test suite and report that CI protection is absent.
5. Confirm no unresolved blocking review comments and no known regression.
6. Squash merge one PR at a time, then re-check the next PR against the updated `main`.

Do not claim that GitHub branch protection, required checks, merge queue, or auto-merge are enabled unless verified in repository settings. If a check is unavailable or flaky, investigate and document it; do not bypass a failing required check.

## 7. Deployment and post-deployment verification

Deployment is permitted after a change is merged only when:
- The repository's deployment workflow/configuration is understood and the relevant checks passed.
- The change does not require an unapproved destructive migration, secret/permission change, or production-risk decision.
- The deployment target and rollback path are known.

After deployment, inspect the workflow result and available health/log signals. Report the deployed commit, workflow/run URL, verification performed, and any follow-up. If deployment cannot be verified, state that clearly; never infer success from a merge.

## 8. Blockers and handoffs

Stop and report when:
- The task requires credentials or permissions that are not available.
- Tests expose an unrelated or pre-existing failure that prevents reliable validation.
- The task overlaps another agent's owned files or changes a shared interface.
- A migration, security decision, or production action needs owner approval.
- The requested outcome cannot be achieved without weakening safety or compatibility.

A handoff must include current branch, latest commit SHA, changed files, test results, remaining work, and exact next action.

## 9. Required completion report

Every agent returns:
- Task ID and status
- Branch name and PR URL
- Latest commit SHA
- Summary of implementation
- Files changed
- Tests/checks run with results
- Compatibility and regression assessment
- Known limitations, blockers, and risks
- Dependencies or coordination needed
- Whether deployment was performed and how it was verified

A task is not complete merely because code was written or a PR was opened. It is complete only after the agreed acceptance criteria are met and the orchestrator records the final merge/deployment state.
