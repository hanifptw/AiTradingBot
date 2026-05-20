"""Settings handler — drastically simplified for the AI-controlled bot.

Only knobs left are the safety caps and operational toggles. The AI handles
all strategy parameters internally; users adjust caps to constrain it.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal, InvalidOperation

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from src.core import repository as repo
from src.core.db import session
from src.core.models import Settings
from src.tgbot.auth import restricted


def _b(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def _noop(label: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data="noop")


def _adj(field: str, step: str) -> tuple[InlineKeyboardButton, InlineKeyboardButton]:
    return _b("➖", f"set:adj:{field}:-{step}"), _b("➕", f"set:adj:{field}:+{step}")


def settings_keyboard(s: Settings) -> InlineKeyboardMarkup:
    minus_lev, plus_lev = _adj("max_leverage_cap", "1")
    minus_eq, plus_eq = _adj("max_equity_per_trade_pct", "1")
    minus_poll, plus_poll = _adj("exit_poll_minutes", "5")

    autotrade_label = "🟢 Autotrade: ON" if s.autotrade_enabled else "🔴 Autotrade: OFF"
    mode_label = f"🌐 Mode: {s.mode.upper()}"

    return InlineKeyboardMarkup(
        [
            [_b(autotrade_label, "set:toggle:autotrade")],
            [_b(mode_label, "set:toggle:mode")],
            [_noop("⚡ Max Leverage"), minus_lev, _noop(f"{s.max_leverage_cap}x"), plus_lev],
            [
                _noop("💵 Max Equity/Trade"),
                minus_eq,
                _noop(f"{s.max_equity_per_trade_pct:.0f}%"),
                plus_eq,
            ],
            [_noop("⏱ Exit Poll"), minus_poll, _noop(f"{s.exit_poll_minutes}m"), plus_poll],
            [_b("← Menu", "menu:main")],
        ]
    )


def _settings_text(s: Settings) -> str:
    return (
        f"*⚙️ Settings*\n"
        f"• Mode: `{s.mode}`\n"
        f"• Autotrade: `{'ON' if s.autotrade_enabled else 'OFF'}`\n"
        f"• Max Leverage Cap: `{s.max_leverage_cap}x`\n"
        f"• Max Equity per Trade: `{s.max_equity_per_trade_pct:.0f}%`\n"
        f"• Exit-monitor Poll: `{s.exit_poll_minutes}` menit\n"
        f"\n_AI (Grok 4.20) memutuskan kapan buka/tutup, ukuran posisi, leverage, SL, dan TP. "
        f"Setting di atas adalah batas atas (safety caps) yang AI tidak boleh lewati._"
    )


async def _render_main(query, s: Settings) -> None:
    with contextlib.suppress(BadRequest):
        await query.edit_message_text(
            _settings_text(s),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=settings_keyboard(s),
        )


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

    async with session() as s:
        cfg = await repo.get_settings(s)

        if action == "show":
            pass

        elif action == "toggle":
            if arg1 == "autotrade":
                await repo.update_setting(s, autotrade_enabled=not cfg.autotrade_enabled)
            elif arg1 == "mode":
                new_mode = "live" if cfg.mode == "testnet" else "testnet"
                await repo.update_setting(s, mode=new_mode)

        elif action == "adj":
            field, raw_delta = arg1, arg2
            try:
                delta = Decimal(raw_delta)
            except InvalidOperation:
                return

            if field == "max_leverage_cap":
                new_val = max(1, min(20, int(cfg.max_leverage_cap) + int(delta)))
                await repo.update_setting(s, max_leverage_cap=new_val)
            elif field == "exit_poll_minutes":
                new_val = max(5, min(60, int(cfg.exit_poll_minutes) + int(delta)))
                await repo.update_setting(s, exit_poll_minutes=new_val)
            elif field == "max_equity_per_trade_pct":
                new_val = max(
                    Decimal("1"), min(Decimal("100"), cfg.max_equity_per_trade_pct + delta)
                )
                await repo.update_setting(s, max_equity_per_trade_pct=new_val)

        cfg = await repo.get_settings(s)

    await _render_main(query, cfg)
