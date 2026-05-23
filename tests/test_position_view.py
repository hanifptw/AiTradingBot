from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd

from src.core.models import Position, Side
from src.strategy.portfolio_agent import _position_view, _prev_close_from_ohlcv


def _make_pos(
    *,
    side: str = Side.LONG.value,
    entry: str = "100",
    sl: str | None = "95",
    tp: str | None = "110",
    opened_minutes_ago: int = 90,
) -> Position:
    p = Position()
    p.id = 1
    p.symbol = "BTCUSDT"
    p.side = side
    p.qty = Decimal("0.1")
    p.entry_price = Decimal(entry)
    p.sl_price = Decimal(sl) if sl is not None else None
    p.tp_price = Decimal(tp) if tp is not None else None
    p.leverage = 5
    p.opened_at = datetime.now(UTC) - timedelta(minutes=opened_minutes_ago)
    return p


def test_long_profit_with_sl_set() -> None:
    pos = _make_pos(side=Side.LONG.value, entry="100", sl="95", tp="110")
    view = _position_view(pos, mark_price=Decimal("103"), prev_close=Decimal("101"))
    # upnl_pct = +3%; sl_distance = 5%; live_R = 0.6
    assert view["upnl_pct"] == 3.0
    assert abs(view["live_r"] - 0.6) < 1e-9
    # dist to TP from 103 in favorable direction = +6.7965%
    assert view["dist_to_tp_pct"] > 6.5 and view["dist_to_tp_pct"] < 7.0
    # dist to SL buffer = (103-95)/103*100 = 7.7669%
    assert view["dist_to_sl_pct"] > 7.5
    # prev_close=101: dist_to_sl_pct_1h_ago = (101-95)/101*100 = 5.94%
    assert view["dist_to_sl_pct_1h_ago"] < view["dist_to_sl_pct"]
    assert view["dist_to_tp_pct_1h_ago"] > view["dist_to_tp_pct"]


def test_long_loss_dist_to_sl_shrinking() -> None:
    pos = _make_pos(side=Side.LONG.value, entry="100", sl="95", tp="110")
    # prev close was at 99, mark now at 97 — drifting toward SL
    view = _position_view(pos, mark_price=Decimal("97"), prev_close=Decimal("99"))
    assert view["upnl_pct"] == -3.0
    assert view["live_r"] is not None and view["live_r"] < 0
    # SL buffer now: (97-95)/97 = 2.06%; was: (99-95)/99 = 4.04% → shrinking
    assert view["dist_to_sl_pct"] < view["dist_to_sl_pct_1h_ago"]


def test_short_profit_signs() -> None:
    pos = _make_pos(side=Side.SHORT.value, entry="100", sl="105", tp="90")
    # Price dropped — SHORT in profit
    view = _position_view(pos, mark_price=Decimal("97"), prev_close=Decimal("99"))
    assert view["upnl_pct"] == 3.0
    # sl_distance = 5%; live_R = 0.6
    assert abs(view["live_r"] - 0.6) < 1e-9
    # dist_to_tp positive (further to fall)
    assert view["dist_to_tp_pct"] > 0
    # dist_to_sl positive (buffer from SL)
    assert view["dist_to_sl_pct"] > 0


def test_null_sl_yields_null_live_r() -> None:
    pos = _make_pos(sl=None, tp="110")
    view = _position_view(pos, mark_price=Decimal("103"), prev_close=Decimal("101"))
    assert view["live_r"] is None
    assert view["dist_to_sl_pct"] is None
    assert view["dist_to_sl_pct_1h_ago"] is None
    # TP fields still computed
    assert view["dist_to_tp_pct"] is not None


def test_null_mark_price_zeroes_everything() -> None:
    pos = _make_pos()
    view = _position_view(pos, mark_price=None, prev_close=Decimal("101"))
    assert view["upnl_pct"] is None
    assert view["live_r"] is None
    assert view["dist_to_tp_pct"] is None
    assert view["dist_to_sl_pct"] is None
    # 1h-ago snapshots still computable since they only need prev_close + sl/tp
    assert view["dist_to_sl_pct_1h_ago"] is not None
    assert view["dist_to_tp_pct_1h_ago"] is not None


def test_null_prev_close_drops_1h_ago_fields() -> None:
    pos = _make_pos()
    view = _position_view(pos, mark_price=Decimal("103"), prev_close=None)
    assert view["dist_to_tp_pct_1h_ago"] is None
    assert view["dist_to_sl_pct_1h_ago"] is None


def test_prev_close_helper_from_dataframe() -> None:
    df = pd.DataFrame({"close": [100.0, 101.5, 102.7]})
    assert _prev_close_from_ohlcv(df) == Decimal("102.7")
    assert _prev_close_from_ohlcv(None) is None
    assert _prev_close_from_ohlcv(pd.DataFrame()) is None


def test_zero_sl_distance_treated_as_null() -> None:
    pos = _make_pos(entry="100", sl="100")  # degenerate: SL at entry
    view = _position_view(pos, mark_price=Decimal("103"), prev_close=Decimal("101"))
    assert view["live_r"] is None
