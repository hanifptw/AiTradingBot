from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models import (
    AIDecision,
    AIReport,
    Order,
    Position,
    PositionStatus,
    Settings,
    Trade,
)

# --- Settings ---------------------------------------------------------------


async def get_settings(s: AsyncSession) -> Settings:
    row = await s.get(Settings, 1)
    if row is None:
        row = Settings(id=1)
        s.add(row)
        await s.flush()
    return row


async def update_setting(s: AsyncSession, **fields: object) -> Settings:
    row = await get_settings(s)
    for k, v in fields.items():
        setattr(row, k, v)
    row.updated_at = datetime.utcnow()
    await s.commit()
    return row


# --- Positions / orders -----------------------------------------------------


async def create_position(s: AsyncSession, pos: Position) -> Position:
    s.add(pos)
    await s.commit()
    await s.refresh(pos)
    return pos


async def open_positions(s: AsyncSession) -> list[Position]:
    res = await s.execute(select(Position).where(Position.status == PositionStatus.OPEN.value))
    return list(res.scalars().all())


async def open_position_for(s: AsyncSession, symbol: str) -> Position | None:
    res = await s.execute(
        select(Position).where(
            Position.symbol == symbol, Position.status == PositionStatus.OPEN.value
        )
    )
    return res.scalars().first()


async def add_order(s: AsyncSession, order: Order) -> Order:
    s.add(order)
    await s.commit()
    await s.refresh(order)
    return order


async def close_position(
    s: AsyncSession,
    pos: Position,
    *,
    exit_price: Decimal,
    realized_pnl: Decimal,
    reason: str,
) -> Trade:
    pos.status = PositionStatus.CLOSED.value
    pos.exit_price = exit_price
    pos.realized_pnl = realized_pnl
    pos.close_reason = reason
    pos.closed_at = datetime.utcnow()

    pnl_pct = (
        ((exit_price - pos.entry_price) / pos.entry_price * 100)
        if pos.side == "LONG"
        else ((pos.entry_price - exit_price) / pos.entry_price * 100)
    )

    # R-multiple: PnL % divided by SL distance % from entry (when available).
    r_multiple: Decimal | None = None
    if pos.sl_price and pos.sl_price > 0 and pos.entry_price > 0:
        sl_dist_pct = abs(pos.entry_price - pos.sl_price) / pos.entry_price * Decimal("100")
        if sl_dist_pct > 0:
            r_multiple = pnl_pct / sl_dist_pct

    duration = int((pos.closed_at - pos.opened_at).total_seconds())

    trade = Trade(
        position_id=pos.id,
        symbol=pos.symbol,
        side=pos.side,
        mode=pos.mode,
        qty=pos.qty,
        entry_price=pos.entry_price,
        exit_price=exit_price,
        leverage=pos.leverage,
        pnl_usdt=realized_pnl,
        pnl_pct=pnl_pct,
        r_multiple=r_multiple,
        close_reason=reason,
        opened_at=pos.opened_at,
        closed_at=pos.closed_at,
        duration_sec=duration,
    )
    s.add(trade)
    await s.commit()
    await s.refresh(trade)
    return trade


# --- Trades / stats ---------------------------------------------------------


async def trades_since(s: AsyncSession, since: datetime) -> list[Trade]:
    res = await s.execute(
        select(Trade).where(Trade.closed_at >= since).order_by(Trade.closed_at.desc())
    )
    return list(res.scalars().all())


async def recent_trades(s: AsyncSession, limit: int = 50) -> list[Trade]:
    res = await s.execute(select(Trade).order_by(Trade.closed_at.desc()).limit(limit))
    return list(res.scalars().all())


async def delete_all_trades(s: AsyncSession) -> int:
    result = await s.execute(delete(Trade))
    await s.commit()
    return result.rowcount


def pnl_window(trades: list[Trade]) -> tuple[Decimal, int, int]:
    total = sum((t.pnl_usdt for t in trades), Decimal("0"))
    wins = sum(1 for t in trades if t.pnl_usdt > 0)
    return total, wins, len(trades)


def windows(now: datetime) -> dict[str, datetime]:
    return {
        "today": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "7d": now - timedelta(days=7),
        "30d": now - timedelta(days=30),
        "all": datetime(1970, 1, 1),
    }


# --- AI reports -------------------------------------------------------------


async def save_ai_report(s: AsyncSession, report: AIReport) -> AIReport:
    s.add(report)
    await s.commit()
    await s.refresh(report)
    return report


async def last_ai_report(s: AsyncSession) -> AIReport | None:
    res = await s.execute(select(AIReport).order_by(AIReport.created_at.desc()).limit(1))
    return res.scalars().first()


# --- AI decisions (portfolio + exit-monitor audit) --------------------------


async def add_ai_decision(s: AsyncSession, decision: AIDecision) -> AIDecision:
    s.add(decision)
    await s.commit()
    await s.refresh(decision)
    return decision


async def recent_ai_decisions(s: AsyncSession, limit: int = 20) -> list[AIDecision]:
    res = await s.execute(select(AIDecision).order_by(AIDecision.created_at.desc()).limit(limit))
    return list(res.scalars().all())


async def latest_decision_per_symbol(
    s: AsyncSession, decision_type: str = "PORTFOLIO"
) -> dict[str, AIDecision]:
    """Latest portfolio decision per symbol — used by Monitor view."""
    res = await s.execute(
        select(AIDecision)
        .where(AIDecision.decision_type == decision_type)
        .order_by(AIDecision.created_at.desc())
        .limit(200)
    )
    out: dict[str, AIDecision] = {}
    for d in res.scalars().all():
        if d.symbol not in out:
            out[d.symbol] = d
    return out
