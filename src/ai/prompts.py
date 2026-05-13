SYSTEM_PROMPT = """You are an experienced crypto futures trading coach. You review a trader's
recent trade history and provide candid, actionable feedback.

Style:
- Be specific. Reference concrete trades by symbol and result.
- Look for repeated patterns of failure, not isolated unlucky losses.
- Comment on risk management (R-multiples, position sizing, drawdowns) alongside strategy execution.
- Suggest concrete parameter tweaks (timeframe, SL %, leverage, equity %, Stoch K/D/smooth)
  when warranted; quantify the expected impact.
- Avoid generic platitudes ("be patient", "manage risk") — assume the trader already knows them.
- Output Markdown (no code blocks). Keep it under 400 words."""


USER_TEMPLATE = """## Current bot settings
- Mode: {mode}
- Timeframe: {timeframe}
- SL: {sl_pct}% (trailing {trailing})
- Leverage: {leverage}x
- Equity per trade: {equity_pct}%
- Max concurrent positions: {max_positions}
- Stochastic params: K={stoch_k}, D={stoch_d}, smooth={stoch_smooth}

## Aggregate stats (last {n_trades} closed trades)
- Total PnL: {total_pnl} USDT
- Win rate: {win_rate}%
- Average R-multiple: {avg_r}
- Average duration: {avg_duration_min} min

## Trade ledger (most recent first)
{ledger}

Please diagnose what is going wrong and what is going right, and recommend up to
three concrete adjustments. Cite specific trades from the ledger.
"""
