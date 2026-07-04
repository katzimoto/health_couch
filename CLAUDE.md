# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal server that pulls Garmin Connect data daily into SQLite, exposes it to ChatGPT Pro via an MCP connector, and runs a two-way Telegram coach (OpenAI-backed) that pushes a 07:30 morning plan and answers questions grounded in the user's own data. One Docker image, several containers each running a different command (see `docker-compose.yml`).

## Commands

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python -m pytest                    # offline tests (DB + analysis), no network needed
python -m pytest tests/test_data.py::test_analyzer_report_and_flags   # single test

docker compose up -d scheduler mcp tunnel   # Phase 1: data + ChatGPT connector
docker compose up -d telegram               # Phase 2/3: enable the Telegram coach
docker compose logs -f <service>            # scheduler | mcp | tunnel | telegram
docker compose run --rm scheduler python scripts/garmin_login.py   # cache Garmin tokens (once, or after ~6mo expiry)
docker compose run --rm scheduler python scripts/backfill.py 180   # backfill N days on demand
docker compose run --rm telegram python scripts/get_chat_id.py     # find your Telegram chat id
docker compose run --rm scheduler python scripts/import_apple_health.py /app/data/export.zip  # import an Apple Health export (idempotent; --since YYYY-MM-DD to limit)
```

There is no lint/typecheck config in this repo — `pytest` is the only checked command.

## Architecture

**One image, several roles.** `Dockerfile` builds a single image; `docker-compose.yml` runs it as four services that differ only in their `command`: `scheduler` (APScheduler: hourly Garmin sync of today, daily pull + gap healing, 07:30 plan push, nightly backup), `mcp` (FastMCP HTTP server for ChatGPT), `tunnel` (Cloudflare Tunnel, exposes `mcp` publicly), `telegram` (long-polling bot). All services share `./data` (SQLite) and `./garmin-tokens` (Garmin auth) as mounted volumes.

**Data flow:** `garmin_client.py` (`GarminClient`) pulls per-metric data from Garmin Connect into SQLModel tables in `database.py`/`models.py`, keyed by `day` (ISO string). Upserts are field-preserving (`Database._upsert`): non-`None` incoming fields update the row, existing values are never blanked — re-pulls are idempotent and a partial write (e.g. `log_weight` with only `weight_kg`) can't erase fields a fuller write stored. Successful pulls are recorded per-day in `pull_log`, which the scheduler's nightly gap healing (`GarminClient.pull_missing_days`) uses to recover from interrupted backfills. `analysis.py` (`Analyzer`) reads a hand-written `daily_summary` SQL view (joins all metric tables, recreated on every DB init in `database.py`) and computes 7d-vs-28d trends, sleep debt, acute:chronic training-load ratio, and plain-language flags. `coach.py` (`Coach`) feeds that analyzer report (never raw DB rows) into an OpenAI system prompt to produce either a structured morning plan or a free-text chat reply, with conversation history persisted in the `Conversation` table for multi-turn memory. Both `mcp_server.py` and `telegram_bot.py` are thin consumers of `Coach`/`Analyzer`/`Database`.

**Config is centralized:** every module reads `garmin_coach.config.settings` (a frozen dataclass populated once from `.env`/env vars) rather than touching `os.environ` directly.

**Failure isolation is a deliberate pattern**, not an oversight — preserve it when touching these paths:
- `garmin_client.py`: every per-metric pull (`_pull_sleep`, `_pull_hrv`, etc.) is wrapped individually in `pull_day`, so one broken Garmin endpoint doesn't abort the rest of the day's pull. The `_get` helper walks nested dict/list keys defensively since Garmin's undocumented JSON shape varies by device/firmware.
- `scheduler.py`: Garmin login failure at boot, backfill failure, and each scheduled job are all caught and logged individually so the container stays up and jobs retry on their own schedule. The morning-plan job additionally retries with backoff (reusing the already-saved plan, not regenerating), and the nightly pull heals a bounded number of missing history days per run.
- `telegram_bot.py` and `scheduler.py` run blocking work (OpenAI calls, Garmin pulls) via `asyncio.to_thread` so the event loop — polling, other handlers, scheduled jobs — never stalls behind it. Long-running services touch `data/heartbeats/<service>` (see `heartbeat.py`); the compose healthchecks check that file's freshness, so keep the beats alive when restructuring these loops.

**Security boundaries to preserve:**
- Only the analyzer's *summaries* are sent to the OpenAI API — never raw DB rows (see `coach.py` docstring).
- `mcp_server.py` requires OAuth via WorkOS AuthKit (`AUTHKIT_DOMAIN` + `MCP_PUBLIC_URL`) for any deployment reachable publicly — needed for ChatGPT's connector, which requires OAuth rather than a static token. AuthKit is wired as a resource-server-only integration (`AuthKitProvider`): WorkOS runs the actual authorization server (login, consent, PKCE, token issuance) via a hosted AuthKit domain with sign-up disabled and exactly one invited user, and this process only verifies the JWTs WorkOS issues — no custom OAuth server code lives here. Falls back to a static `MCP_BEARER_TOKEN` if AuthKit isn't configured; logs a loud warning (not an error) if neither is set.
- `telegram_bot.py` restricts all handlers to `TELEGRAM_CHAT_ID` via `_authorized()` so a leaked bot handle can't leak health data or spend API credits.
- `.env`, `data/`, and `garmin-tokens/` are git-ignored; don't add code paths that would write secrets into the repo or the SQLite file's tracked contents.

## Adding a new Garmin metric

Touches four places in order: add a table to `models.py`, add it to the `daily_summary` view + an `upsert_*` method in `database.py`, add a `_pull_*` extractor wired into the `pulls` dict in `garmin_client.py`, and if it should be queryable by column name, add it to `SUMMARY_COLUMNS` in `models.py`.

## Schema changes and migrations

`create_all` never alters existing tables, so live databases need explicit evolution — both layers run automatically on every startup (`Database.init_schema`):
- **New nullable column on an existing table:** just add it to the model. The generic reconciler (`Database._migrate_missing_columns`) adds it via `ALTER TABLE … ADD COLUMN`, backfilled as `NULL`.
- **Anything else** (data backfills, indexes, NOT NULL columns, renames): an Alembic revision. The environment lives *inside the package* (`garmin_coach/alembic/` — deliberately, so the Docker image ships it; `alembic.ini` at the root is only for the CLI). Create one with `alembic revision -m "..."` (or `--autogenerate`), put the real logic in `garmin_coach/migrations.py` as a plain-`Connection` function the revision script calls — that keeps it unit-testable — and write it guarded/idempotent (table/column-existence checks), because startup order is `create_all` → column reconciler → `upgrade head`, so on fresh databases the tables already exist in final shape and revisions must no-op cleanly (and may rely on reconciled columns existing). Startup upgrades run under a cross-container file lock (`data/.migrations.lock`) since all four services boot against one SQLite file.
- New *tables* need nothing: `create_all` handles them.

Every deploy also snapshots the DB first (`data/backups/pre-deploy-*.db`, newest 7 kept — see `.github/workflows/deploy.yml`), so any migration can be rolled back to the exact pre-deploy state.
