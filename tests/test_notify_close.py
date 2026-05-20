"""Smoke tests for notify_position_closed: it must never raise."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from src.tgbot import notifier


@pytest.mark.parametrize(
    "reason, pnl",
    [
        ("TP", Decimal("12.34")),
        ("SL", Decimal("-8.5")),
        ("MANUAL", Decimal("0")),
        ("AI_EXIT", Decimal("4.2")),
        ("LIQUIDATION", Decimal("-150")),
        ("UNKNOWN_REASON", Decimal("1")),
    ],
)
def test_notify_close_never_raises(reason: str, pnl: Decimal) -> None:
    async def run() -> None:
        with patch.object(notifier, "notify", new=AsyncMock()) as mock_notify:
            await notifier.notify_position_closed(
                side="LONG",
                symbol="BTCUSDT",
                entry_price=Decimal("60000"),
                exit_price=Decimal("61000"),
                pnl=pnl,
                reason=reason,
                confidence=80 if reason == "AI_EXIT" else None,
                ai_reasoning="reversal detected" if reason == "AI_EXIT" else None,
            )
            assert mock_notify.await_count == 1
            text = mock_notify.await_args.args[0]
            assert "BTCUSDT" in text
            # Profit emoji
            if pnl >= 0:
                assert "✅" in text
            else:
                assert "🔴" in text

    asyncio.run(run())


def test_notify_close_swallows_underlying_errors() -> None:
    async def run() -> None:
        with patch.object(notifier, "notify", new=AsyncMock(side_effect=RuntimeError("boom"))):
            # Must NOT raise — the helper exists precisely to be bulletproof.
            await notifier.notify_position_closed(
                side="SHORT",
                symbol="HYPEUSDT",
                entry_price=Decimal("25"),
                exit_price=Decimal("23"),
                pnl=Decimal("10"),
                reason="MANUAL",
            )

    asyncio.run(run())
