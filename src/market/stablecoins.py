"""Stablecoins / pegged assets to skip when picking top-by-mcap.

Matched on CoinGecko symbol (lowercase). Keep this list conservative and update
it when new pegged assets reach the top 30.
"""

STABLECOIN_SYMBOLS: frozenset[str] = frozenset(
    {
        "usdt", "usdc", "dai", "fdusd", "tusd", "usdd", "usde", "pyusd",
        "usdp", "gusd", "frax", "lusd", "susd", "mim", "usdt0", "usds",
        # Wrapped / staked variants of an existing asset (price-pegged to it).
        "wbtc", "weth", "steth", "wsteth", "reth", "cbeth", "wbeth", "sweth", "frxeth",
        "weeth", "ezeth", "rsweth", "rseth", "ankreth", "oseth",
    }
)


def is_stable_or_wrapped(symbol: str) -> bool:
    return symbol.lower() in STABLECOIN_SYMBOLS
