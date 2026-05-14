# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Telegram-controlled crypto trading bot for **Binance Futures USDT-M**. Monitors top 20 cryptocurrencies by market cap (excluding stablecoins), executes long/short positions based on a **two-stage Stochastic oscillator signal**, and uses **OpenRouter LLM** to evaluate trading performance.

Status: greenfield — code not yet written. Implementation plan lives at `/Users/hanifptw/.claude/plans/halo-claude-saya-ingin-abstract-castle.md`.

## Stack

- Python 3.11+
- `python-binance` (async) — Futures USDT-M client
- `python-telegram-bot` v21 (async)
- `APScheduler` (async) — kline polling, weekly AI job, trailing stop loop
- `SQLAlchemy` + SQLite — persistence
- `pandas` — OHLCV frames; Stochastic is hand-rolled (no `pandas-ta`, which doesn't support Python 3.11)
- `httpx` — CoinGecko + OpenRouter HTTP
- `pydantic` — config & DTOs
- Package manager: `uv` (preferred) or `poetry`

## Commands

```bash
# Install deps
uv sync                              # or: poetry install

# Run bot (entrypoint)
uv run python -m src.main

# Run tests
uv run pytest                        # all
uv run pytest tests/test_stochastic.py::test_two_stage -q   # single test

# Lint / format
uv run ruff check src tests
uv run ruff format src tests
```

## Environment Variables (.env)

```
MODE=testnet                         # testnet | live
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_IDS=12345,67890
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=anthropic/claude-sonnet-4.5            # weekly evaluator (deep)
OPENROUTER_DECISION_MODEL=anthropic/claude-haiku-4.5    # entry filter + early exit (cheap+fast)
COINGECKO_API_KEY=                   # optional, free tier works
DB_PATH=./data/bot.db
LOG_LEVEL=INFO
```

## Module Layout

```
src/
├── main.py              entrypoint: start scheduler + Telegram + workers
├── config.py            load .env, validate, expose AppConfig
├── core/                models, db, repository, in-process event bus
├── market/              CoinGecko universe + Binance kline/account/order client
├── indicators/          stochastic.py (%K, %D)
├── strategy/            state machine, signal engine, risk/sizing
├── execution/           live_executor, (optional) paper_executor, trailing stop
├── tgbot/               bot + handlers (menu, balance, positions, pnl, monitor, settings, ai_analysis)
│                        — named `tgbot` (not `telegram`) so it doesn't shadow the python-telegram-bot package
├── ai/                  openrouter_client, prompts, evaluator, weekly scheduler
└── scheduler/           APScheduler runner + jobs (kline poll, universe refresh, trailing, AI weekly)
```

## Architecture (Big Picture)

The bot is split into **independent modules communicating via an in-process event bus** (`src/core/events.py`). This keeps strategy, execution, and Telegram decoupled — swapping `live_executor` for `paper_executor` is a one-line change.

**Data flow per tick:**
1. `scheduler.runner` polls Binance klines for each of the 20 symbols at the configured timeframe.
2. `indicators.stochastic` computes %K, %D.
3. `strategy.signal_engine` updates the per-symbol state machine (see below) and emits `EntrySignal` / `ExitSignal` events.
4. `strategy.risk` validates: max positions, equity % sizing, leverage cap.
5. `execution.executor` (live) places orders on Binance Futures and persists to DB.
6. Telegram handlers read DB for UI; settings changes write back and the next tick picks them up.
7. AI evaluation runs on-demand (Telegram button) or weekly (APScheduler), reads `trades` table, calls OpenRouter, persists report.

## Signal State Machine (Critical Logic)

This is the **non-obvious core**. Per symbol, persist state in `signal_states`:

- `IDLE` → no actionable condition.
- `LONG_ARMED` → %K just **crossed up through %D while both <20**. Set on the bar where the cross happened.
- `SHORT_ARMED` → %K just **crossed down through %D while both >80**.
- Transition `LONG_ARMED → ENTER_LONG` only when **%K closes above 20** on a subsequent bar. Mirror for short: `SHORT_ARMED → ENTER_SHORT` when **%K closes below 80**.
- Reset `LONG_ARMED → IDLE` if %K drops back below the prior low without breakout (invalidation). Mirror for short.
- After entry: state becomes `IN_LONG` / `IN_SHORT`. Exit (TP) fires when **%K crosses %D in the opposite direction** (long: %K crosses below %D; short: %K crosses above %D). SL is a separate Binance `STOP_MARKET` reduce-only order.

Why this is non-obvious: "stochastic crossing" can mean %K-vs-%D OR %K-vs-level. Here we use BOTH at different stages — %K/%D cross arms the signal, level breakout (20/80) triggers entry, %K/%D opposite cross triggers TP.

## Telegram Menu

Inline keyboard: **Saldo · Posisi · PNL · Monitor Coin · Setting · AI Analysis**

`Setting` exposes (all runtime-configurable, persisted in `settings` table):
- Timeframe (1m, 5m, 15m, 1h, 4h)
- SL %
- Trailing stop toggle + offset
- Leverage (default 5x)
- Equity % per trade
- Max concurrent positions (default 5)
- Stochastic params (K, D, smooth)
- AI Entry Filter toggle (default ON) — AI pre-trade gate
- AI Early Exit toggle (default ON) — AI bar-close exit monitor
- AI min confidence (default 60) — reject entry if AI confidence below this
- MODE (testnet / live)

Auth: only `TELEGRAM_ALLOWED_USER_IDS` accepted; all other updates dropped silently.

Setting changes use slash commands (no conversation handler): `/set <field> <value>`. Field whitelist lives in `src/tgbot/handlers/settings.py::_parse_field`.

Important: **autotrade is OFF by default** (`settings.autotrade_enabled`). The bot polls klines and advances state immediately, but won't place orders until you `/set autotrade on`. This is the dry-run on-ramp before flipping to testnet/live execution.

## Demo vs Live

`MODE=testnet` swaps the Binance base URL to `https://testnet.binancefuture.com` and uses testnet API keys. Same code path — no separate executor needed. Trades logged to DB are tagged with the mode.

## AI Evaluation

`ai/evaluator.py` builds a structured summary (last N closed trades, win rate, avg R, current settings) and sends it to OpenRouter (model = `OPENROUTER_MODEL`, default Sonnet 4.5). The prompt asks for: (1) pattern detection in losing trades, (2) parameter tuning suggestions, (3) discipline/risk observations. Reports stored in `ai_reports`. Triggered on-demand from Telegram and via a weekly APScheduler cron.

## AI Decision Layer (Entry Filter + Early Exit)

Two real-time AI gates wrap the Stochastic strategy. Both call `OPENROUTER_DECISION_MODEL` (default `anthropic/claude-haiku-4.5` — cheap, fast, enough reasoning for TA multi-faktor). Sonnet 4.5 stays reserved for the weekly evaluator.

- **Entry filter** (`ai/decision.py::confirm_entry`) — on every `EntrySignal`, before `set_leverage` in `execution/executor.py::_handle_entry`. AI evaluates market structure (HH/HL vs LH/LL), momentum alignment, supply/demand proximity to entry, and R:R viability. Returns JSON `{approve, confidence, reason, concerns}`. Rejected if `approve=false` OR `confidence < settings.ai_min_confidence`. Rejections logged to `ai_decisions` + Telegram notify.
- **Early exit** (`ai/decision.py::should_exit_early`) — runs inside the kline poll job (`scheduler/jobs.py::_process`), once per bar close per symbol that has an open position. Reuses the kline DataFrame already fetched — **no extra Binance HTTP**. AI looks for (a) trend reversal against position, (b) decent profit + reversal forming. Returns JSON `{exit, confidence, reason}`. If `exit=true`, publishes `ExitSignal(reason="AI_EARLY_EXIT")` which `_handle_exit` consumes like any other exit.

Fail-safe: any LLM/parse error → entry rejected, exit held.

All AI decisions persist to `ai_decisions` (decision_type, action, confidence, reason, model, raw_response, position_id) for audit. The two settings toggles `ai_entry_filter_enabled` / `ai_early_exit_enabled` default ON; flip from the Telegram Setting menu.

## Database (SQLite via SQLAlchemy)

- `settings` (singleton) — runtime config controlled by Telegram (incl. AI toggles)
- `monitored_symbols` — current top 20, cached market-cap rank
- `signal_states` — per-symbol state machine snapshot
- `positions` — open + closed positions
- `orders` — raw Binance order log
- `trades` — closed-trade summary (entry, exit, PnL, R-multiple, mode tag)
- `ai_reports` — weekly/on-demand AI evaluation history (markdown, model used)
- `ai_decisions` — per-call audit log for entry filter + early-exit decisions

## Conventions

- All I/O is `async` (Binance, Telegram, OpenRouter, SQLAlchemy async session).
- Money values: use `Decimal`, never `float`.
- Time: store UTC in DB; format to local only at Telegram display layer.
- Never log API keys or full order payloads at INFO level.

## Not in scope (yet)

- Multi-user / multi-account
- Backtesting framework
- Web dashboard
- Indicators other than Stochastic
