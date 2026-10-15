"""/watermark — водяной знак: текст, положение, размер и прозрачность; знак накладывается на фото и видео постов."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from .. import watermark as wm
from ..db import Database
from .menu import not_menu

log = logging.getLogger(__name__)

router = Router(name="watermark")
router.message.filter(F.chat.type == "private")


class WatermarkInput(StatesGroup):
    text = State()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def view(spec: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Экран настроек: (текст, кнопки). Чистая функция."""
    spec = wm.normalize(spec)
    text = wm.clean_text(spec["text"])
    if text:
        shown = text.replace("\n", " / ")
        body = (
            "💧 Водяной знак\n\n"
            f"Текст: «{shown}»\n"
            f"Положение: {wm.POSITION_NAMES[spec['pos']]} · размер {spec['size']}% · "
            f"непрозрачность {spec['opacity']}%\n\n"
            "Знак накладывается на фото и видео. В «Создать пост» включается кнопкой «💧 Водяной знак» "
            "под черновиком, в нейропостинге — переключателем ниже."
        )
    else:
        body = (
            "💧 Водяной знак\n\n"
            "Напишите текст (например, @мой_канал), и бот будет накладывать его на фото и видео ваших постов. "
            "Знак приклеивается к самой картинке и видео: его нельзя убрать.\n\n"
            "Эмодзи в знаке не поддерживаются, до 2 строк."
        )
    rows: list[list[InlineKeyboardButton]] = [[_btn("✏️ Изменить текст" if text else "✏️ Задать текст", "wm:text")]]
    if text:
        order = ("tl", "tc", "tr", "ml", "c", "mr", "bl", "bc", "br")
        for i in range(0, 9, 3):
            rows.append([_btn(("• " if c == spec["pos"] else "") + wm.POSITIONS[c][0], f"wm:pos:{c}")
                         for c in order[i:i + 3]])
        rows.append([_btn("➖", "wm:size:-"), _btn(f"Размер {spec['size']}%", "wm:noop"), _btn("➕", "wm:size:+")])
        rows.append([_btn("➖", "wm:op:-"), _btn(f"Яркость {spec['opacity']}%", "wm:noop"), _btn("➕", "wm:op:+")])
        rows.append([_btn(("✅" if spec["feeds"] else "❌") + " В нейропостинге: "
                          + ("накладывать" if spec["feeds"] else "не накладывать"), "wm:feeds")])
        rows.append([_btn("🖼 Показать пример", "wm:demo"), _btn("🗑 Удалить знак", "wm:clear")])
    return body, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(target: Message, db: Database, user_id: int, edit: bool = True) -> None:
    text, kb = view(await db.get_watermark(user_id))
    if edit:
        try:
            await target.edit_text(text, reply_markup=kb)
            return
        except Exception as exc:  # noqa: BLE001 — «message is not modified» и прочее: показываем заново
            if "not modified" in str(exc).lower():
                return
            log.warning("Экран водяного знака не обновился (%s), отправляю новым сообщением", exc)
    await target.answer(text, reply_markup=kb)


@router.message(Command("watermark"))
async def cmd_watermark(message: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    await _show(message, db, message.from_user.id, edit=False)


@router.callback_query(F.data == "wm:open")
async def cb_open(cb: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    await _show(cb.message, db, cb.from_user.id, edit=False)
    await cb.answer()


@router.callback_query(F.data == "wm:noop")
async def cb_noop(cb: CallbackQuery) -> None:
    await cb.answer()


@router.callback_query(F.data == "wm:text")
async def cb_text(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(WatermarkInput.text)
    await cb.message.answer(
        "✏️ Пришлите текст водяного знака, например @мой_канал. До 2 строк, без эмодзи.\nОтмена: /cancel")
    await cb.answer()


@router.message(StateFilter(WatermarkInput.text), F.text, not_menu)
async def got_text(message: Message, state: FSMContext, db: Database) -> None:
    if message.text.startswith("/"):
        return
    text = wm.clean_text(message.text)
    if not text:
        await message.answer("❌ Не нашёл в тексте букв или цифр. Эмодзи шрифт не рисует. Пришлите другой текст "
                             "или /cancel.")
        return
    await state.clear()
    await db.set_watermark(message.from_user.id, text=text)
    await _show(message, db, message.from_user.id, edit=False)


@router.callback_query(F.data.startswith("wm:"))
async def cb_change(cb: CallbackQuery, db: Database) -> None:
    parts = cb.data.split(":")
    action, arg = parts[1], (parts[2] if len(parts) > 2 else "")
    uid = cb.from_user.id
    spec = await db.get_watermark(uid)
    if action != "clear" and action != "demo" and not wm.ready(spec):
        await cb.answer("Сначала задайте текст знака", show_alert=True)
        return
    if action == "pos" and arg in wm.POSITIONS:
        await db.set_watermark(uid, pos=arg)
    elif action == "size" and arg in ("+", "-"):
        await db.set_watermark(uid, size=wm.step(spec["size"], wm.STEP if arg == "+" else -wm.STEP, wm.SIZE_RANGE))
    elif action == "op" and arg in ("+", "-"):
        await db.set_watermark(
            uid, opacity=wm.step(spec["opacity"], wm.STEP if arg == "+" else -wm.STEP, wm.OPACITY_RANGE))
    elif action == "feeds":
        await db.set_watermark(uid, feeds=not spec["feeds"])
    elif action == "clear":
        await db.set_watermark(uid, text="", feeds=False)
    elif action == "demo":
        if not wm.ready(spec):
            await cb.answer("Сначала задайте текст знака", show_alert=True)
            return
        await cb.answer()
        try:
            await cb.message.answer_photo(
                BufferedInputFile(wm.sample(spec), filename="example.jpg"), caption="Так будет выглядеть знак")
        except Exception as exc:  # noqa: BLE001
            log.warning("Пример водяного знака не построен: %s", exc)
            await cb.message.answer(f"❌ Не удалось построить пример: {exc}")
        return
    else:
        await cb.answer()
        return
    await _show(cb.message, db, uid)
    await cb.answer()