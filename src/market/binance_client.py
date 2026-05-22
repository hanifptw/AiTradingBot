"""Thin async wrapper around python-binance's AsyncClient for Futures USDT-M.

We funnel every call through this module so MODE=testnet vs MODE=live is the
only knob that determines the endpoint. python-binance accepts `testnet=True`
on the AsyncClient constructor for Futures.
"""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any

import pandas as pd
from binance import AsyncClient
from binance.enums import (
    FUTURE_ORDER_TYPE_MARKET,
    FUTURE_ORDER_TYPE_STOP_MARKET,
    SIDE_BUY,
    SIDE_SELL,
)

from src.config import Mode, get_config

log = logging.getLogger(__name__)

# Network-call timeouts (seconds). A hung Binance HTTP call would otherwise
# freeze the executor consumer loop (single subscriber), starving all later
# entry/exit signals.
HTTP_TIMEOUT = 15.0
ORDER_HTTP_TIMEOUT = 20.0
EXCHANGE_INFO_TIMEOUT = 30.0

# How long to trust the cached exchangeInfo (stepSize/tickSize/minNotional).
# Binance changes these rarely (delistings, precision tweaks) but we don't
# want a long-running bot to quantize against year-old filters.
EXCHANGE_INFO_TTL_SECONDS = 6 * 3600  # 6h


class BinanceFutures:
    """Lazy, single-client wrapper. Call .close() on shutdown."""

    def __init__(self) -> None:
        self._client: AsyncClient | None = None
        self._exchange_info: dict[str, Any] | None = None
        self._exchange_info_fetched_at: float = 0.0
        self._filters: dict[str, dict[str, Any]] = {}

    async def client(self) -> AsyncClient:
        if self._client is None:
            cfg = get_config()
            self._client = await AsyncClient.create(
                api_key=cfg.binance_api_key,
                api_secret=cfg.binance_api_secret,
                testnet=(cfg.mode is Mode.TESTNET),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close_connection()
            self._client = None

    # --- exchange info / filters -------------------------------------------

    async def exchange_info(self) -> dict[str, Any]:
        stale = (time.monotonic() - self._exchange_info_fetched_at) > EXCHANGE_INFO_TTL_SECONDS
        if self._exchange_info is None or stale:
            c = await self.client()
            self._exchange_info = await asyncio.wait_for(
                c.futures_exchange_info(), timeout=EXCHANGE_INFO_TIMEOUT
            )
            self._filters = {
                sym["symbol"]: {f["filterType"]: f for f in sym["filters"]}
                for sym in self._exchange_info["symbols"]
            }
            self._exchange_info_fetched_at = time.monotonic()
        return self._exchange_info

    async def usdt_perpetual_symbols(self) -> set[str]:
        info = await self.exchange_info()
        return {
            s["symbol"]
            for s in info["symbols"]
            if s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
        }

    def quantize_qty(self, symbol: str, qty: Decimal) -> Decimal:
        step = Decimal(self._filters[symbol]["LOT_SIZE"]["stepSize"])
        if step == 0:
            return qty
        return (qty // step) * step

    def quantize_price(self, symbol: str, price: Decimal) -> Decimal:
        tick = Decimal(self._filters[symbol]["PRICE_FILTER"]["tickSize"])
        if tick == 0:
            return price
        return (price // tick) * tick

    def validate_order_size(
        self, symbol: str, qty: Decimal, ref_price: Decimal
    ) -> tuple[bool, str]:
        """Check qty meets LOT_SIZE.minQty and notional meets MIN_NOTIONAL.

        Returns (ok, reason). Caller should reject the entry if not ok.
        """
        filters = self._filters.get(symbol)
        if filters is None:
            return False, "filters_not_loaded"

        lot = filters.get("LOT_SIZE", {})
        min_qty = Decimal(lot.get("minQty", "0") or "0")
        if min_qty > 0 and qty < min_qty:
            return False, f"qty {qty} < min_qty {min_qty}"

        # Futures uses MIN_NOTIONAL or NOTIONAL filter depending on version.
        notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
        min_notional_raw = notional_filter.get("notional") or notional_filter.get(
            "minNotional"
        )
        if min_notional_raw:
            min_notional = Decimal(str(min_notional_raw))
            notional = qty * ref_price
            if notional < min_notional:
                return False, f"notional {notional} < min_notional {min_notional}"

        return True, ""

    # --- market data --------------------------------------------------------

    async def klines(self, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
        c = await self.client()
        rows = await asyncio.wait_for(
            c.futures_klines(symbol=symbol, interval=interval, limit=limit),
            timeout=HTTP_TIMEOUT,
        )
        df = pd.DataFrame(
            rows,
            columns=[
                "open_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time",
                "qav",
                "trades",
                "tbav",
                "tqav",
                "ignore",
            ],
        )
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col])
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        return df

    async def mark_price(self, symbol: str) -> Decimal:
        c = await self.client()
        data = await asyncio.wait_for(c.futures_mark_price(symbol=symbol), timeout=HTTP_TIMEOUT)
        return Decimal(str(data["markPrice"]))

    async def mark_price_info(self, symbol: str) -> dict[str, Any]:
        """Return full mark price info: markPrice, lastFundingRate, nextFundingTime."""
        c = await self.client()
        return await asyncio.wait_for(c.futures_mark_price(symbol=symbol), timeout=HTTP_TIMEOUT)

    async def all_open_positions(self) -> list[dict[str, Any]]:
        """Return full Binance position dicts for all symbols with non-zero positionAmt."""
        c = await self.client()
        data = await asyncio.wait_for(c.futures_position_information(), timeout=HTTP_TIMEOUT)
        return [d for d in data if Decimal(str(d.get("positionAmt", "0"))) != 0]

    # --- account ------------------------------------------------------------

    async def account_balance_usdt(self) -> tuple[Decimal, Decimal]:
        """Return (wallet_balance, available_balance) in USDT."""
        c = await self.client()
        data = await asyncio.wait_for(c.futures_account_balance(), timeout=HTTP_TIMEOUT)
        for row in data:
            if row.get("asset") == "USDT":
                return Decimal(str(row["balance"])), Decimal(str(row["availableBalance"]))
        return Decimal("0"), Decimal("0")

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set isolated-or-cross leverage for a symbol.

        Raises on any failure — the caller MUST decide whether to abort the
        entry. Silently swallowing this risks executing at stale leverage.
        """
        c = await self.client()
        await asyncio.wait_for(
            c.futures_change_leverage(symbol=symbol, leverage=leverage),
            timeout=HTTP_TIMEOUT,
        )

    async def position_amount(self, symbol: str) -> Decimal:
        """Live positionAmt from Binance (positive long / negative short / 0 flat)."""
        c = await self.client()
        data = await asyncio.wait_for(
            c.futures_position_information(symbol=symbol), timeout=HTTP_TIMEOUT
        )
        # One-way mode returns a single row per symbol.
        for d in data:
            if d.get("symbol") == symbol:
                return Decimal(str(d.get("positionAmt", "0")))
        return Decimal("0")

    async def cancel_all_open_orders(self, symbol: str) -> None:
        """Cancel every working order on this symbol.

        Used to wipe stale SL/TP from a closed position before opening a new
        one, so a leftover closePosition=True order can't fire on the next
        entry.
        """
        c = await self.client()
        try:
            await asyncio.wait_for(
                c.futures_cancel_all_open_orders(symbol=symbol), timeout=HTTP_TIMEOUT
            )
        except Exception as e:
            log.warning("cancel_all_open_orders(%s) failed: %s", symbol, e)

    # --- orders -------------------------------------------------------------

    async def market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        *,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        c = await self.client()
        binance_side = SIDE_BUY if side.upper() == "BUY" else SIDE_SELL
        kwargs: dict[str, Any] = dict(
            symbol=symbol,
            side=binance_side,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=str(qty),
        )
        if client_order_id:
            kwargs["newClientOrderId"] = client_order_id
        return await asyncio.wait_for(
            c.futures_create_order(**kwargs), timeout=ORDER_HTTP_TIMEOUT
        )

    async def close_market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        *,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """reduceOnly market order — safe to call even if SL already closed the position."""
        c = await self.client()
        binance_side = SIDE_BUY if side.upper() == "BUY" else SIDE_SELL
        kwargs: dict[str, Any] = dict(
            symbol=symbol,
            side=binance_side,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=str(qty),
            reduceOnly=True,
        )
        if client_order_id:
            kwargs["newClientOrderId"] = client_order_id
        return await asyncio.wait_for(
            c.futures_create_order(**kwargs), timeout=ORDER_HTTP_TIMEOUT
        )

    async def open_position_amounts(self) -> dict[str, Decimal]:
        """Return {symbol: positionAmt} for symbols with a non-zero Binance position.

        Positive = long, negative = short (one-way mode).
        """
        c = await self.client()
        data = await asyncio.wait_for(c.futures_position_information(), timeout=HTTP_TIMEOUT)
        result: dict[str, Decimal] = {}
        for d in data:
            amt = Decimal(str(d.get("positionAmt", "0")))
            if amt != 0:
                result[d["symbol"]] = amt
        return result

    async def recent_user_trades(self, symbol: str, limit: int = 10) -> list[dict[str, Any]]:
        """Return recent fill records for a symbol (most-recent last, as Binance returns them)."""
        c = await self.client()
        return await asyncio.wait_for(
            c.futures_account_trades(symbol=symbol, limit=limit), timeout=HTTP_TIMEOUT
        )

    async def stop_market_reduce_only(
        self,
        symbol: str,
        side: str,
        stop_price: Decimal,
        *,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        c = await self.client()
        binance_side = SIDE_BUY if side.upper() == "BUY" else SIDE_SELL
        kwargs: dict[str, Any] = dict(
            symbol=symbol,
            side=binance_side,
            type=FUTURE_ORDER_TYPE_STOP_MARKET,
            stopPrice=str(stop_price),
            closePosition=True,
            workingType="MARK_PRICE",
        )
        if client_order_id:
            kwargs["newClientOrderId"] = client_order_id
        return await asyncio.wait_for(
            c.futures_create_order(**kwargs), timeout=ORDER_HTTP_TIMEOUT
        )

    async def take_profit_market_reduce_only(
        self,
        symbol: str,
        side: str,
        stop_price: Decimal,
        *,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """TAKE_PROFIT_MARKET order — fires when mark price crosses stop_price upward (SELL) or downward (BUY)."""
        c = await self.client()
        binance_side = SIDE_BUY if side.upper() == "BUY" else SIDE_SELL
        kwargs: dict[str, Any] = dict(
            symbol=symbol,
            side=binance_side,
            type="TAKE_PROFIT_MARKET",
            stopPrice=str(stop_price),
            closePosition=True,
            workingType="MARK_PRICE",
        )
        if client_order_id:
            kwargs["newClientOrderId"] = client_order_id
        return await asyncio.wait_for(
            c.futures_create_order(**kwargs), timeout=ORDER_HTTP_TIMEOUT
        )

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        c = await self.client()
        try:
            await asyncio.wait_for(
                c.futures_cancel_order(symbol=symbol, orderId=order_id),
                timeout=HTTP_TIMEOUT,
            )
        except Exception as e:
            log.warning("cancel_order(%s, %s) failed: %s", symbol, order_id, e)


_singleton: BinanceFutures | None = None


def get_binance() -> BinanceFutures:
    global _singleton
    if _singleton is None:
        _singleton = BinanceFutures()
    return _singleton


async def reset_binance() -> None:
    """Tear down the cached Binance client.

    Mostly exists for tests and clean shutdown — the production bot uses one
    set of keys per process, set via .env, and a real mode switch needs a
    restart (testnet and live require different API key pairs).
    """
    global _singleton
    if _singleton is not None:
        try:
            await _singleton.close()
        except Exception:
            log.exception("Error closing Binance client during reset")
    _singleton = None
