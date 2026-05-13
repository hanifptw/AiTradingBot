"""Pulls top crypto by market cap from CoinGecko and filters to symbols that
Binance Futures USDT-M actually lists (perpetuals).

Cached for 6 hours; ranking rarely shifts in a way that should re-trigger
the bot's universe.
"""

from __future__ import annotations

import logging
import time

import httpx

from src.config import get_config
from src.market.stablecoins import is_stable_or_wrapped

log = logging.getLogger(__name__)

COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"
_CACHE_TTL = 6 * 3600
_TOP_N = 20

_cache: tuple[float, list[tuple[str, str, int]]] | None = None


async def fetch_top_universe(
    binance_perpetual_symbols: set[str],
    *,
    top_n: int = _TOP_N,
) -> list[tuple[str, str, int]]:
    """Return [(binance_symbol, base_asset_upper, mcap_rank)] of length top_n.

    `binance_perpetual_symbols` is the set of *USDT-M perpetual* symbols on
    Binance (e.g. {"BTCUSDT", "ETHUSDT", ...}). We intersect with CoinGecko's
    ranking so we don't try to trade a coin that has no futures listing.
    """
    global _cache
    now = time.time()
    if _cache and now - _cache[0] < _CACHE_TTL:
        return _cache[1]

    cfg = get_config()
    params: dict[str, str | int] = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": 100,
        "page": 1,
        "sparkline": "false",
    }
    headers = {}
    if cfg.coingecko_api_key:
        headers["x-cg-demo-api-key"] = cfg.coingecko_api_key

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(COINGECKO_URL, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    picked: list[tuple[str, str, int]] = []
    rank = 0
    for row in data:
        symbol = row.get("symbol", "")
        if not symbol or is_stable_or_wrapped(symbol):
            continue
        binance_symbol = f"{symbol.upper()}USDT"
        if binance_symbol not in binance_perpetual_symbols:
            continue
        rank += 1
        picked.append((binance_symbol, symbol.upper(), rank))
        if len(picked) >= top_n:
            break

    if len(picked) < top_n:
        log.warning("Only resolved %d/%d top coins to Binance perpetuals", len(picked), top_n)

    _cache = (now, picked)
    return picked
