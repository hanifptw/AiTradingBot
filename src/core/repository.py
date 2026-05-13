from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models import (
    AIReport,
    MonitoredSymbol,
    Order,
    Position,
    PositionStatus,
    Settings,
    SignalState,
    SignalStateRow,
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


# --- Universe ---------------------------------------------------------------

async def replace_universe(s: AsyncSession, items: list[tuple[str, str, int]]) -> None:
    """items = list of (symbol, base_asset, mcap_rank)."""
    await s.execute(delete(MonitoredSymbol))
    for sym, base, rank in items:
        s.add(MonitoredSymbol(symbol=sym, base_asset=base, mcap_rank=rank))
    await s.commit()


async def list_universe(s: AsyncSession) -> list[MonitoredSymbol]:
    res = await s.execute(select(MonitoredSymbol).order_by(MonitoredSymbol.mcap_rank))
    return list(res.scalars().all())


# --- Signal state -----------------------------------------------------------

async def get_state_row(s: AsyncSession, symbol: str) -> SignalStateRow:
    row = await s.get(SignalStateRow, symbol)
    if row is None:
        row = SignalStateRow(symbol=symbol, state=SignalState.IDLE.value)
        s.add(row)
        await s.flush()
    return row


async def save_state_row(
    s: AsyncSession,
    symbol: str,
    *,
    state: SignalState,
    last_k: Decimal | None = None,
    last_d: Decimal | None = None,
    armed_at_bar: datetime | None = None,
    armed_extreme_k: Decimal | None = None,
) -> None:
    row = await get_state_row(s, symbol)
    row.state = state.value
    if last_k is not None:
        row.last_k = last_k
    if last_d is not None:
        row.last_d = last_d
    if armed_at_bar is not None:
        row.armed_at_bar = armed_at_bar
    if armed_extreme_k is not None:
        row.armed_extreme_k = armed_extreme_k
    row.updated_at = datetime.utcnow()
    await s.commit()


async def list_states(s: AsyncSession) -> list[SignalStateRow]:
    res = await s.execute(select(SignalStateRow))
    return list(res.scalars().all())


async def reconcile_states(s: AsyncSession) -> int:
    """Reset IN_LONG/IN_SHORT states that have no matching open position.

    Happens after a crash or failed order placement where the state machine
    advanced but no DB position was created. Returns number of rows reset.
    """
    in_position_states = {SignalState.IN_LONG.value, SignalState.IN_SHORT.value}
    states = await list_states(s)
    reset_count = 0
    for st in states:
        if st.state not in in_position_states:
            continue
        pos = await open_position_for(s, st.symbol)
        if pos is None:
            st.state = SignalState.IDLE.value
            st.updated_at = datetime.utcnow()
            reset_count += 1
    if reset_count:
        await s.commit()
    return reset_count


# --- Positions / orders -----------------------------------------------------

async def create_position(s: AsyncSession, pos: Position) -> Position:
    s.add(pos)
    await s.commit()
    await s.refresh(pos)
    return pos


async def open_positions(s: AsyncSession) -> list[Position]:
    res = await s.execute(
        select(Position).where(Position.status == PositionStatus.OPEN.value)
    )
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
    sl_pct: Decimal | None,
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

    r_multiple: Decimal | None = None
    if sl_pct and sl_pct > 0:
        r_multiple = pnl_pct / sl_pct

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
