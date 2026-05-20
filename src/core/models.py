from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import JSON, DateTime, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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
    AI_EXIT = "AI_EXIT"  # AI portfolio call OR exit-monitor poll


class Base(DeclarativeBase):
    pass


class Settings(Base):
    """Singleton row (id=1) holding all runtime-configurable knobs."""

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Mode and autotrade gate.
    mode: Mapped[str] = mapped_column(String, default="testnet")
    autotrade_enabled: Mapped[bool] = mapped_column(default=False)

    # Safety caps applied to every AI-issued trade.
    max_leverage_cap: Mapped[int] = mapped_column(Integer, default=10)
    max_equity_per_trade_pct: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=Decimal("20.0")
    )

    # Exit-monitor cadence (minutes). 1h bar-close cycle runs separately.
    exit_poll_minutes: Mapped[int] = mapped_column(Integer, default=30)

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

    # FK-ish (not enforced) to the AIDecision that opened this position.
    entry_decision_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

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
    """Daily/on-demand AI evaluator output (Sonnet 4.5)."""

    __tablename__ = "ai_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger: Mapped[str] = mapped_column(String)  # 'on_demand' | 'daily'
    model: Mapped[str] = mapped_column(String)
    trades_count: Mapped[int] = mapped_column(Integer)
    report_md: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.utcnow()
    )


class AIDecision(Base):
    """Audit log for AI portfolio + exit-monitor decisions."""

    __tablename__ = "ai_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_type: Mapped[str] = mapped_column(String, index=True)  # 'PORTFOLIO' | 'EXIT_MONITOR'
    symbol: Mapped[str] = mapped_column(String, index=True)
    side: Mapped[str | None] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String)  # OPEN_LONG / OPEN_SHORT / CLOSE / HOLD
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str] = mapped_column(String)
    raw_response: Mapped[str | None] = mapped_column(String, nullable=True)
    position_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # AI-issued trade params (snapshotted at decision time, pre-cap-clamp).
    size_pct_equity: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    leverage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sl_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 10), nullable=True)
    tp_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 10), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.utcnow()
    )
