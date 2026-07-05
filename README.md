# Health Coach

A personal server that pulls your Garmin data daily, lets you explore it inside
**ChatGPT Pro** (via a custom MCP connector), and gives you a **Telegram coach
you can talk to** — to look better and feel healthier.

> General wellness suggestions, not medical advice.

---

## What it does

```
                    Garmin Connect
                          │  (garminconnect, cached token auth)
                          ▼
              Daily Puller ──►  SQLite (data/health.db, via SQLModel)
                          │
            ┌─────────────┴──────────────┐
            ▼                            ▼
   FastMCP Server               Trend Analyzer + LLM Coach
   (HTTP, bearer auth)          (OpenAI API)
            │                            │
            ▼                            ▼
   ChatGPT Pro app              Telegram bot (two-way)
   "How's my sleep trend?"      07:30 daily plan + chat anytime
```

One image, four small containers, one `docker compose up -d`.

| Piece | Choice |
|---|---|
| Language | Python 3.12 |
| Garmin access | `garminconnect` (cached tokens) |
| DB / ORM | SQLite + `SQLModel` |
| MCP server | `fastmcp` (HTTP transport, bearer token) |
| LLM | OpenAI API (`gpt-4o-mini` default) |
| Telegram | `python-telegram-bot` (long polling) |
| Web dashboard | Starlette + inline-SVG charts (no JS deps) |
| Public HTTPS | Cloudflare Tunnel (container) |
| Scheduling | `APScheduler` (in the scheduler container) |
| Deployment | Docker Compose |

---

## Repo layout

```
health_couch/
├── docker-compose.yml        # the whole system
├── Dockerfile                # one image, several roles
├── requirements.txt
├── .env.example              # copy to .env and fill in
├── garmin_coach/
│   ├── config.py             # typed settings from .env
│   ├── models.py             # SQLModel tables
│   ├── database.py           # upserts, daily_summary view, memory
│   ├── garmin_client.py      # daily pull + backfill
│   ├── analysis.py           # trends + flags
│   ├── coach.py              # OpenAI: daily plan + evening report + chat
│   ├── telegram_bot.py       # two-way coach, feedback + health-event capture
│   ├── reminders.py          # Telegram reminder engine (recurrence + CRUD)
│   ├── telegram_sender.py    # one-shot Bot API sends (reminders, MCP tool)
│   ├── mcp_server.py         # FastMCP tools for ChatGPT
│   ├── scheduler.py          # daily pull + 07:30 plan + reminder dispatch
│   ├── webapp.py             # Starlette web dashboard + JSON API
│   └── web/                  # dashboard page, styles, SVG-chart JS
└── scripts/
    ├── garmin_login.py       # run once to cache tokens
    ├── get_chat_id.py        # find your Telegram chat id
    └── backfill.py           # import history on demand
```

---

## Setup

### 0. Prerequisites
- Docker + Docker Compose
- A Garmin Connect account
- An OpenAI API key (for the coach) — pay-as-you-go, ~$1–2/mo for this use
- A Telegram bot token from [@BotFather](https://t.me/BotFather) (for Phase 2/3)

### 1. Configure
```bash
cp .env.example .env
# edit .env — set OPENAI_API_KEY, COACH_GOALS, TZ, and generate MCP_BEARER_TOKEN:
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 2. Log in to Garmin once (caches tokens to ./garmin-tokens)
```bash
docker compose run --rm scheduler python scripts/garmin_login.py
```

### 3. Start the data foundation (Phase 1)
```bash
docker compose up -d scheduler mcp tunnel
```
- `scheduler` backfills the last 90 days on first run, then pulls daily.
- `mcp` serves the read tools; `tunnel` puts them on a public HTTPS URL.

### 4. Connect ChatGPT Pro
In ChatGPT: **Settings → Connectors → developer mode → Add connector**, using
your Cloudflare Tunnel URL and an `Authorization: Bearer <MCP_BEARER_TOKEN>`
header. Then ask things like *"Compare my recovery this month vs last and adjust
my training."*

### 5. Open the web dashboard
```bash
docker compose up -d dashboard
```
Browse to `http://<host>:8050` for metric cards, trend deltas, current flags,
and charts (sleep, HRV, resting HR, steps, weight, body fat, training load,
stress) with a 7/30/90-day selector. It's read-only and unauthenticated by
default — keep it on localhost/LAN, or set `DASHBOARD_TOKEN` (then browse with
`?token=…`) and route it through the tunnel to expose it safely.

### 6. Enable the Telegram coach (Phase 2/3)
```bash
# message your bot once, then:
docker compose run --rm telegram python scripts/get_chat_id.py
# put TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env, then:
docker compose up -d telegram
```
You'll get a plan at 07:30 daily. Message the bot anytime; use `/status`,
`/plan`, `/report`, and `/done` `/skipped` `/felt <note>` to log feedback.

### 7. Telegram reminders (managed by ChatGPT)

ChatGPT can schedule Telegram nudges through the connector — ask it to
"install the default health reminders" (morning plan 08:00, lunch log 13:00,
dinner log 20:00, evening report 21:30, local time) or to create custom ones
(`create_telegram_reminder`: once / daily / weekly / weekdays / RRULE).
The scheduler container delivers them; replies are captured as structured
health events ChatGPT can read back (`get_health_events`):

- `/meal <description>` (or just send a photo) — logs a meal
- `/skipped lunch|dinner` — records the skipped meal
- `/water 500` — adds to today's hydration total
- `/done workout` — marks today's planned workout done
- `/reminders`, `/edit_reminder <id> <field> <value>`, `/pause_reminder <id>`,
  `/resume_reminder <id>`, `/delete_reminder <id>` — manage reminders in-chat

---

## Build phases

- **Phase 1 — Data + ChatGPT Pro:** `scheduler` + `mcp` + `tunnel`. Pull data,
  connect the MCP connector, chat with your data in ChatGPT.
- **Phase 2 — Telegram daily coach:** enable `telegram` → 07:30 morning plan.
- **Phase 3 — Talk to your coach:** two-way chat with data context, conversation
  memory, and a feedback loop that shapes the next day's plan.

---

## Maintenance (designed to be boring)

- **Start/stop**: `docker compose up -d` / `docker compose down`
- **Update**: `git pull && docker compose up -d --build`
- **Logs**: `docker compose logs -f telegram` (or any service)
- **Backup**: copy `./data/health.db` — that's your entire state
- **Backfill more history**: `docker compose run --rm scheduler python scripts/backfill.py 180`
- **Garmin token expiry (~6 months)**: re-run `scripts/garmin_login.py`
- **Restart policy**: `restart: unless-stopped` — survives reboots

---

## Security & privacy

- Health data stays in **your** SQLite. Only computed *summaries* go to the LLM
  API — never the raw database.
- The MCP endpoint is protected with a bearer token. It's your health data on a
  public URL — don't run it without one.
- `.env`, `data/`, and Garmin tokens are git-ignored and never enter the repo.
- The Telegram coach only responds to your `TELEGRAM_CHAT_ID`.

---

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m pytest        # offline tests (DB + analysis)
```
