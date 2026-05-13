"""Thin async wrapper around python-binance's AsyncClient for Futures USDT-M.

We funnel every call through this module so MODE=testnet vs MODE=live is the
only knob that determines the endpoint. python-binance accepts `testnet=True`
on the AsyncClient constructor for Futures.
"""

from __future__ import annotations

import logging
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


class BinanceFutures:
    """Lazy, single-client wrapper. Call .close() on shutdown."""

    def __init__(self) -> None:
        self._client: AsyncClient | None = None
        self._exchange_info: dict[str, Any] | None = None
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
        if self._exchange_info is None:
            c = await self.client()
            self._exchange_info = await c.futures_exchange_info()
            for sym in self._exchange_info["symbols"]:
                self._filters[sym["symbol"]] = {f["filterType"]: f for f in sym["filters"]}
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

    # --- market data --------------------------------------------------------

    async def klines(self, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
        c = await self.client()
        rows = await c.futures_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(
            rows,
            columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "qav", "trades", "tbav", "tqav", "ignore",
            ],
        )
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col])
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        return df

    async def mark_price(self, symbol: str) -> Decimal:
        c = await self.client()
        data = await c.futures_mark_price(symbol=symbol)
        return Decimal(str(data["markPrice"]))

    async def mark_price_info(self, symbol: str) -> dict[str, Any]:
        """Return full mark price info: markPrice, lastFundingRate, nextFundingTime."""
        c = await self.client()
        return await c.futures_mark_price(symbol=symbol)

    async def all_open_positions(self) -> list[dict[str, Any]]:
        """Return full Binance position dicts for all symbols with non-zero positionAmt."""
        c = await self.client()
        data = await c.futures_position_information()
        return [d for d in data if Decimal(str(d.get("positionAmt", "0"))) != 0]

    # --- account ------------------------------------------------------------

    async def account_balance_usdt(self) -> tuple[Decimal, Decimal]:
        """Return (wallet_balance, available_balance) in USDT."""
        c = await self.client()
        data = await c.futures_account_balance()
        for row in data:
            if row.get("asset") == "USDT":
                return Decimal(str(row["balance"])), Decimal(str(row["availableBalance"]))
        return Decimal("0"), Decimal("0")

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        c = await self.client()
        try:
            await c.futures_change_leverage(symbol=symbol, leverage=leverage)
        except Exception as e:  # leverage already set, or invalid → log and continue
            log.warning("set_leverage(%s, %d) failed: %s", symbol, leverage, e)

    # --- orders -------------------------------------------------------------

    async def market_order(self, symbol: str, side: str, qty: Decimal) -> dict[str, Any]:
        c = await self.client()
        binance_side = SIDE_BUY if side.upper() == "BUY" else SIDE_SELL
        return await c.futures_create_order(
            symbol=symbol,
            side=binance_side,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=str(qty),
        )

    async def close_market_order(self, symbol: str, side: str, qty: Decimal) -> dict[str, Any]:
        """reduceOnly market order — safe to call even if SL already closed the position."""
        c = await self.client()
        binance_side = SIDE_BUY if side.upper() == "BUY" else SIDE_SELL
        return await c.futures_create_order(
            symbol=symbol,
            side=binance_side,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=str(qty),
            reduceOnly=True,
        )

    async def open_position_amounts(self) -> dict[str, Decimal]:
        """Return {symbol: positionAmt} for symbols with a non-zero Binance position.

        Positive = long, negative = short (one-way mode).
        """
        c = await self.client()
        data = await c.futures_position_information()
        result: dict[str, Decimal] = {}
        for d in data:
            amt = Decimal(str(d.get("positionAmt", "0")))
            if amt != 0:
                result[d["symbol"]] = amt
        return result

    async def recent_user_trades(self, symbol: str, limit: int = 10) -> list[dict[str, Any]]:
        """Return recent fill records for a symbol (most-recent last, as Binance returns them)."""
        c = await self.client()
        return await c.futures_account_trades(symbol=symbol, limit=limit)

    async def stop_market_reduce_only(
        self, symbol: str, side: str, stop_price: Decimal
    ) -> dict[str, Any]:
        c = await self.client()
        binance_side = SIDE_BUY if side.upper() == "BUY" else SIDE_SELL
        return await c.futures_create_order(
            symbol=symbol,
            side=binance_side,
            type=FUTURE_ORDER_TYPE_STOP_MARKET,
            stopPrice=str(stop_price),
            closePosition=True,
            workingType="MARK_PRICE",
        )

    async def take_profit_market_reduce_only(
        self, symbol: str, side: str, stop_price: Decimal
    ) -> dict[str, Any]:
        """TAKE_PROFIT_MARKET order — fires when mark price crosses stop_price upward (SELL) or downward (BUY)."""
        c = await self.client()
        binance_side = SIDE_BUY if side.upper() == "BUY" else SIDE_SELL
        return await c.futures_create_order(
            symbol=symbol,
            side=binance_side,
            type="TAKE_PROFIT_MARKET",
            stopPrice=str(stop_price),
            closePosition=True,
            workingType="MARK_PRICE",
        )

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        c = await self.client()
        try:
            await c.futures_cancel_order(symbol=symbol, orderId=order_id)
        except Exception as e:
            log.warning("cancel_order(%s, %s) failed: %s", symbol, order_id, e)


_singleton: BinanceFutures | None = None


def get_binance() -> BinanceFutures:
    global _singleton
    if _singleton is None:
        _singleton = BinanceFutures()
    return _singleton
