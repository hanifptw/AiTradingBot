"""Daily AI evaluator (deep model, default Claude Sonnet 4.5).

Reads recent closed trades + bot config, asks for diagnosis + actionable tweaks.
Triggered daily via APScheduler and on-demand via Telegram /ai.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import mean

from src.ai import prompts
from src.ai.openrouter_client import chat
from src.config import get_config
from src.core import repository as repo
from src.core.db import session
from src.core.models import AIReport, Trade

log = logging.getLogger(__name__)


def _trade_to_dict(t: Trade) -> dict:
    return {
        "symbol": t.symbol,
        "side": t.side,
        "entry_price": t.entry_price,
        "exit_price": t.exit_price,
        "pnl_usdt": t.pnl_usdt,
        "pnl_pct": t.pnl_pct,
        "r_multiple": t.r_multiple,
        "close_reason": t.close_reason,
        "duration_min": t.duration_sec // 60,
    }


async def generate_report(trigger: str = "on_demand") -> AIReport:
    cfg = get_config()
    cutoff = datetime.utcnow() - timedelta(days=1)
    async with session() as s:
        settings = await repo.get_settings(s)
        trades = await repo.trades_since(s, cutoff)

    if trades:
        total_pnl = sum((t.pnl_usdt for t in trades), Decimal("0"))
        wins = sum(1 for t in trades if t.pnl_usdt > 0)
        win_rate = (wins / len(trades)) * 100
        rs = [float(t.r_multiple) for t in trades if t.r_multiple is not None]
        avg_r = mean(rs) if rs else 0.0
        avg_duration_min = mean(t.duration_sec for t in trades) / 60
    else:
        total_pnl, win_rate, avg_r, avg_duration_min = Decimal("0"), 0.0, 0.0, 0.0

    user_msg = prompts.build_daily_evaluator_user_prompt(
        mode=settings.mode,
        max_leverage_cap=settings.max_leverage_cap,
        max_equity_per_trade_pct=settings.max_equity_per_trade_pct,
        exit_poll_minutes=settings.exit_poll_minutes,
        universe=cfg.universe_symbols,
        trades=[_trade_to_dict(t) for t in trades],
        total_pnl=total_pnl,
        win_rate=win_rate,
        avg_r=avg_r,
        avg_duration_min=avg_duration_min,
    )

    log.info(
        "Requesting AI evaluator (%s) for %d trades via %s",
        trigger,
        len(trades),
        cfg.openrouter_model,
    )
    report_md = await chat(prompts.DAILY_EVALUATOR_SYSTEM, user_msg)

    async with session() as s:
        report = AIReport(
            trigger=trigger,
            model=cfg.openrouter_model,
            trades_count=len(trades),
            report_md=report_md,
        )
        return await repo.save_ai_report(s, report)
