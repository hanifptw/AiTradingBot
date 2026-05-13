from __future__ import annotations

import logging
from decimal import Decimal
from statistics import mean

from src.ai import openrouter_client, prompts
from src.config import get_config
from src.core import repository as repo
from src.core.db import session
from src.core.models import AIReport, Trade

log = logging.getLogger(__name__)

_MAX_TRADES_IN_PROMPT = 50


def _format_ledger(trades: list[Trade]) -> str:
    if not trades:
        return "(no closed trades yet)"
    lines = ["| # | symbol | side | entry | exit | pnl USDT | pnl % | R | reason | dur(min) |",
             "|---|--------|------|-------|------|----------|-------|---|--------|----------|"]
    for i, t in enumerate(trades, 1):
        r = f"{t.r_multiple:.2f}" if t.r_multiple is not None else "—"
        lines.append(
            f"| {i} | {t.symbol} | {t.side} | {t.entry_price} | {t.exit_price} | "
            f"{t.pnl_usdt:.2f} | {t.pnl_pct:.2f}% | {r} | {t.close_reason} | "
            f"{t.duration_sec // 60} |"
        )
    return "\n".join(lines)


async def generate_report(trigger: str = "on_demand") -> AIReport:
    cfg = get_config()
    async with session() as s:
        settings = await repo.get_settings(s)
        trades = await repo.recent_trades(s, limit=_MAX_TRADES_IN_PROMPT)

    if trades:
        total_pnl = sum((t.pnl_usdt for t in trades), Decimal("0"))
        wins = sum(1 for t in trades if t.pnl_usdt > 0)
        win_rate = (wins / len(trades)) * 100
        rs = [float(t.r_multiple) for t in trades if t.r_multiple is not None]
        avg_r = mean(rs) if rs else 0.0
        avg_duration_min = mean(t.duration_sec for t in trades) / 60
    else:
        total_pnl, win_rate, avg_r, avg_duration_min = Decimal("0"), 0.0, 0.0, 0.0

    user_msg = prompts.USER_TEMPLATE.format(
        mode=settings.mode,
        timeframe=settings.timeframe,
        sl_pct=settings.sl_pct,
        trailing=f"ON ({settings.trailing_offset_pct}%)" if settings.trailing_enabled else "OFF",
        leverage=settings.leverage,
        equity_pct=settings.equity_pct,
        max_positions=settings.max_positions,
        stoch_k=settings.stoch_k,
        stoch_d=settings.stoch_d,
        stoch_smooth=settings.stoch_smooth,
        n_trades=len(trades),
        total_pnl=f"{total_pnl:.2f}",
        win_rate=f"{win_rate:.1f}",
        avg_r=f"{avg_r:.2f}",
        avg_duration_min=f"{avg_duration_min:.1f}",
        ledger=_format_ledger(trades),
    )

    log.info("Requesting AI report (%s) for %d trades via %s", trigger, len(trades), cfg.openrouter_model)
    report_md = await openrouter_client.chat(prompts.SYSTEM_PROMPT, user_msg)

    async with session() as s:
        report = AIReport(
            trigger=trigger,
            model=cfg.openrouter_model,
            trades_count=len(trades),
            report_md=report_md,
        )
        return await repo.save_ai_report(s, report)
