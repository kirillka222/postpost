"""/copy — админское копирование: бот берёт новые посты из канала-источника и публикует их в ваши каналы.

Отличия от обычного нейропостинга: доступно только администраторам бота; текст копируется как есть (без ИИ,
подписи, ссылки на источник и чистки ссылок); если в посте больше одного фото/видео, последнее отбрасывается
(пост на 3 картинки выходит с первыми двумя). Расписание, пауза и водяной знак общие с нейропостингом.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..config import Config
from ..db import Database
from ..feeds import Reader, SourceError, feed_key, is_private, normalize_username, source_label
from . import feedscreens as screens
from .feedscreens import btn, kb, nav_row, paginate, short
from .menu import not_menu

log = logging.getLogger(__name__)

router = Router(name="copyfeeds")
router.message.filter(F.chat.type == "private")

NO_READER = "Нужен аккаунт-читатель (TG_API_ID / TG_API_HASH и вход python -m bot.login): см. README."
DENIED = "🔒 Копирование доступно только администраторам бота."


class CopyNew(StatesGroup):
    username = State()
    targets = State()


def is_admin(event: Any, cfg: Config) -> bool:
    return cfg.is_admin(event.from_user.id)


router.message.filter(is_admin)
router.callback_query.filter(is_admin)


# ---------- экраны (чистые функции) ----------
def main_view(feeds: list[dict], titles: dict[int, str]) -> tuple[str, Any]:
    text = (
        "📋 Копирование (для админов)\n\n"
        "Бот берёт новые посты из канала-источника и публикует их в ваши каналы: текст как есть, без ИИ и "
        "подписи. Если в посте несколько фото/видео, последнее отбрасывается: из 3 картинок выйдут первые две.\n\n"
        f"📥 Источников: {len(feeds)}   📤 Каналов: {len(titles)}"
    )
    if not titles:
        text += "\n\nСначала подключите свой канал: раздел «Каналы»."
    rows = [[btn(("" if f["active"] else "⏸ ") + f"{short(source_label(f['username'], f['title']), 28)} · "
                 f"{len(f['targets'])} 📤", f"cp:s:{f['id']}")] for f in feeds]
    rows.append([btn("➕ Добавить источник", "cp:new")])
    return text, kb(rows)


def card_view(feed: dict, titles: dict[int, str]) -> tuple[str, Any]:
    names = [titles.get(t, str(t)) for t in feed["targets"]]
    where = f"{len(names)} каналов" if len(names) > 3 else (", ".join(names) or "—")
    text = (f"📥 {source_label(feed['username'], feed['title'])}\n→ {where}\n\n"
            f"Копирование без ИИ · {'работает' if feed['active'] else 'на паузе'}")
    if not feed["targets"]:
        text += "\n\n⚠️ Каналы не выбраны: посты не публикуются."
    fid = feed["id"]
    rows = [
        [btn(f"📤 Каналы: {len(feed['targets'])} из {len(titles)}", f"cp:tg:{fid}:0")],
        [btn("⏸ Пауза" if feed["active"] else "▶️ Включить", f"cp:act:{fid}"), btn("🗑 Удалить", f"cp:del:{fid}")],
        [btn("◀️ К списку", "cp:home")],
    ]
    return text, kb(rows)


def targets_view(feed: dict, channels: list[dict], page: int = 0) -> tuple[str, Any]:
    chunk, page, pages = paginate(channels, page)
    chosen = set(feed["targets"])
    fid = feed["id"]
    rows = [[btn(("✅ " if c["chat_id"] in chosen else "❌ ") + short(c["title"]),
                 f"cp:tt:{fid}:{c['chat_id']}:{page}")] for c in chunk]
    nav = nav_row(f"cp:tg:{fid}", page, pages)
    if nav:
        rows.append(nav)
    everyone = bool(channels) and all(c["chat_id"] in chosen for c in channels)
    rows.append([btn("❌ Снять все каналы" if everyone else "✅ Выбрать все каналы", f"cp:ta:{fid}:{page}")])
    rows.append([btn("◀️ Готово", f"cp:s:{fid}")])
    return f"📤 Куда публиковать {source_label(feed['username'], feed['title'])}\nОтметьте каналы.", kb(rows)


def new_targets_kb(channels: list[dict], chosen: list[int]) -> Any:
    rows = [[btn(("✅ " if c["chat_id"] in chosen else "❌ ") + short(c["title"]), f"cp:nt:{c['chat_id']}")]
            for c in channels]
    if len(channels) > 1:
        everyone = all(c["chat_id"] in chosen for c in channels)
        rows.append([btn("❌ Снять все каналы" if everyone else "✅ Выбрать все каналы", "cp:na")])
    rows.append([btn("✅ Готово", "cp:nd"), btn("✖️ Отмена", "cp:nc")])
    return kb(rows)


# ---------- вспомогательное ----------
async def _edit(msg: Message, text: str, markup: Any) -> None:
    try:
        await msg.edit_text(text, reply_markup=markup)
    except Exception as exc:  # noqa: BLE001
        if "not modified" in str(exc).lower():
            return
        log.warning("Экран копирования не обновился (%s), отправляю новым сообщением", exc)
        await msg.answer(text, reply_markup=markup)


async def _titles(db: Database, uid: int) -> dict[int, str]:
    return {c["chat_id"]: c["title"] for c in await db.list_channels(uid)}


async def _home(msg: Message, db: Database, uid: int, edit: bool) -> None:
    text, markup = main_view(await db.list_feeds(uid, "copy"), await _titles(db, uid))
    if edit:
        await _edit(msg, text, markup)
    else:
        await msg.answer(text, reply_markup=markup)


async def _card(msg: Message, db: Database, uid: int, fid: int) -> bool:
    feed = await db.get_feed(uid, fid, "copy")
    if feed is None:
        return False
    text, markup = card_view(feed, await _titles(db, uid))
    await _edit(msg, text, markup)
    return True


# ---------- обработчики ----------
@router.message(Command("copy"))
async def cmd_copy(message: Message, db: Database, cfg: Config, state: Optional[FSMContext] = None) -> None:
    if state is not None:
        await state.clear()
    await db.ensure_user(message.from_user.id, cfg.default_tz)
    await _home(message, db, message.from_user.id, edit=False)


@router.callback_query(F.data == "cp:home")
async def cb_home(cb: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    await _home(cb.message, db, cb.from_user.id, edit=True)
    await cb.answer()


@router.callback_query(F.data.startswith("cp:s:"))
async def cb_card(cb: CallbackQuery, db: Database) -> None:
    if not await _card(cb.message, db, cb.from_user.id, int(cb.data.split(":")[2])):
        await cb.answer("Источник не найден", show_alert=True)
        return
    await cb.answer()


@router.callback_query(F.data.startswith("cp:act:"))
async def cb_toggle(cb: CallbackQuery, db: Database) -> None:
    uid, fid = cb.from_user.id, int(cb.data.split(":")[2])
    if await db.get_feed(uid, fid, "copy") is None or await db.toggle_feed(uid, fid, "active") is None:
        await cb.answer("Источник не найден", show_alert=True)
        return
    await _card(cb.message, db, uid, fid)
    await cb.answer()


@router.callback_query(F.data.startswith("cp:del:"))
async def cb_delete_ask(cb: CallbackQuery, db: Database) -> None:
    feed = await db.get_feed(cb.from_user.id, int(cb.data.split(":")[2]), "copy")
    if feed is None:
        await cb.answer("Источник не найден", show_alert=True)
        return
    await _edit(cb.message, f"Удалить источник {source_label(feed['username'], feed['title'])}?",
                kb([[btn("🗑 Да, удалить", f"cp:delok:{feed['id']}"), btn("✖️ Отмена", f"cp:s:{feed['id']}")]]))
    await cb.answer()


@router.callback_query(F.data.startswith("cp:delok:"))
async def cb_delete(cb: CallbackQuery, db: Database) -> None:
    uid, fid = cb.from_user.id, int(cb.data.split(":")[2])
    if await db.get_feed(uid, fid, "copy") is not None:
        await db.delete_feed(uid, fid)
    await _home(cb.message, db, uid, edit=True)
    await cb.answer()


@router.callback_query(F.data.startswith("cp:tg:"))
async def cb_targets(cb: CallbackQuery, db: Database) -> None:
    uid = cb.from_user.id
    _, _, raw, page = cb.data.split(":")
    feed = await db.get_feed(uid, int(raw), "copy")
    if feed is None:
        await cb.answer("Источник не найден", show_alert=True)
        return
    text, markup = targets_view(feed, await db.list_channels(uid), int(page))
    await _edit(cb.message, text, markup)
    await cb.answer()


@router.callback_query(F.data.regexp(r"^cp:t[ta]:"))
async def cb_target_change(cb: CallbackQuery, db: Database) -> None:
    uid, parts = cb.from_user.id, cb.data.split(":")
    fid = int(parts[2])
    feed = await db.get_feed(uid, fid, "copy")
    if feed is None:
        await cb.answer("Источник не найден", show_alert=True)
        return
    channels = await db.list_channels(uid)
    if parts[1] == "tt":
        await db.toggle_feed_target(uid, fid, int(parts[3]))
        page = int(parts[4])
    else:
        everyone = bool(channels) and all(c["chat_id"] in feed["targets"] for c in channels)
        await db.set_all_feed_targets(uid, fid, enable=not everyone)
        page = int(parts[3])
    feed = await db.get_feed(uid, fid, "copy")
    text, markup = targets_view(feed, channels, page)  # type: ignore[arg-type]
    await _edit(cb.message, text, markup)
    await cb.answer()


# ---------- добавление источника ----------
@router.callback_query(F.data == "cp:new")
async def new_start(cb: CallbackQuery, state: FSMContext, db: Database, reader: Optional[Reader] = None) -> None:
    if reader is None:
        await cb.answer(NO_READER, show_alert=True)
        return
    if not await db.list_channels(cb.from_user.id):
        await cb.answer("Сначала подключите свой канал, куда публиковать: /channels", show_alert=True)
        return
    await state.set_state(CopyNew.username)
    await cb.message.answer("Пришлите канал-источник: @username, ссылку t.me/username или инвайт-ссылку t.me/+… "
                            "закрытого канала. /cancel — отмена.")
    await cb.answer()


@router.message(StateFilter(CopyNew.username), F.text, not_menu)
async def got_username(message: Message, state: FSMContext, db: Database, reader: Optional[Reader] = None) -> None:
    if message.text.startswith("/"):
        return
    uid = message.from_user.id
    if reader is None:
        await state.clear()
        await message.answer(NO_READER)
        return
    try:
        username = normalize_username(message.text)
        info = await reader.resolve(username)
    except SourceError as exc:
        await message.answer(f"❌ {exc}\nПришлите другой канал или /cancel.")
        return
    except Exception as exc:  # noqa: BLE001
        log.warning("resolve %s: %s", message.text, exc)
        await message.answer("❌ Не получилось проверить канал, попробуйте позже или /cancel.")
        return
    key = feed_key(username)
    existing = [f for kind in ("feed", "copy") for f in await db.list_feeds(uid, kind)]
    if any(f["username"] == key for f in existing):
        await message.answer("Этот источник у вас уже добавлен (в нейропостинге или копировании). "
                             "Пришлите другой или /cancel.")
        return
    channels = await db.list_channels(uid)
    chosen = [channels[0]["chat_id"]] if len(channels) == 1 else []
    await state.set_state(CopyNew.targets)
    await state.set_data({"username": username, "title": info.title, "targets": chosen})
    await message.answer(
        f"Источник: «{info.title}» ({'закрытый канал' if is_private(username) else source_label(username, info.title)})."
        "\nКуда публиковать его новые посты? Отметьте каналы и нажмите «Готово».",
        reply_markup=new_targets_kb(channels, chosen))


@router.callback_query(StateFilter(CopyNew.targets), F.data.startswith("cp:n"))
async def new_targets(cb: CallbackQuery, state: FSMContext, db: Database, reader: Optional[Reader] = None) -> None:
    uid, parts = cb.from_user.id, cb.data.split(":")
    action = parts[1]
    data = await state.get_data()
    channels = await db.list_channels(uid)
    ids = [c["chat_id"] for c in channels]
    if action == "nc":
        await state.clear()
        await _edit(cb.message, "Добавление источника отменено.", None)
        await cb.answer()
        return
    if action == "nt" and int(parts[2]) in ids:
        chat = int(parts[2])
        data["targets"] = [t for t in data["targets"] if t != chat] if chat in data["targets"] \
            else [*data["targets"], chat]
    elif action == "na":
        data["targets"] = [] if all(i in data["targets"] for i in ids) else ids
    elif action == "nd":
        if not data["targets"]:
            await cb.answer("Отметьте хотя бы один канал", show_alert=True)
            return
        if reader is None:
            await state.clear()
            await _edit(cb.message, NO_READER, None)
            await cb.answer()
            return
        try:
            last_id = await reader.latest_id(data["username"])  # старые посты не копируем
            fid = await db.add_feed(uid, data["username"], data["title"], last_id, data["targets"],
                                    rewrite=False, credit=False, kind="copy")
        except (SourceError, ValueError) as exc:
            await cb.answer(str(exc), show_alert=True)
            return
        await state.clear()
        await _card(cb.message, db, uid, fid)
        await cb.answer()
        return
    await state.set_data(data)
    try:
        await cb.message.edit_reply_markup(reply_markup=new_targets_kb(channels, data["targets"]))
    except Exception as exc:  # noqa: BLE001
        if "not modified" not in str(exc).lower():
            log.warning("Кнопки не обновились: %s", exc)
    await cb.answer()


__all__ = ["router", "screens"]