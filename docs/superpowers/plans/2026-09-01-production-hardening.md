# Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add durable migrations, queued proactive delivery, runtime status/health, and deployment guidance without changing user-facing business workflows.

**Architecture:** SQLite is the durable source of truth for schema versions and outbound rows. The existing `MessageDispatcher` remains the sole QQ sender and consumes its own persisted queue. `HealthService` aggregates bounded dependency checks for the command and HTTP health endpoint.

**Tech Stack:** Python 3.11+, asyncio, aiosqlite, APScheduler, httpx, NoneBot2/FastAPI.

**Spec:** `docs/superpowers/specs/2026-09-01-production-hardening-design.md`

## Global Constraints

- Do not add dependencies or business commands beyond the requested `/status` expansion.
- Keep existing SQLite data intact and all service IDs as `str`.
- Write a failing test before each production behavior change.
- Run the target test module after each task.

---

### Task 1: Versioned database migrations

**Files:**
- Modify: `rumi_helper/database/models.py`, `rumi_helper/database/db.py`
- Test: `tests/test_migrations.py`

- [ ] Write a test that creates the legacy schema without migration metadata, reconnects through `Database`, and verifies existing rows survive plus migration versions are recorded.
- [ ] Verify the test fails because no migration ledger exists.
- [ ] Add ordered idempotent migrations and apply them from `Database.connect()`.
- [ ] Run `pytest -q tests/test_migrations.py`.

### Task 2: Durable outbound message queue

**Files:**
- Modify: `rumi_helper/database/models.py`, `rumi_helper/database/db.py`, `rumi_helper/config.py`, `rumi_helper/services/qq/dispatcher.py`, `rumi_helper/services/scheduler.py`, `rumi_helper/services/github/tracker.py`, `rumi_helper/services/report.py`, `rumi_helper/services/runtime.py`
- Test: `tests/test_outbound_queue.py`, `tests/test_scheduler.py`, `tests/test_github.py`, `tests/test_report.py`

- [ ] Write tests for successful delivery, retry after failure, terminal failure at the configured attempt limit, and reclaiming an expired sending lease.
- [ ] Verify each test fails because queue methods do not exist.
- [ ] Add the outbound table, database claim/update methods, dispatcher worker, and scheduler enqueue calls.
- [ ] Run queue, scheduler, GitHub, report, and dispatcher tests.

### Task 3: Runtime status and health

**Files:**
- Create: `rumi_helper/services/health.py`
- Modify: `rumi_helper/services/llm/base.py`, `rumi_helper/services/llm/openai_compatible.py`, `rumi_helper/services/github/client.py`, `rumi_helper/services/runtime.py`, `rumi_helper/plugins/admin.py`, `bot.py`
- Test: `tests/test_health.py`, `tests/test_plugin_rules.py`

- [ ] Write tests for dependency health results, timeout/error isolation, and status rendering of uptime, counts, scheduler state, and recent errors.
- [ ] Verify the tests fail because the service and probes do not exist.
- [ ] Add bounded checks and expose `/status` plus `/healthz` without secrets.
- [ ] Run health and plugin tests.

### Task 4: Deployment documentation and final verification

**Files:**
- Create: `docs/DEPLOYMENT.md`
- Modify: `README.md`, `.env.example`, `AGENTS.md`

- [ ] Document Windows/Linux setup, NapCat reverse WebSocket settings, all environment variables, health endpoint, backups, and fault diagnosis.
- [ ] Run `pytest -q`, `ruff check rumi_helper tests`, `python -m compileall -q rumi_helper tests`, plugin import, and temporary Runtime startup/shutdown smoke test.
