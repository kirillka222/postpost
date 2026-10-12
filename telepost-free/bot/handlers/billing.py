from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from ..config import Config
from ..db import Database, today_str
from ..plans import FREE, PRO
from ..timeparse import format_local
from .menu import MENU_PRO, menu_is

log = logging.getLogger(__name__)

router = Router(name="billing")


def _limits(plan) -> str:
    return (
        f"• каналов: {plan.max_channels}\n"
        f"• постов в плане: {plan.max_pending_posts}\n"
        f"• ИИ-рерайтов в сутки: {plan.rewrites_per_day}\n"
        f"• источников нейропостинга: {plan.max_feeds}"
    )


@router.message(menu_is(MENU_PRO))
@router.message(Command("pro"), F.chat.type == "private")
@router.message(Command("plan"), F.chat.type == "private")
async def cmd_pro(message: Message, db: Database, cfg: Config, state: Optional[FSMContext] = None) -> None:
    if state is not None:
        await state.clear()
    uid = message.from_user.id
    user = await db.ensure_user(uid, cfg.default_tz)
    now = int(datetime.now(timezone.utc).timestamp())
    plan = await db.get_plan(uid, now)
    used = await db.rewrites_used(uid, today_str())

    text = [f"Ваш тариф: {plan.name}"]
    if plan is PRO:
        text.append(f"Pro активен до {format_local(user['pro_until'], user['tz'])}")
    text.append(
        f"\nСегодня ИИ-рерайтов: {used}/{plan.rewrites_per_day}"
        f"\nКаналов: {await db.count_channels(uid)}/{plan.max_channels}"
        f"\nПостов в плане: {await db.count_pending_posts(uid)}/{plan.max_pending_posts}"
        f"\nИсточников нейропостинга: {await db.count_feeds(uid)}/{plan.max_feeds}"
    )
    text.append(
        "\nБесплатно навсегда: публикация и отложка с премиум-эмодзи и любыми медиа.\n\n"
        f"Free:\n{_limits(FREE)}\n\nPro:\n{_limits(PRO)}\n\n"
        f"Pro: {cfg.pro_price_stars} ⭐ за {cfg.pro_days} дней."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"⭐ Купить Pro — {cfg.pro_price_stars} Stars", callback_data="buy:pro")]]
    )
    await message.answer("\n".join(text), reply_markup=kb)


@router.callback_query(F.data == "buy:pro")
async def cb_buy(cb: CallbackQuery, bot: Bot, cfg: Config) -> None:
    await bot.send_invoice(
        chat_id=cb.from_user.id,
        title=f"Pro на {cfg.pro_days} дней",
        description="Больше каналов, план постов и ИИ-рерайт.",
        payload=f"pro:{cfg.pro_days}",
        currency="XTR",  # Telegram Stars: provider_token не нужен
        prices=[LabeledPrice(label=f"Pro {cfg.pro_days} дн.", amount=cfg.pro_price_stars)],
        provider_token="",
    )
    await cb.answer()


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, cfg: Config) -> None:
    ok = query.invoice_payload.startswith("pro:") and query.total_amount == cfg.pro_price_stars
    await query.answer(ok=ok, error_message=None if ok else "Цена изменилась, откройте /pro заново")


@router.message(F.successful_payment)
async def paid(message: Message, db: Database, cfg: Config) -> None:
    payment = message.successful_payment
    uid = message.from_user.id
    user = await db.ensure_user(uid, cfg.default_tz)
    days = int(payment.invoice_payload.split(":")[1]) if payment.invoice_payload.startswith("pro:") else cfg.pro_days
    until = await db.extend_pro(uid, days)
    log.info("Оплата Pro: user=%s charge=%s amount=%s", uid, payment.telegram_payment_charge_id, payment.total_amount)
    await message.answer(f"🎉 Спасибо! Pro активен до {format_local(until, user['tz'])}.")


@router.message(Command("paysupport"), F.chat.type == "private")
async def cmd_paysupport(message: Message, cfg: Config) -> None:
    contact = cfg.support_contact or "владельцу бота"
    await message.answer(
        f"По вопросам оплаты и возвратов Stars напишите {contact}. "
        "Укажите, пожалуйста, время платежа и ваш ID."
    )