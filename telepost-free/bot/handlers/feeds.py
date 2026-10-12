"""/feeds — нейропостинг: список каналов-источников, из которых бот сам перепощивает в ваши каналы."""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import llm
from ..config import Config
from ..db import Database
from ..entities import from_bot, strip_text
from ..feeds import Reader, SourceError, feed_key, is_private, normalize_username, source_label
from ..publisher import _bot_entities
from .albums import album_already_handled
from .menu import FEEDS_DENIED, MENU_FEEDS, feeds_denied, menu_is, not_menu

log = logging.getLogger(__name__)

router = Router(name="feeds")
router.message.filter(F.chat.type == "private")


@router.message(menu_is(MENU_FEEDS), feeds_denied)
@router.message(Command("feeds"), feeds_denied)
async def feeds_closed_msg(message: Message) -> None:
    await message.answer(FEEDS_DENIED)


@router.callback_query(F.data.regexp(r"^(fe|fd|fp|fn):"), feeds_denied)
async def feeds_closed_cb(cb: CallbackQuery) -> None:
    await cb.answer(FEEDS_DENIED, show_alert=True)

NO_READER = (
    "Нейропостинг пока не настроен владельцем бота: нужен аккаунт-читатель "
    "(TG_API_ID / TG_API_HASH в .env и вход командой python -m bot.login). См. README."
)


FOOTER_MAX = 500


class FeedFooter(StatesGroup):
    waiting = State()


class FeedNew(StatesGroup):
    username = State()
    targets = State()


def _short(title: str, n: int = 24) -> str:
    return title if len(title) <= n else title[: n - 1] + "…"


def feeds_view(
    feeds: list[dict], titles: dict[int, str], prefs: Optional[dict] = None
) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["📡 Нейропостинг: бот следит за каналами-источниками и сам публикует их новые посты в ваши каналы."]
    rows: list[list[InlineKeyboardButton]] = []
    if not feeds:
        lines.append("\nИсточников пока нет. Нажмите «Добавить источник».")
    for f in feeds:
        dst = ", ".join(titles.get(t, str(t)) for t in f["targets"])
        ai = "ИИ: " + llm.STYLE_LABELS[f["style"]].split(" ", 1)[1] if f["rewrite"] else "без ИИ"
        credit = "со ссылкой на источник" if f["credit"] else "без ссылки"
        state = "активно" if f["active"] else "пауза"
        lines.append(f"\n#{f['id']} {source_label(f['username'], f['title'])} → {dst}\n   {ai} · {credit} · {state}")
        rows.append(
            [
                InlineKeyboardButton(text=f"🤖 ИИ #{f['id']}", callback_data=f"fd:ai:{f['id']}"),
                InlineKeyboardButton(text=f"🔗 #{f['id']}", callback_data=f"fd:cr:{f['id']}"),
                InlineKeyboardButton(
                    text=("⏸" if f["active"] else "▶️") + f" #{f['id']}", callback_data=f"fd:act:{f['id']}"
                ),
                InlineKeyboardButton(text=f"🗑 #{f['id']}", callback_data=f"fd:del:{f['id']}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ Добавить источник", callback_data="fn:start")])
    if feeds:
        lines.append("\n🤖 — вкл/выкл ИИ · 🔗 — ссылка на источник · ⏸ — пауза · 🗑 — удалить")
    prefs = prefs or {"strip_links": True, "footer": "", "footer_entities": []}
    lines.append("\n🧹 Чужие ссылки и @упоминания в постах: " + ("убираются" if prefs["strip_links"] else "остаются"))
    lines.append("📌 Своя подпись: " + (f"«{_short(prefs['footer'], 60)}»" if prefs["footer"] else "не задана"))
    rows.append([InlineKeyboardButton(
        text="🧹 Убирать чужие ссылки: " + ("ВКЛ" if prefs["strip_links"] else "выкл"), callback_data="fp:strip")])
    footer_row = [InlineKeyboardButton(text="✏️ Своя подпись", callback_data="fp:footer")]
    if prefs["footer"]:
        footer_row.append(InlineKeyboardButton(text="❌ Убрать подпись", callback_data="fp:clear"))
    rows.append(footer_row)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(target: Message, db: Database, uid: int, edit: bool, reader: Optional[Reader] = None) -> None:
    feeds = await db.list_feeds(uid)
    titles = {c["chat_id"]: c["title"] for c in await db.list_channels(uid)}
    text, kb = feeds_view(feeds, titles, await db.get_feed_prefs(uid))
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.message(menu_is(MENU_FEEDS))
@router.message(Command("feeds"))
async def cmd_feeds(
    message: Message, db: Database, cfg: Config, reader: Optional[Reader] = None, state: Optional[FSMContext] = None
) -> None:
    if state is not None:
        await state.clear()
    await db.ensure_user(message.from_user.id, cfg.default_tz)
    if reader is None:
        await message.answer(NO_READER)
        return
    await _show(message, db, message.from_user.id, edit=False, reader=reader)


@router.callback_query(F.data.startswith("fd:"))
async def manage_feed(cb: CallbackQuery, db: Database, reader: Optional[Reader] = None) -> None:
    _, action, raw = cb.data.split(":")
    fid, uid = int(raw), cb.from_user.id
    if action == "del":
        ok = await db.delete_feed(uid, fid)
    else:
        field = {"ai": "rewrite", "cr": "credit", "act": "active"}.get(action)
        ok = field is not None and (await db.toggle_feed(uid, fid, field)) is not None
    if not ok:
        await cb.answer("Источник не найден", show_alert=True)
        return
    await _show(cb.message, db, uid, edit=True, reader=reader)
    await cb.answer()


# ---------- чистка ссылок и своя подпись ----------
@router.callback_query(F.data == "fp:strip")
async def toggle_strip(cb: CallbackQuery, db: Database, reader: Optional[Reader] = None) -> None:
    await db.toggle_strip_links(cb.from_user.id)
    await _show(cb.message, db, cb.from_user.id, edit=True, reader=reader)
    await cb.answer()


@router.callback_query(F.data == "fp:clear")
async def clear_footer(cb: CallbackQuery, db: Database, reader: Optional[Reader] = None) -> None:
    await db.set_footer(cb.from_user.id, "", [])
    await _show(cb.message, db, cb.from_user.id, edit=True, reader=reader)
    await cb.answer()


@router.callback_query(F.data == "fp:footer")
async def footer_start(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(FeedFooter.waiting)
    await cb.message.answer(
        "Пришлите текст подписи одним сообщением: она добавится в конец каждого поста нейропостинга.\n"
        "Можно с форматированием, гиперссылками (выделите слово → «Ссылка») и премиум-эмодзи. "
        f"До {FOOTER_MAX} символов. /cancel — отмена."
    )
    await cb.answer()


@router.message(StateFilter(FeedFooter.waiting), not_menu)
async def footer_received(
    message: Message, state: FSMContext, db: Database, reader: Optional[Reader] = None
) -> None:
    if album_already_handled(message):
        return
    raw = message.text or message.caption or ""
    ents = from_bot(message.entities or message.caption_entities)
    text, ents = strip_text(raw, ents)
    if not text:
        await message.answer("Нужен текст. Пришлите подпись или /cancel.")
        return
    if len(text) > FOOTER_MAX:
        await message.answer(f"Слишком длинно ({len(text)} символов, максимум {FOOTER_MAX}). Сократите и пришлите снова.")
        return
    await db.set_footer(message.from_user.id, text, [asdict(e) for e in ents])
    await state.clear()
    await message.answer("✅ Подпись сохранена. Так она будет выглядеть в конце постов:")
    await message.answer(text, entities=_bot_entities(ents) or None)
    await _show(message, db, message.from_user.id, edit=False, reader=reader)


# ---------- добавление ----------
def _targets_kb(channels: list[dict], data: dict) -> InlineKeyboardMarkup:
    rows = []
    for c in channels:
        mark = "✅" if c["chat_id"] in data["targets"] else "▫️"
        rows.append([InlineKeyboardButton(text=f"{mark} {_short(c['title'])}", callback_data=f"fn:tg:{c['chat_id']}")])
    rows.append(
        [InlineKeyboardButton(text="🤖 ИИ-рерайт: ВКЛ" if data["ai"] else "🤖 ИИ-рерайт: выкл", callback_data="fn:ai")]
    )
    if data["ai"]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=("• " if data["style"] == s else "") + label.split(" ", 1)[1],
                    callback_data=f"fn:st:{s}",
                )
                for s, label in llm.STYLE_LABELS.items()
            ]
        )
    rows.append(
        [InlineKeyboardButton(
            text="🔗 Ссылка на источник: ВКЛ" if data["credit"] else "🔗 Ссылка на источник: выкл",
            callback_data="fn:cr",
        )]
    )
    rows.append(
        [
            InlineKeyboardButton(text="✅ Готово", callback_data="fn:done"),
            InlineKeyboardButton(text="✖️ Отмена", callback_data="fn:cancel"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "fn:start")
async def new_start(cb: CallbackQuery, state: FSMContext, db: Database, reader: Optional[Reader] = None) -> None:
    uid = cb.from_user.id
    if reader is None:
        await cb.answer(NO_READER, show_alert=True)
        return
    plan = await db.get_plan(uid)
    if await db.count_feeds(uid) >= plan.max_feeds:
        await cb.answer(f"Лимит источников на тарифе {plan.name}: {plan.max_feeds}. /pro", show_alert=True)
        return
    if not await db.list_channels(uid):
        await cb.answer("Сначала подключите свой канал, куда публиковать: /channels", show_alert=True)
        return
    await state.set_state(FeedNew.username)
    await cb.message.answer(
        "Пришлите канал-источник:\n"
        "• публичный — @username или ссылку t.me/username;\n"
        "• закрытый — инвайт-ссылку t.me/+… (аккаунт-читатель вступит в канал по ссылке).\n\n"
        "Копируйте чужие материалы только с разрешения авторов или в рамках закона. /cancel — отмена."
    )
    await cb.answer()


@router.message(StateFilter(FeedNew.username), F.text, not_menu)
async def got_username(
    message: Message, state: FSMContext, db: Database, reader: Optional[Reader] = None
) -> None:
    uid = message.from_user.id
    if message.text.startswith("/"):
        return
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
        log.warning("resolve @%s: %s", message.text, exc)
        await message.answer("❌ Не получилось проверить канал, попробуйте позже или /cancel.")
        return
    if any(f["username"] == feed_key(username) for f in await db.list_feeds(uid)):
        await message.answer("Этот источник у вас уже добавлен. Пришлите другой или /cancel.")
        return
    channels = await db.list_channels(uid)
    data = {
        "username": username, "title": info.title, "targets": [channels[0]["chat_id"]] if len(channels) == 1 else [],
        "ai": True, "style": "normal", "credit": True,
    }
    await state.set_state(FeedNew.targets)
    await state.set_data(data)
    await message.answer(
        f"Источник: «{info.title}» ({source_label(username, info.title) if not is_private(username) else 'закрытый канал'}).\n"
        + ("Закрытый канал: копируйте его посты только с разрешения владельца.\n" if is_private(username) else "")
        + "Куда публиковать его новые посты? Отметьте каналы и нажмите «Готово».",
        reply_markup=_targets_kb(channels, data),
    )


@router.callback_query(StateFilter(FeedNew.targets), F.data.startswith("fn:"))
async def targets_step(cb: CallbackQuery, state: FSMContext, db: Database, reader: Optional[Reader] = None) -> None:
    parts = cb.data.split(":")
    action, uid = parts[1], cb.from_user.id
    data = await state.get_data()
    channels = await db.list_channels(uid)

    if action == "cancel":
        await state.clear()
        await cb.message.edit_text("Добавление источника отменено.")
        await cb.answer()
        return
    if action == "tg":
        chat_id = int(parts[2])
        if chat_id in {c["chat_id"] for c in channels}:
            if chat_id in data["targets"]:
                data["targets"].remove(chat_id)
            else:
                data["targets"].append(chat_id)
    elif action == "ai":
        data["ai"] = not data["ai"]
    elif action == "cr":
        data["credit"] = not data["credit"]
    elif action == "st" and parts[2] in llm.STYLES:
        data["style"] = parts[2]
    elif action == "done":
        if not data["targets"]:
            await cb.answer("Отметьте хотя бы один канал", show_alert=True)
            return
        if reader is None:
            await state.clear()
            await cb.message.edit_text(NO_READER)
            await cb.answer()
            return
        plan = await db.get_plan(uid)
        if await db.count_feeds(uid) >= plan.max_feeds:
            await state.clear()
            await cb.message.edit_text(f"Лимит источников на тарифе {plan.name}: {plan.max_feeds}. /pro")
            await cb.answer()
            return
        try:
            last_id = await reader.latest_id(data["username"])  # старые посты не публикуем
            await db.add_feed(uid, data["username"], data["title"], last_id, data["targets"],
                              rewrite=data["ai"], style=data["style"], credit=data["credit"])
        except (SourceError, ValueError) as exc:
            await cb.answer(str(exc), show_alert=True)
            return
        await state.clear()
        await _show(cb.message, db, uid, edit=True, reader=reader)
        await cb.answer()
        return
    await state.set_data(data)
    await cb.message.edit_reply_markup(reply_markup=_targets_kb(channels, data))
    await cb.answer()