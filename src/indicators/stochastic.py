"""Stochastic Oscillator — manual implementation (no pandas-ta dependency at runtime).

Standard formula:
    raw_k_t  = 100 * (close_t - lowest_low_n) / (highest_high_n - lowest_low_n)
    %K_t     = SMA(raw_k_t, smooth)        # "slow %K"
    %D_t     = SMA(%K_t, d_period)

Defaults: k=14, d=3, smooth=3.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class StochParams:
    k: int = 14
    d: int = 3
    smooth: int = 3


def stochastic(df: pd.DataFrame, params: StochParams) -> pd.DataFrame:
    """Return df with added 'stoch_k' and 'stoch_d' float columns.

    Expects columns: 'high', 'low', 'close'. Rows with insufficient lookback
    are NaN, which is the normal warmup behaviour.
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]

    lowest = low.rolling(window=params.k, min_periods=params.k).min()
    highest = high.rolling(window=params.k, min_periods=params.k).max()

    rng = (highest - lowest).replace(0, pd.NA)
    raw_k = 100 * (close - lowest) / rng

    k = raw_k.rolling(window=params.smooth, min_periods=params.smooth).mean()
    d = k.rolling(window=params.d, min_periods=params.d).mean()

    out = df.copy()
    out["stoch_k"] = k.astype(float)
    out["stoch_d"] = d.astype(float)
    return out
