from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select, update
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


def _as_utc(dt: datetime) -> datetime:
    """Promote a naive datetime (assumed UTC) to a tz-aware UTC datetime.

    SQLite has no native tz storage; SQLAlchemy can return naive datetimes
    for `DateTime(timezone=True)` columns. Normalize before any arithmetic
    against `datetime.now(UTC)` to avoid TypeError.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


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
    row.updated_at = datetime.now(UTC)
    await s.commit()
    return row


async def adjust_setting(
    s: AsyncSession,
    field: str,
    delta: object,
    *,
    min_value: object,
    max_value: object,
) -> Settings:
    """Atomically `field := clamp(field + delta, min, max)` via a SQL UPDATE.

    Prevents a lost-update race where two concurrent Telegram taps both read
    the old value, both compute new = old + delta, and the second write
    overwrites the first.
    """
    col = getattr(Settings, field)
    new_expr = func.max(min_value, func.min(max_value, col + delta))
    await s.execute(
        update(Settings)
        .where(Settings.id == 1)
        .values({field: new_expr, "updated_at": datetime.now(UTC)})
    )
    await s.commit()
    return await get_settings(s)


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


async def active_position_for(s: AsyncSession, symbol: str) -> Position | None:
    """Return OPEN or PENDING position for a symbol — both block a new entry."""
    res = await s.execute(
        select(Position).where(
            Position.symbol == symbol,
            Position.status.in_(
                (PositionStatus.OPEN.value, PositionStatus.PENDING.value)
            ),
        )
    )
    return res.scalars().first()


async def pending_positions(s: AsyncSession) -> list[Position]:
    """All positions left in PENDING state (used by startup reconcile)."""
    res = await s.execute(
        select(Position).where(Position.status == PositionStatus.PENDING.value)
    )
    return list(res.scalars().all())


async def mark_position_cancelled(
    s: AsyncSession, position_id: int, reason: str
) -> None:
    """Mark a PENDING position as CANCELLED (entry never reached OPEN)."""
    pos = await s.get(Position, position_id)
    if pos is None:
        return
    pos.status = PositionStatus.CANCELLED.value
    pos.close_reason = reason
    pos.closed_at = datetime.now(UTC)
    await s.commit()


async def finalize_pending_position(
    s: AsyncSession,
    position_id: int,
    *,
    qty: Decimal,
    entry_price: Decimal,
    sl_price: Decimal | None,
    sl_order_id: str | None,
    tp_price: Decimal | None,
    tp_order_id: str | None,
) -> Position | None:
    """Transition a PENDING position to OPEN once all protective orders are placed."""
    pos = await s.get(Position, position_id)
    if pos is None:
        return None
    pos.qty = qty
    pos.entry_price = entry_price
    pos.sl_price = sl_price
    pos.sl_order_id = sl_order_id
    pos.tp_price = tp_price
    pos.tp_order_id = tp_order_id
    pos.status = PositionStatus.OPEN.value
    await s.commit()
    await s.refresh(pos)
    return pos


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
    pos.closed_at = datetime.now(UTC)

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

    duration = int((_as_utc(pos.closed_at) - _as_utc(pos.opened_at)).total_seconds())

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
        "all": datetime(1970, 1, 1, tzinfo=UTC),
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
