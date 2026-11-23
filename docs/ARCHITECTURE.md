# Architecture map and safe modularization plan

## Purpose

This document records a low-risk path for reducing the size and coupling of the three largest Python modules. It is a plan, not authorization to perform a bulk rewrite. Runtime behavior, route contracts, database semantics, and bot lifecycle must remain stable throughout extraction.

## Evidence snapshot

Measured from the repository's Python coverage artifacts on 2026-11-23:

| Module | Lines | Functions | Line coverage | Interpretation |
|---|---:|---:|---:|---|
| `main.py` | 4,437 | 104 | 13.7% | Largest behavior surface; weak regression net |
| `routes/admin.py` | 4,896 | 48 | 13.5% | Large route/template module; weak route coverage |
| `db.py` | 5,795 | 164 | 38.1% | Multiple persistence domains behind one module |
| `verification.py` | 157 | 4 | 88.0% | Improved by offline tests in QA-01 |
| `routes/settings.py` | 751 | 16 | 50.9% | Improved by mocked route tests in QA-02 |

Repository-wide line coverage was 28.29% after QA-01 and 28.98% in the QA-02 Python 3.13 PR artifact. These are line-coverage measurements, not proof of behavioral completeness.

## Current responsibility clusters

### `main.py`

The current file combines several distinct responsibilities:

1. **Interaction and permission plumbing** — interaction context binding, admin checks, safe defer/reply helpers, interaction lag diagnostics.
2. **Verification workflows** — click timeouts, pending codes, bio/live verification progress, verified-role grants/revocation, coin-only verification, persistent verification views.
3. **Live announcements** — announcement channel/message resolution, active announcement processing, session reconciliation, API-triggered announcements.
4. **Role tiers and leaderboard** — tier assignment/reset loops, tier-up announcements, leaderboard embeds and updater.
5. **Bot lifecycle and diagnostics** — persistent view registration, Discord event handlers, loop watchdogs, keep-awake and interaction diagnostics.
6. **Commands and setup** — prefix/slash commands, reports, help, verification setup, welcome/log setup.
7. **Startup orchestration** — startup snapshot and `main()`.

**Candidate future modules** (names are illustrative):
- `services/verification_service.py`
- `services/announcement_service.py`
- `services/role_tier_service.py`
- `bot/lifecycle.py`
- `commands/` for command groups

Do not extract these by moving functions mechanically. First identify their shared state, Discord client dependencies, callback registration, and event-loop assumptions.

### `routes/admin.py`

The file contains:
- Admin dashboard, host/viewer pages, activity and log pages.
- Analytics caching, query fan-out, analytics pages and large inline templates.
- Verification administration and role-grant/revoke flows.
- Timeline data and visualization routes.
- Maintenance and database repair actions.

**Candidate future blueprints**:
- `routes/admin_dashboard.py` (overview, hosts, viewers, monitor)
- `routes/admin_analytics.py` (analytics cache and analytics routes)
- `routes/admin_verification.py` (verification management)
- `routes/admin_timeline.py` (timeline page and data API)

Preserve every existing URL, endpoint name, method, auth/CSRF behavior, response shape, and template context. Blueprint registration order and endpoint names can be externally observable through `url_for` and tests.

### `db.py`

The file currently contains:
- Database environment policy, connection creation, transaction helpers, schema initialization.
- Settings cache and asynchronous settings writer.
- Analytics queries and aggregation.
- Host/alias and live-session state.
- Activity log batching and pruning.
- Gift-event transactions, integrity checks, and backfill.
- Discord/TikTok mappings and verification state.
- Tier settings and timeline aggregation.

**Candidate future persistence modules**:
- `repositories/core.py` — connection policy, transaction primitives, schema.
- `repositories/settings.py`
- `repositories/live_sessions.py`
- `repositories/gifts.py`
- `repositories/activity.py`
- `repositories/analytics.py`
- `repositories/verification.py`
- `repositories/timeline.py`

Keep `db.py` as a compatibility façade during migration. A naive `from module import *` façade is insufficient if callers or tests monkeypatch names on `db`; preserve patch points or update callers/tests intentionally. There must be one canonical connection/transaction implementation and no duplicate module instances.

## Recommended extraction order

### Phase 0 — strengthen behavioral tests first

Before moving code, add focused tests for:
- Verification flows and role-grant/revoke failure paths.
- Announcement create/update/delete/reconciliation and duplicate delivery behavior.
- Admin route authentication, CSRF, response contracts, and mocked database failures.
- Live-session transaction invariants, gift idempotency, and settings writer behavior.

The current low coverage of `main.py` and `routes/admin.py` makes immediate extraction unnecessarily risky.

### Phase 1 — extract pure helpers

Start with functions that have no Discord client, Flask request, global mutable state, or database connection dependency. Keep input/output contracts identical and add direct unit tests.

### Phase 2 — extract cohesive services

Move one responsibility cluster at a time (verification, announcements, role tiers). Inject or pass the bot/client and callbacks rather than importing `main.py` from service modules. Avoid circular imports and implicit global initialization.

### Phase 3 — split admin blueprints

Move one route family per PR. Keep route URLs and endpoint names stable; keep auth and CSRF hooks centralized in `server.py`. Move templates only when the route extraction is already covered and rendering contracts are tested.

### Phase 4 — split persistence modules last

This is the highest-risk phase. Extract query groups behind a single transaction/connection layer. Preserve atomicity, retries, transaction boundaries, settings-cache invalidation, write queues, and public function signatures. Do not change schema or data during module extraction.

## Non-negotiable invariants

- No direct commits to `main`; use a task branch and focused PR.
- No bulk file moves or mixed behavior/refactor changes.
- Preserve public imports until all call sites are migrated.
- Preserve Discord command names, callback registration, bot startup order, and event-loop ownership.
- Preserve Flask paths, endpoint names, auth/CSRF behavior, request validation, response shapes, and template variables.
- Preserve Turso fail-closed production behavior, one canonical database connection policy, and atomic write semantics.
- Preserve one open live session per account and existing resume/stale-session behavior.
- Never test against live TikTok, Discord, or production Turso in unit tests.
- Require Ruff, Python 3.11 and 3.13 full tests, and relevant regression tests for every extraction PR.

## Immediate next steps

1. Increase coverage around the critical `main.py` verification and announcement paths.
2. Increase `routes/admin.py` route coverage with Flask test clients and fake DB/service dependencies.
3. Audit duplicate webhook delivery separately. The database start-session path reuses an existing open session, but announcement/reconnect/logging side effects may still repeat; do not invent event-ID semantics until the provider's event identifier and retry contract are confirmed.
4. Only then begin Phase 1 pure-helper extraction.

## Explicitly deferred

No runtime refactor is included in this change. The coverage gaps and shared mutable state make a large one-shot split more likely to create regressions than to improve maintainability.
