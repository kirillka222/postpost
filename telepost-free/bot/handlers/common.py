from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from ..config import Config
from ..db import Database
from ..postopts import POST_EMOJI_ID
from ..timeparse import TimeParseError, normalize_tz
from .menu import MENU_POST, MENU_PRO as MENU_PRO_TEXT, MENU_SETTINGS, main_menu, menu_is

log = logging.getLogger(__name__)

router = Router(name="common")
router.message.filter(F.chat.type == "private")

# Премиум-эмодзи в приветствии: id документов эмодзи (иконка бота и галочка списка)
LOGO_EMOJI_ID = "5071082600945158428"
CHECK_EMOJI_ID = "5260341314095947411"


def _emoji(emoji_id: str, fallback: str, rich: bool) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>' if rich else fallback


def help_text(rich: bool = True) -> str:
    """Приветствие. rich=False — с обычными эмодзи вместо премиум (если Telegram не принял премиум-эмодзи)."""
    logo = _emoji(LOGO_EMOJI_ID, "✈️", rich)
    ok = _emoji(CHECK_EMOJI_ID, "✅", rich)
    return (
        f"{logo} <b>FlyPost</b> — быстрый и удобный бот для <b>комфортного</b> ведения ваших каналов.\n\n"
        "<b>FlyPost</b> поможет с задачами:\n\n"
        f"{ok} Публикация сразу и отложенный постинг\n"
        f"{ok} Пост в несколько каналов за один раз\n"
        f"{ok} Нейропостинг: посты из других каналов с ИИ-рерайтом\n"
        f"{ok} Сохраняет форматирование, премиум-эмодзи, фото, видео и альбомы\n"
        f"{ok} Свои подпись и ссылки в конце постов\n\n"
        "Начните с кнопки «Каналы» в меню ниже 👇"
    )


HELP = help_text(rich=False)

def post_help(rich: bool = True) -> str:
    """Подсказка раздела «Создать пост»."""
    mark = _emoji(POST_EMOJI_ID, "📝", rich)
    return (
        f"{mark} <b>Новый пост</b>\n\n"
        "Пришлите пост одним сообщением: текст, фото, видео, файл или альбом. "
        "Форматирование и премиум-эмодзи сохранятся.\n\n"
        "Дальше:\n"
        "<b>1.</b> Выберите каналы\n"
        "<b>2.</b> Опубликуйте сразу или отложите на удобное время"
    )


POST_HELP = post_help(rich=False)


START_DEBOUNCE = 0.8  # секунд ожидания, не пришёл ли ещё один /start
_start_seq: dict[int, int] = {}


@router.message(CommandStart())
@router.message(Command("help"))
@router.message(Command("menu"))
async def cmd_start(message: Message, db: Database, cfg: Config) -> None:
    uid = message.from_user.id
    # Если бот был недоступен и пользователь нажал /start несколько раз, все они придут пачкой.
    # Отвечаем один раз: каждое новое сообщение «перебивает» предыдущие, ответит только последнее.
    _start_seq[uid] = seq = _start_seq.get(uid, 0) + 1
    if START_DEBOUNCE:
        await asyncio.sleep(START_DEBOUNCE)
        if _start_seq.get(uid) != seq:
            return
    await db.ensure_user(uid, cfg.default_tz)
    # от самого красивого к самому простому: Telegram может не принять премиум-иконки кнопок или эмодзи в тексте
    variants = [(True, main_menu(cfg.menu_emoji) if cfg.menu_emoji else None)] if cfg.menu_emoji else []
    variants += [(True, None), (False, cfg.menu_emoji and main_menu(cfg.menu_emoji) or None), (False, None)]
    first_error: Optional[Exception] = None
    for rich, menu in variants:
        try:
            await message.answer(
                help_text(rich), reply_markup=menu if menu is not None else main_menu(), parse_mode="HTML"
            )
            break
        except Exception as exc:  # noqa: BLE001
            first_error = first_error or exc
            log.warning("Приветствие (премиум-эмодзи в тексте: %s, иконки меню: %s) не принято: %s",
                        rich, menu is not None, exc)
    else:
        raise first_error  # type: ignore[misc]
    if first_error is not None and cfg.is_admin(message.from_user.id):  # админу сразу показываем причину
        await message.answer(f"ℹ️ Telegram не принял часть премиум-эмодзи, показал упрощённый вариант.\n"
                             f"Ответ Telegram: {first_error}")


@router.message(menu_is(MENU_POST))
async def menu_post(message: Message, state: FSMContext) -> None:
    await state.clear()
    try:
        await message.answer(post_help(), parse_mode="HTML")
    except Exception:  # noqa: BLE001 — Telegram не принял премиум-эмодзи
        await message.answer(post_help(rich=False), parse_mode="HTML")


@router.message(menu_is(MENU_SETTINGS))
async def menu_settings(message: Message, state: FSMContext, db: Database, cfg: Config) -> None:
    await state.clear()
    user = await db.ensure_user(message.from_user.id, cfg.default_tz)
    plan = await db.get_plan(message.from_user.id)
    await message.answer(
        "⚙️ Настройки\n\n"
        f"🕒 Часовой пояс: {user['tz']}\n"
        "Изменить: /tz Europe/Moscow, /tz Asia/Yekaterinburg или /tz +3. По нему считается время отложенных постов.\n\n"
        f"⭐ Тариф: {plan.name}. Лимиты и оплата — кнопка «{MENU_PRO_TEXT}».\n\n"
        "🤖 Чистка чужих ссылок, своя подпись и премиум-эмодзи настраиваются в разделе «Нейропостинг»."
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ок, отменено. Пришлите пост, когда будете готовы.")


@router.message(Command("tz"))
async def cmd_tz(message: Message, command: CommandObject, db: Database, cfg: Config) -> None:
    user = await db.ensure_user(message.from_user.id, cfg.default_tz)
    if not command.args:
        await message.answer(
            f"Ваш часовой пояс: {user['tz']}\n"
            "Изменить: /tz Europe/Moscow, /tz Asia/Yekaterinburg или /tz +3"
        )
        return
    try:
        tz = normalize_tz(command.args)
    except TimeParseError as exc:
        await message.answer(f"❌ {exc}\nПример: /tz Europe/Moscow или /tz +3")
        return
    await db.set_tz(message.from_user.id, tz)
    await message.answer(f"✅ Часовой пояс: {tz}")