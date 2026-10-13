"""/feeds — нейропостинг: список каналов-источников, из которых бот сам перепощивает в ваши каналы."""
from __future__ import annotations

import logging
import time
from dataclasses import asdict
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import llm, schedule
from ..config import Config
from ..db import Database
from ..entities import from_bot, strip_text
from ..feeds import Reader, SourceError, feed_key, is_private, normalize_username, source_label
from ..publisher import _bot_entities
from . import feedscreens as screens
from .albums import album_already_handled
from .menu import FEEDS_DENIED, MENU_FEEDS, feeds_denied, menu_is, not_menu

log = logging.getLogger(__name__)

router = Router(name="feeds")
router.message.filter(F.chat.type == "private")


@router.message(menu_is(MENU_FEEDS), feeds_denied)
@router.message(Command("feeds"), feeds_denied)
async def feeds_closed_msg(message: Message) -> None:
    await message.answer(FEEDS_DENIED)


@router.callback_query(F.data.regexp(r"^(fe|fd|fp|fn|fs|fv):"), feeds_denied)
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
    feeds: list[dict], titles: dict[int, str], prefs: Optional[dict] = None, sched: Optional[dict] = None,
    tz: str = "UTC", now: Optional[int] = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Главный экран нейропостинга (prefs оставлен для совместимости: оформление теперь на своём экране)."""
    return screens.main_view(feeds, titles, sched, tz, now)


async def _edit(msg: Message, text: str, kb_: InlineKeyboardMarkup) -> None:
    """Меняет экран на месте; если Telegram не дал отредактировать, присылает новым сообщением.

    Так кнопки не «молчат» при ошибке, а причина попадает в лог.
    """
    try:
        await msg.edit_text(text, reply_markup=kb_)
    except Exception as exc:  # noqa: BLE001
        if "not modified" in str(exc).lower():
            return
        log.warning("Не удалось отредактировать экран нейропостинга (%s), отправляю новым сообщением", exc)
        await msg.answer(text, reply_markup=kb_)


async def _edit_markup(msg: Message, kb_: InlineKeyboardMarkup) -> None:
    try:
        await msg.edit_reply_markup(reply_markup=kb_)
    except Exception as exc:  # noqa: BLE001
        if "not modified" not in str(exc).lower():
            log.warning("Не удалось обновить кнопки: %s", exc)


async def _show(target: Message, db: Database, uid: int, edit: bool, reader: Optional[Reader] = None) -> None:
    feeds = await db.list_feeds(uid)
    titles = {c["chat_id"]: c["title"] for c in await db.list_channels(uid)}
    user = await db.get_user(uid)
    text, kb_ = feeds_view(feeds, titles, None, await db.get_feed_schedule(uid), (user or {}).get("tz") or "UTC")
    if edit:
        await _edit(target, text, kb_)
    else:
        await target.answer(text, reply_markup=kb_)


# ---------- расписание ----------
class SchedInput(StatesGroup):
    value = State()


SCHED_PROMPTS = {
    "every": "Пришлите интервал между постами: число минут (например 45) или часы (2 ч). От 5 минут до 24 часов.",
    "times": "Пришлите время публикации через запятую в вашем часовом поясе.\nПример: 09:00, 13:30, 18:00",
    "quiet": "Пришлите промежуток, когда посты публиковать не нужно.\nПример: 23:00-08:00",
}


def schedule_view(sched: dict, tz: str, now: Optional[int] = None) -> tuple[str, InlineKeyboardMarkup]:
    now_ts = int(time.time()) if now is None else now
    mode_line, quiet_line, pause_line = schedule.describe(sched, now_ts, tz)
    text = (
        "⏰ Расписание\n\n"
        f"{mode_line}\n{quiet_line}\n{pause_line}\n\n"
        f"Один пост за раз на каждый источник. Во время паузы посты пропускаются.\nПояс: {tz} (/tz)"
    )
    mode = sched["mode"]

    def mark(on: bool, label: str) -> str:
        return ("✅ " if on else "") + label

    rows = [
        [InlineKeyboardButton(text=mark(mode == "off", "⚡ Сразу"), callback_data="fs:mode:off")],
        [InlineKeyboardButton(
            text=mark(mode == "interval" and sched["every"] == m, schedule.fmt_every(m)),
            callback_data=f"fs:every:{m}") for m in schedule.INTERVALS],
        [InlineKeyboardButton(text="✏️ Свой интервал", callback_data="fs:ask:every"),
         InlineKeyboardButton(text=mark(mode == "times", "🕒 В заданное время"), callback_data="fs:mode:times")],
    ]
    if mode == "times":
        rows.append([InlineKeyboardButton(text="✏️ Изменить время публикации", callback_data="fs:ask:times")])
    quiet_row = [InlineKeyboardButton(text="🌙 Тихое время", callback_data="fs:ask:quiet")]
    if sched["quiet_from"]:
        quiet_row.append(InlineKeyboardButton(text="❌ Убрать", callback_data="fs:qclear"))
    rows.append(quiet_row)
    if int(sched["paused_until"]) > now_ts:
        rows.append([InlineKeyboardButton(text="▶️ Возобновить нейропостинг", callback_data="fs:resume")])
    else:
        rows.append([InlineKeyboardButton(text=f"⏸ {label}", callback_data=f"fs:pause:{h}")
                     for h, label in schedule.PAUSES]
                    + [InlineKeyboardButton(text="⏸ До включения", callback_data="fs:pause:0")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="fs:back")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


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
async def _show_settings(target: Message, db: Database, uid: int, edit: bool) -> None:
    text, kb_ = screens.settings_view(await db.get_feed_prefs(uid))
    if edit:
        await _edit(target, text, kb_)
    else:
        await target.answer(text, reply_markup=kb_)


@router.callback_query(F.data == "fp:strip")
async def toggle_strip(cb: CallbackQuery, db: Database, reader: Optional[Reader] = None) -> None:
    await db.toggle_strip_links(cb.from_user.id)
    await _show_settings(cb.message, db, cb.from_user.id, edit=True)
    await cb.answer()


@router.callback_query(F.data == "fp:clear")
async def clear_footer(cb: CallbackQuery, db: Database, reader: Optional[Reader] = None) -> None:
    await db.set_footer(cb.from_user.id, "", [])
    await _show_settings(cb.message, db, cb.from_user.id, edit=True)
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
    await _show_settings(message, db, message.from_user.id, edit=False)


# ---------- добавление ----------
def _targets_kb(channels: list[dict], data: dict) -> InlineKeyboardMarkup:
    rows = []
    for c in channels:
        mark = "✅" if c["chat_id"] in data["targets"] else "❌"
        rows.append([InlineKeyboardButton(text=f"{mark} {_short(c['title'])}", callback_data=f"fn:tg:{c['chat_id']}")])
    if len(channels) > 1:
        everyone = all(c["chat_id"] in data["targets"] for c in channels)
        rows.append([InlineKeyboardButton(text="❌ Снять все каналы" if everyone else "✅ Выбрать все каналы",
                                          callback_data="fn:all")])
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
    elif action == "all":  # повторное нажатие снимает выбор
        ids = [c["chat_id"] for c in channels]
        data["targets"] = [] if all(i in data["targets"] for i in ids) else ids
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
            fid = await db.add_feed(uid, data["username"], data["title"], last_id, data["targets"],
                                    rewrite=data["ai"], style=data["style"], credit=data["credit"])
        except (SourceError, ValueError) as exc:
            await cb.answer(str(exc), show_alert=True)
            return
        await state.clear()
        await _show_source(cb.message, db, uid, fid, edit=True)
        await cb.answer()
        return
    await state.set_data(data)
    await _edit_markup(cb.message, _targets_kb(channels, data))
    await cb.answer()


# ---------- расписание: экраны и ввод ----------
async def _show_schedule(target: Message, db: Database, uid: int, edit: bool) -> None:
    user = await db.get_user(uid)
    text, kb_ = schedule_view(await db.get_feed_schedule(uid), (user or {}).get("tz") or "UTC")
    if edit:
        await _edit(target, text, kb_)
    else:
        await target.answer(text, reply_markup=kb_)


@router.callback_query(F.data.startswith("fs:"))
async def schedule_action(
    cb: CallbackQuery, state: FSMContext, db: Database, reader: Optional[Reader] = None
) -> None:
    parts = cb.data.split(":")
    action, uid = parts[1], cb.from_user.id
    if action == "back":
        await state.clear()
        await _show(cb.message, db, uid, edit=True, reader=reader)
        await cb.answer()
        return
    if action == "ask" and parts[2] in SCHED_PROMPTS:
        await state.set_state(SchedInput.value)
        await state.set_data({"kind": parts[2]})
        await cb.message.answer(SCHED_PROMPTS[parts[2]] + "\n/cancel — отмена.")
        await cb.answer()
        return
    if action == "mode" and parts[2] == "off":
        await db.set_feed_schedule(uid, mode="off")
    elif action == "mode" and parts[2] == "times":
        if (await db.get_feed_schedule(uid))["times"]:
            await db.set_feed_schedule(uid, mode="times")
        else:
            await state.set_state(SchedInput.value)
            await state.set_data({"kind": "times"})
            await cb.message.answer(SCHED_PROMPTS["times"] + "\n/cancel — отмена.")
            await cb.answer()
            return
    elif action == "every" and parts[2].isdigit():
        await db.set_feed_schedule(uid, mode="interval", every=int(parts[2]))
    elif action == "qclear":
        await db.set_feed_schedule(uid, quiet_from="", quiet_to="")
    elif action == "pause" and parts[2].isdigit():
        hours = int(parts[2])
        until = schedule.FOREVER if hours == 0 else int(time.time()) + hours * 3600
        await db.set_feed_schedule(uid, paused_until=until)
    elif action == "resume":
        await db.set_feed_schedule(uid, paused_until=0)
    await _show_schedule(cb.message, db, uid, edit=True)
    await cb.answer()


@router.message(StateFilter(SchedInput.value), not_menu)
async def schedule_input(message: Message, state: FSMContext, db: Database) -> None:
    kind = (await state.get_data()).get("kind")
    text = (message.text or "").strip()
    uid = message.from_user.id
    try:
        if kind == "every":
            await db.set_feed_schedule(uid, mode="interval", every=schedule.parse_every(text))
        elif kind == "times":
            await db.set_feed_schedule(uid, mode="times", times=schedule.parse_times(text))
        elif kind == "quiet":
            a, b = schedule.parse_window(text)
            await db.set_feed_schedule(uid, quiet_from=a, quiet_to=b)
        else:
            await state.clear()
            return
    except schedule.ScheduleError as exc:
        await message.answer(f"{exc}\nПопробуйте ещё раз или /cancel.")
        return
    await state.clear()
    await _show_schedule(message, db, uid, edit=False)


# ---------- экраны: источники, каналы, карточки ----------
async def _feed_of(db: Database, uid: int, fid: int) -> Optional[dict]:
    return next((f for f in await db.list_feeds(uid) if f["id"] == fid), None)


async def _show_source(msg: Message, db: Database, uid: int, fid: int, edit: bool = True) -> None:
    feed = await _feed_of(db, uid, fid)
    channels = await db.list_channels(uid)
    if feed is None:
        text, kb_ = screens.sources_view(await db.list_feeds(uid))
    else:
        text, kb_ = screens.source_view(feed, {c["chat_id"]: c["title"] for c in channels}, len(channels))
    await (_edit(msg, text, kb_) if edit else msg.answer(text, reply_markup=kb_))


async def _show_targets(msg: Message, db: Database, uid: int, fid: int, page: int, note: str = "") -> None:
    feed = await _feed_of(db, uid, fid)
    if feed is None:
        await _edit(msg, *screens.sources_view(await db.list_feeds(uid)))
        return
    await _edit(msg, *screens.targets_view(feed, await db.list_channels(uid), page, note))


async def _show_channel(msg: Message, db: Database, uid: int, chat: int, page: int, note: str = "") -> None:
    channel = await db.get_channel(uid, chat)
    feeds = await db.list_feeds(uid)
    if channel is None:
        await _edit(msg, *screens.channels_view(await db.list_channels(uid), feeds))
        return
    await _edit(msg, *screens.channel_view(channel, feeds, page, note))


@router.callback_query(F.data.startswith("fv:"))
async def view_action(
    cb: CallbackQuery, state: FSMContext, db: Database, reader: Optional[Reader] = None
) -> None:
    parts = cb.data.split(":")
    act, uid, msg = parts[1], cb.from_user.id, cb.message
    if act == "noop":
        await cb.answer()
        return
    nums = [int(x) for x in parts[2:] if x.lstrip("-").isdigit()]
    await state.clear()  # переход по экранам отменяет незаконченный ввод (подпись, время)
    n = lambda i, default=0: nums[i] if len(nums) > i else default  # noqa: E731

    if act == "home":
        await _show(msg, db, uid, edit=True, reader=reader)
    elif act == "set":
        await _show_settings(msg, db, uid, edit=True)
    elif act == "ls":
        await _edit(msg, *screens.sources_view(await db.list_feeds(uid), n(0)))
    elif act == "lc":
        await _edit(msg, *screens.channels_view(await db.list_channels(uid), await db.list_feeds(uid), n(0)))
    elif act == "s":
        await _show_source(msg, db, uid, n(0))
    elif act in ("ai", "cr", "act"):
        field = {"ai": "rewrite", "cr": "credit", "act": "active"}[act]
        await db.toggle_feed(uid, n(0), field)
        await _show_source(msg, db, uid, n(0))
    elif act == "st":
        style = parts[3] if len(parts) > 3 else ""
        if style in llm.STYLES:
            await db.set_feed_style(uid, n(0), style)
        await _show_source(msg, db, uid, n(0))
    elif act == "del":
        feed = await _feed_of(db, uid, n(0))
        if feed is None:
            await _show_source(msg, db, uid, n(0))
        else:
            await _edit(msg, *screens.delete_view(feed))
    elif act == "delok":
        await db.delete_feed(uid, n(0))
        await _edit(msg, *screens.sources_view(await db.list_feeds(uid)))
    elif act == "tg":
        await _show_targets(msg, db, uid, n(0), n(1))
    elif act == "tt":
        await db.toggle_feed_target(uid, n(0), n(1))
        await _show_targets(msg, db, uid, n(0), n(2))
    elif act == "ta":  # «все каналы» работает переключателем: выбрать все / снять все
        feed = await _feed_of(db, uid, n(0))
        channels = await db.list_channels(uid)
        if feed is not None:
            everyone = bool(channels) and all(c["chat_id"] in feed["targets"] for c in channels)
            await db.set_all_feed_targets(uid, n(0), enable=not everyone)
        await _show_targets(msg, db, uid, n(0), n(1))
    elif act == "c":
        await _show_channel(msg, db, uid, n(0), n(1))
    elif act == "ct":
        await db.toggle_feed_target(uid, n(1), n(0))
        await _show_channel(msg, db, uid, n(0), n(2))
    elif act in ("ca", "cn"):
        await db.set_channel_feeds(uid, n(0), enable=(act == "ca"))
        await _show_channel(msg, db, uid, n(0), n(1))
    await cb.answer()