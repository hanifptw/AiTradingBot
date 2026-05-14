"""Settings handler with full inline-keyboard UI.

Views:
  main   — all settings, numeric +/− rows, toggle buttons
  tf     — timeframe picker sub-menu
  stoch  — stochastic K/D/smooth editor sub-menu
"""

from __future__ import annotations

import asyncio
import contextlib
from decimal import Decimal, InvalidOperation

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from src.core import repository as repo
from src.core.db import session
from src.core.models import Settings
from src.execution.trailing import run_trailing_tick
from src.tgbot.auth import restricted

ALLOWED_TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"]
_STOCH_FIELDS = {"stoch_k", "stoch_d", "stoch_smooth"}

# ── helpers ──────────────────────────────────────────────────────────────────

def _b(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def _noop(label: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data="noop")


def _adj(field: str, step: str) -> tuple[InlineKeyboardButton, InlineKeyboardButton]:
    return _b("➖", f"set:adj:{field}:-{step}"), _b("➕", f"set:adj:{field}:+{step}")


# ── keyboard builders ─────────────────────────────────────────────────────────

def settings_keyboard(s: Settings) -> InlineKeyboardMarkup:
    minus_lev, plus_lev = _adj("leverage", "1")
    minus_sl, plus_sl = _adj("sl_pct", "0.5")
    minus_tp, plus_tp = _adj("tp_pct", "0.5")
    minus_amt, plus_amt = _adj("trade_amount", "10")
    minus_tr, plus_tr = _adj("trailing_offset_pct", "0.1")
    minus_mp, plus_mp = _adj("max_positions", "1")

    autotrade_label = "🟢 Autotrade: ON" if s.autotrade_enabled else "🔴 Autotrade: OFF"
    trailing_label = "📈 Trailing: ON" if s.trailing_enabled else "📈 Trailing: OFF"

    return InlineKeyboardMarkup([
        [_b(autotrade_label, "set:toggle:autotrade")],
        [_b(f"⏱ Timeframe: {s.timeframe}", "set:menu:timeframe")],
        [_noop("⚡ Leverage"),    minus_lev, _noop(f"{s.leverage}x"),           plus_lev],
        [_noop("🛡 SL %"),        minus_sl,  _noop(f"{s.sl_pct:.1f}%"),         plus_sl],
        [_noop("🎯 TP %"),        minus_tp,  _noop(f"{s.tp_pct:.1f}%"),         plus_tp],
        [_noop("💵 Amount USDT"), minus_amt, _noop(f"{s.trade_amount:.0f}"),    plus_amt],
        [_b(trailing_label, "set:toggle:trailing"),
         minus_tr, _noop(f"{s.trailing_offset_pct:.1f}%"), plus_tr],
        [_noop("👥 Max pos"),     minus_mp,  _noop(str(s.max_positions)),       plus_mp],
        [_b(f"🔬 Stochastic  K={s.stoch_k}  D={s.stoch_d}  sm={s.stoch_smooth}",
            "set:menu:stoch")],
        [_b("← Menu", "menu:main")],
    ])


def timeframe_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [_b(tf, f"set:tf:{tf}") for tf in ALLOWED_TIMEFRAMES[i:i + 3]]
        for i in range(0, len(ALLOWED_TIMEFRAMES), 3)
    ]
    rows.append([_b("← Kembali", "set:show")])
    return InlineKeyboardMarkup(rows)


def stoch_keyboard(s: Settings) -> InlineKeyboardMarkup:
    mk, pd_, ps = _adj("stoch_k", "1"), _adj("stoch_d", "1"), _adj("stoch_smooth", "1")
    return InlineKeyboardMarkup([
        [mk[0], _noop(f"K = {s.stoch_k}"),      mk[1]],
        [pd_[0], _noop(f"D = {s.stoch_d}"),     pd_[1]],
        [ps[0], _noop(f"Smooth = {s.stoch_smooth}"), ps[1]],
        [_b("← Kembali", "set:show")],
    ])


# ── text builders ─────────────────────────────────────────────────────────────

def _settings_text(s: Settings) -> str:
    return (
        f"*⚙️ Settings*\n"
        f"• Autotrade: `{'ON' if s.autotrade_enabled else 'OFF'}`\n"
        f"• Timeframe: `{s.timeframe}` | Leverage: `{s.leverage}x`\n"
        f"• SL: `{s.sl_pct:.1f}%` | TP: `{s.tp_pct:.1f}%` | Amount: `{s.trade_amount:.0f}` USDT\n"
        f"• Trailing: `{'ON' if s.trailing_enabled else 'OFF'}` "
        f"(offset `{s.trailing_offset_pct:.1f}%`)\n"
        f"• Max positions: `{s.max_positions}`\n"
        f"• Stoch: K=`{s.stoch_k}` D=`{s.stoch_d}` smooth=`{s.stoch_smooth}`"
    )


def _stoch_text(s: Settings) -> str:
    return (
        f"*🔬 Stochastic Parameters*\n\n"
        f"K Period: `{s.stoch_k}`\n"
        f"D Period: `{s.stoch_d}`\n"
        f"Smooth:   `{s.stoch_smooth}`"
    )


# ── view renderers ────────────────────────────────────────────────────────────

async def _render_main(query, s: Settings) -> None:
    with contextlib.suppress(BadRequest):
        await query.edit_message_text(
            _settings_text(s), parse_mode=ParseMode.MARKDOWN,
            reply_markup=settings_keyboard(s),
        )


async def _render_stoch(query, s: Settings) -> None:
    with contextlib.suppress(BadRequest):
        await query.edit_message_text(
            _stoch_text(s), parse_mode=ParseMode.MARKDOWN,
            reply_markup=stoch_keyboard(s),
        )


# ── public handlers ───────────────────────────────────────────────────────────

@restricted
async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with session() as s:
        cfg = await repo.get_settings(s)
    text = _settings_text(cfg)
    markup = settings_keyboard(cfg)
    if update.callback_query:
        with contextlib.suppress(BadRequest):
            await update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup
            )
    else:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup
        )


@restricted
async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    data = query.data or ""

    if data == "noop":
        return

    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    arg1 = parts[2] if len(parts) > 2 else ""
    arg2 = parts[3] if len(parts) > 3 else ""

    # ── sub-menu views (no DB change) ─────────────────────────────────────────
    if action == "menu" and arg1 == "timeframe":
        with contextlib.suppress(BadRequest):
            await query.edit_message_text(
                "⏱ *Pilih Timeframe:*", parse_mode=ParseMode.MARKDOWN,
                reply_markup=timeframe_keyboard(),
            )
        return

    if action == "menu" and arg1 == "stoch":
        async with session() as s:
            cfg = await repo.get_settings(s)
        await _render_stoch(query, cfg)
        return

    # ── DB mutations ──────────────────────────────────────────────────────────
    async with session() as s:
        cfg = await repo.get_settings(s)

        if action == "show":
            pass  # just re-render

        elif action == "toggle":
            if arg1 == "autotrade":
                await repo.update_setting(s, autotrade_enabled=not cfg.autotrade_enabled)
            elif arg1 == "trailing":
                new_trailing = not cfg.trailing_enabled
                await repo.update_setting(s, trailing_enabled=new_trailing)
                if new_trailing:
                    asyncio.create_task(run_trailing_tick())

        elif action == "tf":
            if arg1 in ALLOWED_TIMEFRAMES:
                await repo.update_setting(s, timeframe=arg1)

        elif action == "adj":
            field, raw_delta = arg1, arg2
            try:
                delta = Decimal(raw_delta)
            except InvalidOperation:
                return

            if field in {"leverage", "max_positions", "stoch_k", "stoch_d", "stoch_smooth"}:
                current = int(getattr(cfg, field))
                new_val = max(1, current + int(delta))
                caps = {"leverage": 125, "max_positions": 20}
                new_val = min(caps.get(field, 9999), new_val)
                await repo.update_setting(s, **{field: new_val})

            elif field == "trade_amount":
                current: Decimal = getattr(cfg, field)
                new_val = max(Decimal("10"), current + delta)
                await repo.update_setting(s, **{field: new_val})

            elif field in {"sl_pct", "tp_pct", "equity_pct", "trailing_offset_pct"}:
                current = getattr(cfg, field)
                new_val = max(Decimal("0.1"), min(Decimal("100"), current + delta))
                await repo.update_setting(s, **{field: new_val})

        cfg = await repo.get_settings(s)

    # ── re-render view ────────────────────────────────────────────────────────
    if action == "adj" and arg1 in _STOCH_FIELDS:
        await _render_stoch(query, cfg)
    else:
        await _render_main(query, cfg)
