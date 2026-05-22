from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
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
        connect_args: dict = {}
        if cfg.db_url.startswith("sqlite"):
            # 30s busy timeout matches the PRAGMA below; aiosqlite uses this on connect.
            connect_args["timeout"] = 30
        _engine = create_async_engine(cfg.db_url, future=True, connect_args=connect_args)
        if cfg.db_url.startswith("sqlite"):
            _install_sqlite_pragmas(_engine)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    assert _session_factory is not None
    return _engine, _session_factory


def _install_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Enable WAL + sane defaults on every new SQLite connection.

    Why: default journal_mode=DELETE serializes writers and deadlocks under our
    concurrent workload (executor + scheduler + telegram + AI all commit independently).
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _connection_record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.execute("PRAGMA busy_timeout=30000;")
        finally:
            cursor.close()


async def init_db() -> None:
    engine, factory = _ensure_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_sqlite_add_columns(conn)
    # Ensure singleton settings row exists and `mode` always matches .env.
    # Mode is the source of truth for which Binance endpoint we hit; keeping
    # DB.mode in sync with cfg.mode prevents Position rows from being tagged
    # with a stale label.
    cfg_mode = get_config().mode.value
    async with factory() as s:
        existing = await s.get(Settings, 1)
        if existing is None:
            s.add(Settings(id=1, mode=cfg_mode))
            await s.commit()
        elif existing.mode != cfg_mode:
            existing.mode = cfg_mode
            await s.commit()


async def _migrate_sqlite_add_columns(conn) -> None:  # noqa: ANN001
    """Idempotently ADD COLUMN for new fields on an existing DB.

    Plain `create_all` doesn't ALTER existing tables, so any new optional column
    added to a model needs a one-line entry here. Safe to run on a fresh DB —
    the column already exists so the ALTER is skipped.
    """
    desired: dict[str, list[tuple[str, str]]] = {
        "settings": [
            ("last_bar_seen_ms", "BIGINT NOT NULL DEFAULT 0"),
            ("ai_min_confidence", "INTEGER NOT NULL DEFAULT 60"),
        ],
        "positions": [("client_order_id", "VARCHAR")],
        "orders": [("client_order_id", "VARCHAR")],
    }
    for table, cols in desired.items():
        res = await conn.execute(text(f"PRAGMA table_info({table})"))
        existing_cols = {row[1] for row in res.fetchall()}
        for name, decl in cols:
            if name not in existing_cols:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {decl}"))


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
