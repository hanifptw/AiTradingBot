from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from src.core.models import Position, SignalStateRow, Trade


def fmt_balance(wallet: Decimal, available: Decimal, mode: str) -> str:
    return (
        f"*Saldo* ({mode})\n"
        f"• Wallet: `{wallet:.2f}` USDT\n"
        f"• Available: `{available:.2f}` USDT"
    )


def _fmt_qty(qty: Decimal) -> str:
    """Remove trailing zeros: 0.0030000000 → 0.003"""
    return f"{float(qty):.8g}"


def _fmt_dur(opened_at: datetime) -> str:
    delta = datetime.utcnow() - opened_at.replace(tzinfo=None)
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m = rem // 60
    if h >= 24:
        d = h // 24
        return f"{d}d {h % 24}h"
    return f"{h}h {m}m"


def fmt_positions(
    binance_positions: list[dict],
    db_map: dict[str, Any] | None = None,
    funding_data: dict[str, dict] | None = None,
) -> str:
    """Format open positions using Binance as the source of truth.

    binance_positions: list of dicts from futures_position_information (non-zero only)
    db_map:            symbol → Position for positions tracked by this bot
    funding_data:      symbol → dict from futures_mark_price (lastFundingRate, nextFundingTime)
    """
    if not binance_positions:
        return "*Posisi*\n_No open positions._"

    db_map = db_map or {}
    funding_data = funding_data or {}
    now_ms = int(datetime.utcnow().timestamp() * 1000)

    lines = [f"*Posisi Terbuka* ({len(binance_positions)})"]
    for bd in binance_positions:
        sym = bd["symbol"]
        pos: Position | None = db_map.get(sym)
        fd = funding_data.get(sym, {})

        # Side from positionAmt sign (one-way mode: positive=LONG, negative=SHORT).
        amt = Decimal(str(bd.get("positionAmt", "0")))
        side = "LONG" if amt > 0 else "SHORT"
        qty = abs(amt)

        entry_raw = bd.get("entryPrice")
        entry = Decimal(str(entry_raw)) if entry_raw else None
        entry_str = f"`{entry:.4f}`" if entry else "`n/a`"

        mark_raw = bd.get("markPrice") or fd.get("markPrice")
        mark = Decimal(str(mark_raw)) if mark_raw else None
        mark_str = f"`{mark:.4f}`" if mark else "`n/a`"

        liq_raw = bd.get("liquidationPrice")
        liq = Decimal(str(liq_raw)) if liq_raw else None
        liq_str = f" liq=`{liq:.4f}`" if (liq and liq > 0) else ""

        lev_raw = bd.get("leverage")
        lev = lev_raw if lev_raw else (str(pos.leverage) if pos else "?")

        # Metadata from DB (only available for positions opened by this bot).
        sl = f"{pos.sl_price:.4f}" if (pos and pos.sl_price) else "—"
        tp = f"{pos.tp_price:.4f}" if (pos and pos.tp_price) else "—"
        dur = _fmt_dur(pos.opened_at) if (pos and pos.opened_at) else "—"
        ext = "" if pos else " _(ext)_"

        # Unrealized PnL from Binance (authoritative).
        upnl_raw = bd.get("unRealizedProfit") or bd.get("unrealizedProfit")
        if upnl_raw is not None:
            upnl = Decimal(str(upnl_raw))
            notional_raw = bd.get("notional")
            notional = Decimal(str(notional_raw)) if notional_raw else None
            try:
                margin = abs(notional) / Decimal(str(lev)) if notional else None
            except Exception:
                margin = None
            upnl_pct = upnl / margin * 100 if (margin and margin > 0) else None
            pct_str = f" (`{upnl_pct:+.1f}%`)" if upnl_pct is not None else ""
            pnl_str = f"`{upnl:+.2f}` USDT{pct_str}"
        elif mark and entry:
            direction = Decimal("1") if side == "LONG" else Decimal("-1")
            upnl = qty * (mark - entry) * direction
            pnl_str = f"`{upnl:+.2f}` USDT"
        else:
            pnl_str = "`n/a`"

        # Estimated funding fee for next period.
        funding_str = ""
        rate_raw = fd.get("lastFundingRate")
        next_ms = int(fd.get("nextFundingTime", 0))
        notional_raw2 = bd.get("notional")
        if rate_raw and next_ms > 0 and notional_raw2:
            rate = Decimal(str(rate_raw))
            notional2 = abs(Decimal(str(notional_raw2)))
            side_mul = Decimal("-1") if side == "LONG" else Decimal("1")
            est_fee = side_mul * notional2 * rate
            mins_left = max(0, (next_ms - now_ms) // 60000)
            h, m = divmod(mins_left, 60)
            sign = "+" if est_fee >= 0 else ""
            funding_str = f"\n  Funding: `{sign}{est_fee:.4f}` USDT (in {h}h {m}m)"

        lines.append(
            f"\n*{sym}* {side}{ext} — _{dur}_\n"
            f"  entry={entry_str} → mark={mark_str}{liq_str}\n"
            f"  qty=`{_fmt_qty(qty)}` | SL=`{sl}` → TP=`{tp}` | lev=`{lev}x`\n"
            f"  uPnL {pnl_str}{funding_str}"
        )
    return "\n".join(lines)


def fmt_pnl_windows(stats: dict[str, tuple[Decimal, int, int]]) -> str:
    lines = ["*PNL*"]
    for label, (pnl, wins, total) in stats.items():
        wr = f"{(wins / total * 100):.1f}%" if total else "—"
        lines.append(f"• {label}: `{pnl:+.2f}` USDT  ({wins}/{total}, WR {wr})")
    return "\n".join(lines)


_STATE_BADGE = {
    "IN_LONG":     "🟢",
    "IN_SHORT":    "🔴",
    "LONG_ARMED":  "🟡",
    "SHORT_ARMED": "🟠",
    "IDLE":        "⚪",
}
_STATE_LABEL = {
    "IN_LONG":     "IN LONG ",
    "IN_SHORT":    "IN SHORT",
    "LONG_ARMED":  "ARMED ▲ ",
    "SHORT_ARMED": "ARMED ▼ ",
    "IDLE":        "watching",
}


def _kzone(k: Decimal | None) -> str:
    if k is None:
        return "  "
    if k < 20:
        return "📉"
    if k > 80:
        return "📈"
    return "  "


def _zone_type(st: SignalStateRow) -> str | None:
    """Return 'OS' if both K&D <20, 'OB' if both K&D >80, else None."""
    if st.last_k is None or st.last_d is None:
        return None
    if st.last_k < 20 and st.last_d < 20:
        return "OS"
    if st.last_k > 80 and st.last_d > 80:
        return "OB"
    return None


def _sort_group(st: SignalStateRow) -> int:
    if st.state in ("IN_LONG", "IN_SHORT"):
        return 0
    if st.state in ("LONG_ARMED", "SHORT_ARMED"):
        return 1
    if _zone_type(st) is not None:
        return 2
    return 3


def fmt_monitor(states: list[SignalStateRow], timeframe: str = "?") -> str:
    if not states:
        return "*👀 Monitor Coin*\n_Universe not loaded yet._"

    # Stable sort preserves mcap rank within each group.
    ordered = sorted(states, key=_sort_group)

    active   = [s for s in ordered if _sort_group(s) == 0]
    armed    = [s for s in ordered if _sort_group(s) == 1]
    in_zone  = [s for s in ordered if _sort_group(s) == 2]
    watching = [s for s in ordered if _sort_group(s) == 3]

    summary_parts = []
    if active:
        summary_parts.append(f"{len(active)} aktif")
    if armed:
        summary_parts.append(f"{len(armed)} armed")
    if in_zone:
        summary_parts.append(f"{len(in_zone)} in zone")
    summary_parts.append(f"{len(watching)} watching")

    lines = [f"*👀 Monitor Coin* `{timeframe}` — {' · '.join(summary_parts)}", ""]

    def _row(st: SignalStateRow) -> str:
        badge = _STATE_BADGE.get(st.state, "⚪")
        label = _STATE_LABEL.get(st.state, st.state)
        k = f"{float(st.last_k):5.1f}" if st.last_k is not None else "  —  "
        d = f"{float(st.last_d):5.1f}" if st.last_d is not None else "  —  "
        zone = _kzone(st.last_k)
        zt = _zone_type(st)

        if st.state in ("IN_LONG", "IN_SHORT"):
            return f"{badge} *{st.symbol}*   `{label}`  K=`{k}` {zone}  D=`{d}`"
        if st.state in ("LONG_ARMED", "SHORT_ARMED"):
            return f"{badge} _{st.symbol}_   `{label}`  K=`{k}` {zone}  D=`{d}`"
        if zt == "OS":
            return f"🔵 _{st.symbol}_   `OS zone ▲`  K=`{k}` {zone}  D=`{d}`"
        if zt == "OB":
            return f"🟣 _{st.symbol}_   `OB zone ▼`  K=`{k}` {zone}  D=`{d}`"
        sym = f"{st.symbol:<12}"
        return f"{badge} {sym}  `{label}`  K=`{k}` {zone}  D=`{d}`"

    if active:
        for st in active:
            lines.append(_row(st))
        lines.append("")

    if armed:
        for st in armed:
            lines.append(_row(st))
        lines.append("")

    if in_zone:
        for st in in_zone:
            lines.append(_row(st))
        lines.append("")

    for st in watching:
        lines.append(_row(st))

    return "\n".join(lines)


def fmt_settings(s) -> str:
    return (
        "*Setting*\n"
        f"• Mode: `{s.mode}`\n"
        f"• Autotrade: `{'ON' if s.autotrade_enabled else 'OFF'}`\n"
        f"• Timeframe: `{s.timeframe}`\n"
        f"• SL: `{s.sl_pct}%` | TP: `{s.tp_pct}%` | Amount: `{s.trade_amount:.0f}` USDT\n"
        f"• Trailing: `{'ON' if s.trailing_enabled else 'OFF'}` "
        f"(offset `{s.trailing_offset_pct}%`)\n"
        f"• Leverage: `{s.leverage}x`\n"
        f"• Equity/trade: `{s.equity_pct}%`\n"
        f"• Max positions: `{s.max_positions}`\n"
        f"• Stoch: K=`{s.stoch_k}` D=`{s.stoch_d}` smooth=`{s.stoch_smooth}`"
    )


def fmt_trade_row(t: Trade) -> str:
    return (
        f"`{t.symbol}` {t.side} pnl=`{t.pnl_usdt:+.2f}` ({t.pnl_pct:+.2f}%) "
        f"reason=`{t.close_reason}` @{t.closed_at:%Y-%m-%d %H:%M}"
    )


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC")
