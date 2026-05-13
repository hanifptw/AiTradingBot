from __future__ import annotations

import numpy as np
import pandas as pd

from src.indicators.stochastic import StochParams, stochastic


def _ohlc_from_closes(closes: list[float], *, hl_spread: float = 1.0) -> pd.DataFrame:
    # Synthesize plausible high/low around each close for testing.
    return pd.DataFrame(
        {
            "high": [c + hl_spread for c in closes],
            "low": [c - hl_spread for c in closes],
            "close": closes,
        }
    )


def test_stochastic_in_range():
    rng = np.random.default_rng(seed=42)
    closes = list(np.cumsum(rng.normal(0, 1, size=200)) + 100)
    df = _ohlc_from_closes(closes)
    out = stochastic(df, StochParams())
    valid = out.dropna(subset=["stoch_k", "stoch_d"])
    assert not valid.empty
    assert valid["stoch_k"].between(0, 100).all()
    assert valid["stoch_d"].between(0, 100).all()


def test_stochastic_pegs_at_100_on_uptrend():
    # Use hl_spread=0 (high==low==close) so a strict uptrend yields %K=100.
    closes = list(range(1, 100))
    df = _ohlc_from_closes(closes, hl_spread=0)
    out = stochastic(df, StochParams())
    assert out["stoch_k"].iloc[-1] == 100.0


def test_stochastic_pegs_at_0_on_downtrend():
    closes = list(range(100, 1, -1))
    df = _ohlc_from_closes(closes, hl_spread=0)
    out = stochastic(df, StochParams())
    assert out["stoch_k"].iloc[-1] == 0.0
