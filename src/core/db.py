from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import get_config
from src.core.models import Base, Settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _ensure_engine() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    global _engine, _session_factory
    if _engine is None:
        cfg = get_config()
        _engine = create_async_engine(cfg.db_url, future=True)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    assert _session_factory is not None
    return _engine, _session_factory


async def _migrate(engine: AsyncEngine) -> None:
    """Add columns introduced after initial schema creation (idempotent)."""
    migrations = [
        "ALTER TABLE settings ADD COLUMN tp_pct NUMERIC(10,4) DEFAULT 3.0 NOT NULL",
        "ALTER TABLE positions ADD COLUMN tp_price NUMERIC(30,10)",
        "ALTER TABLE positions ADD COLUMN tp_order_id VARCHAR",
        "ALTER TABLE settings ADD COLUMN trade_amount NUMERIC(18,4) DEFAULT 100.0 NOT NULL",
        "ALTER TABLE settings ADD COLUMN trailing_trigger_pct NUMERIC(10,4) DEFAULT 1.0 NOT NULL",
        "ALTER TABLE settings ADD COLUMN ai_entry_filter_enabled BOOLEAN DEFAULT 1 NOT NULL",
        "ALTER TABLE settings ADD COLUMN ai_early_exit_enabled BOOLEAN DEFAULT 1 NOT NULL",
        "ALTER TABLE settings ADD COLUMN ai_min_confidence INTEGER DEFAULT 60 NOT NULL",
    ]
    async with engine.begin() as conn:
        for sql in migrations:
            with contextlib.suppress(Exception):
                await conn.execute(text(sql))


async def init_db() -> None:
    engine, factory = _ensure_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _migrate(engine)
    # Ensure singleton settings row exists.
    async with factory() as s:
        existing = await s.get(Settings, 1)
        if existing is None:
            s.add(Settings(id=1, mode=get_config().mode.value))
            await s.commit()


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    _, factory = _ensure_engine()
    async with factory() as s:
        yield s


async def dispose() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
