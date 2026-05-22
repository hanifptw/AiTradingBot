# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Telegram-controlled crypto trading bot for **Binance Futures USDT-M**. Trades a fixed universe of perpetuals; **all open/close/sizing/leverage/SL/TP decisions are made by an LLM** (default Grok 4.20 via OpenRouter) on each 1h bar close, with an exit-monitor poll between bars. A separate evaluator model (Sonnet 4.5) reviews closed trades daily.

The bot supports both Binance Futures testnet and live endpoints. Switching between them requires different API key pairs in `.env` and a process restart.

## Stack

- Python 3.11+
- `python-binance` (async) — Futures USDT-M client
- `python-telegram-bot` v21 (async)
- `APScheduler` (async) — bar-close cycle, exit-monitor poll, daily AI report, position sync
- `SQLAlchemy` 2.x + SQLite (WAL mode) — persistence
- `pandas` — OHLCV frames for prompt building
- `httpx` — OpenRouter HTTP
- `pydantic` v2 + `pydantic-settings` — config & DTOs
- Package manager: `uv` (preferred) or `poetry`

## Commands

```bash
# Install deps
uv sync                              # or: poetry install

# Run bot (entrypoint)
uv run python -m src.main

# Run tests
uv run pytest                        # all
uv run pytest tests/test_config.py -q   # single test file

# Lint / format
uv run ruff check src tests
uv run ruff format src tests
```

## Environment Variables (.env)

All secret-bearing vars below are required — `AppConfig` fails fast at boot if any are missing (see `src/config.py::_require_secrets`).

```
MODE=testnet                                          # testnet | live (needs matching key pair below)
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_IDS=12345,67890                 # comma-separated, auth whitelist
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=anthropic/claude-sonnet-4.5          # daily evaluator (deep)
OPENROUTER_DECISION_MODEL=x-ai/grok-4.20              # live trader (bar-close + exit monitor)
DB_PATH=./data/bot.db
LOG_LEVEL=INFO
TIMEZONE=Asia/Jakarta                                 # APScheduler local TZ (display only; jobs run on UTC cron)
```

## Module Layout

```
src/
├── main.py              entrypoint: init DB, build Telegram app, init binance, wire notifier,
│                        run startup reconcile, start scheduler, start polling, start executor
├── config.py            pydantic AppConfig from .env; fail-fast on missing secrets
├── core/                models, db (WAL + migration shim), repository, event bus
├── market/              binance_client (single thin async wrapper around python-binance)
├── strategy/            portfolio_agent (the brain), risk (sizing helper)
├── execution/           executor — only module placing real orders; idempotent PENDING→OPEN flow
├── tgbot/               bot + handlers (menu, balance, positions, pnl, monitor, settings,
│                        ai_analysis, history). Named `tgbot` to avoid shadowing python-telegram-bot.
├── ai/                  openrouter_client, prompts, portfolio_decision (live trader),
│                        exit_monitor (between bars), evaluator (daily Sonnet review)
└── scheduler/           APScheduler runner (cron + intervals) + jobs
```

## Architecture (Big Picture)

The bot is split into **independent modules communicating via an in-process event bus** (`src/core/events.py`). Strategy publishes `EntrySignal`/`ExitSignal`; the executor consumes them. Telegram is decoupled and only reads/writes DB.

**Per cycle (1h bar close):**
1. `scheduler.runner` fires `portfolio_bar_close_job` ~10s after bar close (UTC cron).
2. `strategy.portfolio_agent.run_bar_close_cycle` fetches OHLCV for the universe + open positions, builds a single prompt, calls `OPENROUTER_DECISION_MODEL`.
3. For each symbol, the model returns `OPEN_LONG`/`OPEN_SHORT`/`CLOSE`/`HOLD` plus full trade params (size %, lev, SL, TP, confidence).
4. `_apply_decision` clamps against `max_leverage_cap` / `max_equity_per_trade_pct`, gates on `autotrade_enabled` + `ai_min_confidence`, runs a liquidation-distance sanity check, then publishes `EntrySignal`/`ExitSignal`.
5. `execution.executor` consumes events serialized by a per-symbol `asyncio.Lock`. Entry flow: pre-record `Position(status=PENDING)`, MARKET (with `newClientOrderId`), SL, TP, finalize to OPEN. If SL or TP fails, market-close the just-opened position and mark CANCELLED.
6. Between bars, `exit_monitor_job` (interval = `settings.exit_poll_minutes`) re-evaluates open positions only and may publish `ExitSignal`.

**Other scheduler jobs:**
- `sync_positions` (every 2 min): reconciles DB-OPEN positions against Binance — detects SL/TP/liquidation fills the bot didn't observe.
- `daily_ai_report` (00:05 UTC): runs `ai/evaluator.py` against the last 24h of closed trades using `OPENROUTER_MODEL` (Sonnet).

## Startup ordering (matters)

`amain()` in `src/main.py`:
1. `init_db()` — WAL pragmas, schema, idempotent ADD COLUMN migrations, sync `settings.mode` with `cfg.mode`.
2. Build Telegram `Application`, `await app.initialize()` so the bot's HTTP client is ready.
3. `notifier.set_bot(...)` — **before** anything that can publish notifications.
4. Warm `binance.exchange_info()` (cached 6h), validate universe symbols.
5. `reconcile_pending_positions()` — adopt or cancel any `PENDING` Position rows left by a crash mid-entry.
6. Build + start scheduler. (Exposes `_scheduler` for runtime rescheduling.)
7. `tg_run(app)` — start polling for inbound messages.
8. `run_executor()` task on the event bus.

Shutdown waits in-flight scheduler jobs to drain before closing the Binance client.

## Telegram Menu

Inline keyboard: **Saldo · Posisi · PNL · Monitor Coin · History · Setting · AI Analysis**

`Setting` exposes:
- Autotrade toggle (default OFF — dry-run on-ramp; nothing trades until ON, including AI CLOSE)
- Mode (read-only, .env-set; switching needs different API keys + restart)
- Max Leverage Cap (default 10)
- Max Equity per Trade % (default 20)
- Exit-monitor poll minutes (default 30 — reschedules APScheduler job live on change)
- AI Min Confidence (default 60 — both entry and CLOSE gate)

The AI (Grok 4.20) chooses position size, leverage, SL, TP per trade — these settings are **upper caps** the AI cannot exceed, not parameters the user tunes directly.

Auth: every handler + callback (including menu router) is gated by `TELEGRAM_ALLOWED_USER_IDS`. Other updates dropped silently. Settings changes use inline-keyboard `+`/`-` buttons (atomic SQL UPDATE; no read-modify-write race).

## AI Decision Layer

Two LLM calls drive trading. Both publish to `ai_decisions` for audit.

- **`portfolio_decision.decide_portfolio`** (bar close) — `OPENROUTER_DECISION_MODEL` decides per-symbol action with full trade params. JSON-only output; any parse/LLM error → no action this cycle.
- **`exit_monitor.evaluate_open_positions`** (between bars) — same model, restricted to CLOSE/HOLD on currently-open positions. JSON-only; parse error → hold everything.

Fail-safe: every parse path returns `(None, raw)` on error. The agent treats `None` as "no action".

OpenRouter retries only on 5xx / transport errors — never on 4xx (bad key, bad payload would just multiply cost).

## Database (SQLite via SQLAlchemy)

- `settings` (singleton id=1) — runtime config controlled by Telegram + `last_bar_seen_ms` (persisted dedupe)
- `positions` — OPEN / PENDING / CLOSED / CANCELLED, with `client_order_id` for crash-recovery reconcile
- `orders` — Binance order log (whitelisted fields only, see `executor.sanitize_order_resp`)
- `trades` — closed-trade summary (entry, exit, PnL, R-multiple, mode tag)
- `ai_reports` — daily/on-demand AI evaluation history (markdown, model used)
- `ai_decisions` — per-call audit log for portfolio + exit-monitor decisions

## Conventions

- All I/O is `async` (Binance, Telegram, OpenRouter, SQLAlchemy async session).
- Money values: use `Decimal`, never `float`.
- Time: store UTC in DB; format to local only at Telegram display layer. Always use `datetime.now(UTC)`, never `datetime.utcnow()` (deprecated + naive).
- Never log API keys or full order payloads at INFO level. Use `sanitize_order_resp()` from `src/execution/executor.py` before persisting any Binance order response.

## Schema migrations (no Alembic — yet)

We do **not** have Alembic. New columns are added via a small idempotent shim:

1. Add the column to the model in `src/core/models.py`.
2. Append an entry to the `desired` dict in `src/core/db.py::_migrate_sqlite_add_columns`, e.g.
   `"settings": [("new_field", "INTEGER NOT NULL DEFAULT 0")]`.
3. Restart the bot — `init_db()` will `ALTER TABLE ADD COLUMN` if missing, no-op if present.

Limitations: this only handles **ADD COLUMN**. Renames, drops, type changes, or new constraints need either a manual SQL migration step or a real Alembic setup. If a model change is more invasive than ADD COLUMN, stop and ask before pushing — production has live trade history.

## Session/commit semantics

`src/core/repository.py` functions commit inside the session (`await s.commit()` before return). This means:

- The `async with session() as s:` context manager does **not** roll back on exception once a repo function has committed — partial state is already persisted.
- Callers should treat each repo call as its own atomic unit.
- Multi-step writes that must be all-or-nothing belong inside a single repo function so they share one `commit()`.
- For atomic read-modify-write on a single column (e.g. settings increments), use `repo.adjust_setting()` which expresses the operation as a single SQL UPDATE — never read-then-write in Python.

## Not in scope (yet)

- Multi-user / multi-account
- Backtesting framework
- Web dashboard
- Alembic migrations (we use a manual ADD-COLUMN shim — see above)
- Switching testnet↔live at runtime (different API key pairs, restart-only)
