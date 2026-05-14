from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import JSON, DateTime, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class SignalState(str, Enum):
    IDLE = "IDLE"
    LONG_ARMED = "LONG_ARMED"
    SHORT_ARMED = "SHORT_ARMED"
    IN_LONG = "IN_LONG"
    IN_SHORT = "IN_SHORT"


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class CloseReason(str, Enum):
    TP = "TP"
    SL = "SL"
    MANUAL = "MANUAL"
    LIQUIDATED = "LIQUIDATED"


class Base(DeclarativeBase):
    pass


class Settings(Base):
    """Singleton row (id=1) holding all runtime-configurable knobs."""

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    timeframe: Mapped[str] = mapped_column(String, default="15m")
    sl_pct: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal("2.0"))
    trailing_enabled: Mapped[bool] = mapped_column(default=False)
    trailing_trigger_pct: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal("1.0"))
    trailing_offset_pct: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal("0.5"))
    leverage: Mapped[int] = mapped_column(Integer, default=5)
    equity_pct: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal("2.0"))
    trade_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("100.0"))
    max_positions: Mapped[int] = mapped_column(Integer, default=5)

    stoch_k: Mapped[int] = mapped_column(Integer, default=14)
    stoch_d: Mapped[int] = mapped_column(Integer, default=3)
    stoch_smooth: Mapped[int] = mapped_column(Integer, default=3)

    tp_pct: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal("3.0"))
    mode: Mapped[str] = mapped_column(String, default="testnet")
    autotrade_enabled: Mapped[bool] = mapped_column(default=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.utcnow()
    )


class MonitoredSymbol(Base):
    __tablename__ = "monitored_symbols"

    symbol: Mapped[str] = mapped_column(String, primary_key=True)  # e.g. BTCUSDT
    base_asset: Mapped[str] = mapped_column(String)
    mcap_rank: Mapped[int] = mapped_column(Integer)
    last_refreshed: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.utcnow()
    )


class SignalStateRow(Base):
    __tablename__ = "signal_states"

    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[str] = mapped_column(String, default=SignalState.IDLE.value)
    last_k: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    last_d: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    armed_at_bar: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    armed_extreme_k: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.utcnow()
    )


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    side: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default=PositionStatus.OPEN.value, index=True)
    mode: Mapped[str] = mapped_column(String)

    qty: Mapped[Decimal] = mapped_column(Numeric(30, 10))
    entry_price: Mapped[Decimal] = mapped_column(Numeric(30, 10))
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 10), nullable=True)
    leverage: Mapped[int] = mapped_column(Integer)
    sl_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 10), nullable=True)
    sl_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tp_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 10), nullable=True)
    tp_order_id: Mapped[str | None] = mapped_column(String, nullable=True)

    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(30, 10), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.utcnow()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_positions_open", "symbol", "status"),)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    side: Mapped[str] = mapped_column(String)
    type: Mapped[str] = mapped_column(String)
    qty: Mapped[Decimal] = mapped_column(Numeric(30, 10))
    price: Mapped[Decimal | None] = mapped_column(Numeric(30, 10), nullable=True)
    binance_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.utcnow()
    )


class Trade(Base):
    """Closed-trade summary, one row per closed position."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(Integer, unique=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    side: Mapped[str] = mapped_column(String)
    mode: Mapped[str] = mapped_column(String)
    qty: Mapped[Decimal] = mapped_column(Numeric(30, 10))
    entry_price: Mapped[Decimal] = mapped_column(Numeric(30, 10))
    exit_price: Mapped[Decimal] = mapped_column(Numeric(30, 10))
    leverage: Mapped[int] = mapped_column(Integer)
    pnl_usdt: Mapped[Decimal] = mapped_column(Numeric(30, 10))
    pnl_pct: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    r_multiple: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    close_reason: Mapped[str] = mapped_column(String)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_sec: Mapped[int] = mapped_column(Integer)

    __table_args__ = (UniqueConstraint("position_id", name="uq_trades_position"),)


class AIReport(Base):
    __tablename__ = "ai_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger: Mapped[str] = mapped_column(String)  # 'on_demand' | 'weekly'
    model: Mapped[str] = mapped_column(String)
    trades_count: Mapped[int] = mapped_column(Integer)
    report_md: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.utcnow()
    )
