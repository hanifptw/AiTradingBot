"""Lightweight in-process pub/sub built on asyncio.Queue.

The Portfolio agent emits EntrySignal/ExitSignal events with the AI's chosen
trade params already populated. The executor is the sole consumer.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal

from src.core.models import Side


@dataclass(frozen=True)
class EntrySignal:
    symbol: str
    side: Side
    price: Decimal  # last close reference; executor still uses MARKET
    size_pct_equity: Decimal  # 0–100, capped by max_equity_per_trade_pct
    leverage: int  # 1..N, capped by max_leverage_cap
    sl_price: Decimal
    tp_price: Decimal
    confidence: int  # 0–100
    decision_id: int  # AIDecision row id (audit trail)
    reason: str = "AI_PORTFOLIO"


@dataclass(frozen=True)
class ExitSignal:
    symbol: str
    position_id: int
    reason: str  # 'AI_EXIT' | 'MANUAL'
    price: Decimal
    decision_id: int | None = None


Event = EntrySignal | ExitSignal


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[Event]] = []
        self._lock = asyncio.Lock()

    async def publish(self, event: Event) -> None:
        async with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            await q.put(event)

    async def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue()
        async with self._lock:
            self._subscribers.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        async with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    async def stream(self) -> AsyncIterator[Event]:
        q = await self.subscribe()
        try:
            while True:
                yield await q.get()
        finally:
            await self.unsubscribe(q)


_bus: EventBus | None = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
