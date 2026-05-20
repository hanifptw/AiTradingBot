from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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


async def init_db() -> None:
    engine, factory = _ensure_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
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
