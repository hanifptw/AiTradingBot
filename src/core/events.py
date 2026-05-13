"""Lightweight in-process pub/sub built on asyncio.Queue.

Strategy emits EntrySignal/ExitSignal/StateChanged events; Execution and Telegram
subscribe. Keeping this in-process avoids the operational overhead of Redis/etc.
for a single-user bot.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal

from src.core.models import Side, SignalState


@dataclass(frozen=True)
class EntrySignal:
    symbol: str
    side: Side
    price: Decimal  # last close as reference; executor uses MARKET


@dataclass(frozen=True)
class ExitSignal:
    symbol: str
    reason: str  # 'TP' (cross back) — SL handled by Binance stop order
    price: Decimal


@dataclass(frozen=True)
class StateChanged:
    symbol: str
    old: SignalState
    new: SignalState


Event = EntrySignal | ExitSignal | StateChanged


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
