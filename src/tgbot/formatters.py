from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from src.core.models import AIDecision, Position, Trade


def fmt_balance(wallet: Decimal, available: Decimal, mode: str) -> str:
    return f"*Saldo* ({mode})\n• Wallet: `{wallet:.2f}` USDT\n• Available: `{available:.2f}` USDT"


def _fmt_qty(qty: Decimal) -> str:
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

        sl = f"{pos.sl_price:.4f}" if (pos and pos.sl_price) else "—"
        tp = f"{pos.tp_price:.4f}" if (pos and pos.tp_price) else "—"
        dur = _fmt_dur(pos.opened_at) if (pos and pos.opened_at) else "—"
        ext = "" if pos else " _(ext)_"

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


def fmt_pnl_windows(
    stats: dict[str, tuple[Decimal, int, int]],
    upnl: Decimal | None = None,
    open_count: int = 0,
) -> str:
    lines = ["*PNL*"]
    if upnl is not None:
        sign = "+" if upnl >= 0 else ""
        lines.append(f"• Unrealized: `{sign}{upnl:.2f}` USDT  ({open_count} posisi terbuka)")
        lines.append("")
    for label, (pnl, wins, total) in stats.items():
        wr = f"{(wins / total * 100):.1f}%" if total else "—"
        lines.append(f"• {label}: `{pnl:+.2f}` USDT  ({wins}/{total}, WR {wr})")
    return "\n".join(lines)


_ACTION_BADGE = {
    "OPEN_LONG": "🟢",
    "OPEN_SHORT": "🔴",
    "CLOSE": "✋",
    "HOLD": "⚪",
}


def fmt_monitor(symbols: list[str], latest: dict[str, AIDecision]) -> str:
    """Show last AI portfolio decision per universe symbol."""
    if not symbols:
        return "*👀 Monitor*\n_Universe is empty._"

    lines = ["*👀 Monitor AI* `1h`"]
    for sym in symbols:
        d = latest.get(sym)
        if d is None:
            lines.append(f"⚪ `{sym}`  — `no decision yet`")
            continue
        badge = _ACTION_BADGE.get(d.action, "⚪")
        ts = d.created_at.strftime("%m-%d %H:%M") if d.created_at else "?"
        conf = f" conf=`{d.confidence}%`" if d.confidence is not None else ""
        reason = (d.reason or "").strip()
        reason_str = f"\n  _{reason[:120]}_" if reason else ""
        lines.append(f"{badge} *{sym}*  `{d.action}`{conf}  _{ts}_{reason_str}")
    return "\n".join(lines)


def fmt_trade_row(t: Trade) -> str:
    return (
        f"`{t.symbol}` {t.side} pnl=`{t.pnl_usdt:+.2f}` ({t.pnl_pct:+.2f}%) "
        f"reason=`{t.close_reason}` @{t.closed_at:%Y-%m-%d %H:%M}"
    )


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC")
