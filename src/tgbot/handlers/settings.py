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
from src.scheduler.runner import reschedule_exit_monitor
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
    minus_conf, plus_conf = _adj("ai_min_confidence", "5")

    autotrade_label = "🟢 Autotrade: ON" if s.autotrade_enabled else "🔴 Autotrade: OFF"
    # Mode is read-only: switching live↔testnet needs different API keys, set
    # via .env, requires restart. Showing as a noop button prevents the user
    # from thinking a tap actually changes the endpoint.
    mode_label = f"🌐 Mode: {s.mode.upper()} (.env)"

    return InlineKeyboardMarkup(
        [
            [_b(autotrade_label, "set:toggle:autotrade")],
            [_noop(mode_label)],
            [_noop("⚡ Max Leverage"), minus_lev, _noop(f"{s.max_leverage_cap}x"), plus_lev],
            [
                _noop("💵 Max Equity/Trade"),
                minus_eq,
                _noop(f"{s.max_equity_per_trade_pct:.0f}%"),
                plus_eq,
            ],
            [_noop("⏱ Exit Poll"), minus_poll, _noop(f"{s.exit_poll_minutes}m"), plus_poll],
            [
                _noop("🎯 AI Min Conf"),
                minus_conf,
                _noop(f"{s.ai_min_confidence}%"),
                plus_conf,
            ],
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
        f"• AI Min Confidence: `{s.ai_min_confidence}%`\n"
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
            # mode toggle removed: testnet/live needs different API keys (.env)

        elif action == "adj":
            field, raw_delta = arg1, arg2
            try:
                delta = Decimal(raw_delta)
            except InvalidOperation:
                return

            # Atomic clamp+increment via SQL UPDATE — two fast taps can't
            # race to lose a delta the way a read-modify-write would.
            if field == "max_leverage_cap":
                await repo.adjust_setting(
                    s, "max_leverage_cap", int(delta), min_value=1, max_value=20
                )
            elif field == "exit_poll_minutes":
                updated = await repo.adjust_setting(
                    s, "exit_poll_minutes", int(delta), min_value=5, max_value=60
                )
                reschedule_exit_monitor(updated.exit_poll_minutes)
            elif field == "max_equity_per_trade_pct":
                await repo.adjust_setting(
                    s,
                    "max_equity_per_trade_pct",
                    delta,
                    min_value=Decimal("1"),
                    max_value=Decimal("100"),
                )
            elif field == "ai_min_confidence":
                await repo.adjust_setting(
                    s, "ai_min_confidence", int(delta), min_value=0, max_value=100
                )

        cfg = await repo.get_settings(s)

    await _render_main(query, cfg)
