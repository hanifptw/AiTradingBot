# Binance Futures Trading Bot

Telegram-controlled crypto trading bot for **Binance Futures USDT-M**. Monitors the top 20 cryptocurrencies by market cap (excluding stablecoins), executes long/short positions on a **two-stage Stochastic oscillator signal**, and evaluates trading performance via an **OpenRouter LLM**.

> See [`CLAUDE.md`](CLAUDE.md) for the architecture map. See [`docs/STRATEGY.md`](docs/STRATEGY.md) if you want to dig into the signal logic.

## Quick start

```bash
# 1. Install deps (uses uv)
uv sync

# 2. Copy and fill in your keys
cp .env.example .env
$EDITOR .env

# 3. Run (defaults to MODE=testnet)
uv run python -m src.main
```

You'll need:

- A Telegram bot token from [@BotFather](https://t.me/BotFather), and your numeric Telegram user ID (whitelist in `TELEGRAM_ALLOWED_USER_IDS`).
- Binance Futures **testnet** keys from <https://testnet.binancefuture.com/> for `MODE=testnet`. Production keys only once you flip `MODE=live`.
- An [OpenRouter](https://openrouter.ai/) key for AI trade reviews.

## Telegram menu

`/start` opens the main menu:

- **Saldo** — wallet balance & available margin
- **Posisi** — open positions
- **PNL** — realized PnL (today / 7d / 30d / all)
- **Monitor Coin** — the 20 tracked coins with current signal state
- **Setting** — timeframe, SL %, trailing stop, leverage, equity %, max positions, Stoch params, mode
- **AI Analysis** — on-demand AI trade review (also runs weekly)

## Strategy in one paragraph

For each of the 20 symbols, on each closed bar of the configured timeframe, compute Stochastic %K/%D. When %K crosses %D **inside the <20 zone**, arm a long; entry fires once %K **closes above 20**. Mirror in the >80 zone for shorts. Take profit fires on the opposite %K/%D cross. Stop loss is a Binance `STOP_MARKET` reduce-only order at a configurable percentage from entry, with an optional trailing stop.

## Tests

```bash
uv run pytest                       # all
uv run pytest tests/test_stochastic.py -q
```

## Disclaimer

This software is provided as-is, for educational use. Trading crypto futures with leverage carries substantial risk of loss. Run on testnet until you understand the behaviour, and never risk capital you cannot afford to lose.
