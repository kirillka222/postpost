from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import postopts
from ..config import Config
from ..db import Database
from ..manual import build_payload
from ..publisher import publish_post
from ..timeparse import TimeParseError, format_local, parse_when
from .common import _emoji
from .menu import MENU_QUEUE, menu_is, not_menu

log = logging.getLogger(__name__)

router = Router(name="posting")
router.message.filter(F.chat.type == "private")


class Draft(StatesGroup):
    menu = State()
    waiting_time = State()
    buttons = State()


# Альбом приходит отдельными сообщениями: первое собирает остальные в течение ALBUM_WAIT секунд
ALBUM_WAIT = 1.2
_album_buf: dict[str, list[Message]] = {}


def _not_command(message: Message) -> bool:
    """Не команда и не кнопка главного меню: только такое сообщение считается постом или вводом времени."""
    return not (message.text and message.text.startswith("/")) and not_menu(message)


def _short(title: str, n: int = 28) -> str:
    return title if len(title) <= n else title[: n - 1] + "…"


def draft_kb(channels: list[dict], selected: list[int], options: Optional[dict] = None) -> InlineKeyboardMarkup:
    opts = postopts.normalize(options)
    rows: list[list[InlineKeyboardButton]] = []
    for ch in channels:
        mark = "✅" if ch["chat_id"] in selected else "▫️"
        rows.append(
            [InlineKeyboardButton(text=f"{mark} {_short(ch['title'])}", callback_data=f"dch:{ch['chat_id']}")]
        )
    if len(channels) > 1:
        everything = len(selected) >= len(channels)
        rows.append([InlineKeyboardButton(
            text="▫️ Снять все каналы" if everything else "☑️ Выбрать все каналы", callback_data="dch:all")])
    rows.append([
        InlineKeyboardButton(text="🔕 Без звука" if opts["silent"] else "🔔 Звук", callback_data="dop:silent"),
        InlineKeyboardButton(text="📌 ✅ Закрепить" if opts["pin"] else "📌 Закрепить", callback_data="dop:pin"),
        InlineKeyboardButton(text="🛡 ✅ Защита" if opts["protect"] else "🛡 Защита", callback_data="dop:protect"),
    ])
    rows.append([InlineKeyboardButton(
        text="🚫 Без комментариев" if opts["nocomments"] else "💬 Комментарии", callback_data="dop:nocomments")])
    n = postopts.count_buttons(opts["buttons"])
    rows.append([InlineKeyboardButton(text=f"🔗 Кнопки ({n})" if n else "🔗 Добавить кнопки",
                                      callback_data="dbt:edit")])
    if n:
        rows.append([InlineKeyboardButton(text="🗑 Убрать кнопки", callback_data="dbt:clear")])
    count = f" ({len(selected)})" if selected else ""
    rows.append(
        [
            InlineKeyboardButton(text=f"🚀 Сейчас{count}", callback_data="dgo:now"),
            InlineKeyboardButton(text=f"⏰ Отложить{count}", callback_data="dgo:later"),
        ]
    )
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="dgo:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


PARAMS_HELP = (
    "🔔 Звук — пост приходит с уведомлением, 🔕 — тихо\n"
    "📌 Закрепить — закрепит пост в канале\n"
    "🛡 Защита — пост нельзя пересылать и копировать\n"
    "💬 Комментарии — можно закрыть под этим постом"
)


def draft_text(data: dict, total: Optional[int] = None, rich: bool = True) -> str:
    """Текст черновика (HTML). rich=False — с обычным эмодзи вместо премиум."""
    mark = _emoji(postopts.POST_EMOJI_ID, "📝", rich)
    lines = [
        f"{mark} <b>Черновик поста</b>",
        "",
        "Отметьте каналы, при желании добавьте кнопки и параметры — и публикуйте.",
    ]
    info = []
    if data.get("kind") == "album":
        info.append(f"📸 Альбом из {1 + len(data.get('extra', []))} файлов опубликуется целиком")
    if total and total > 1:
        info.append(f"📣 Каналы: {len(data.get('selected', []))} из {total}")
    n = postopts.count_buttons(postopts.normalize(data.get("options"))["buttons"])
    if n:
        info.append(f"🔗 Кнопок под постом: {n}")
    if info:
        lines += ["", *info]
    lines += ["", f"<blockquote>{PARAMS_HELP}</blockquote>"]
    return "\n".join(lines)


async def _send_draft(send: Any, data: dict, channels: list[dict], **kw: Any) -> None:
    """Показывает черновик (send — answer/reply/edit_text). Если Telegram не принял премиум-эмодзи — обычный."""
    kb = draft_kb(channels, data["selected"], data.get("options"))
    try:
        await send(draft_text(data, len(channels)), reply_markup=kb, parse_mode="HTML", **kw)
    except Exception:  # noqa: BLE001 — например, у владельца бота нет Premium
        await send(draft_text(data, len(channels), rich=False), reply_markup=kb, parse_mode="HTML", **kw)


async def _render(target: Message, data: dict, channels: list[dict]) -> None:
    await _send_draft(target.edit_text, data, channels)


@router.message(StateFilter(None, Draft.menu), _not_command)
async def new_draft(
    message: Message, state: FSMContext, db: Database, cfg: Config, reader: Optional[Any] = None
) -> None:
    album: list[int] = []
    album_msgs: list[Message] = []
    if message.media_group_id:
        key = f"{message.from_user.id}:{message.media_group_id}"
        if key in _album_buf:  # не первое сообщение альбома — его заберёт первое
            _album_buf[key].append(message)
            return
        _album_buf[key] = [message]
        await asyncio.sleep(ALBUM_WAIT)
        album_msgs = sorted(_album_buf.pop(key, [message]), key=lambda m: m.message_id)
        album = [m.message_id for m in album_msgs]

    user_id = message.from_user.id
    await db.ensure_user(user_id, cfg.default_tz)
    channels = await db.list_channels(user_id)
    if not channels:
        await message.answer("Сначала подключите канал: /channels")
        return

    is_text = message.text is not None
    data = {
        "src_chat": message.chat.id,
        "src_msg": album[0] if album else message.message_id,
        "extra": album[1:],
        "kind": "album" if len(album) > 1 else ("text" if is_text else "media"),
        # у альбома текст не переписываем: он копируется целиком вместе с подписями
        "text": "" if len(album) > 1 else ((message.text if is_text else message.caption) or ""),
        "override": None,
        "options": postopts.normalize(None),
        "selected": [channels[0]["chat_id"]] if len(channels) == 1 else [],
    }
    # Пост с премиум-эмодзи публикует аккаунт (Premium, админ канала): запоминаем текст, оформление и файлы
    if reader is not None and getattr(reader, "premium", False):
        data["payload"] = build_payload(album_msgs or [message])
        log.info("Черновик пользователя %s: премиум-эмодзи %s, публикация через аккаунт %s", user_id,
                 "найдены" if data["payload"] else "не найдены (или тип файла не поддержан)",
                 "будет" if data["payload"] else "не нужна")
    else:
        log.info("Черновик пользователя %s: Premium-аккаунт недоступен, посты публикует бот без премиум-эмодзи",
                 user_id)
    await state.set_state(Draft.menu)
    await state.set_data(data)
    await _send_draft(message.reply, data, channels)


@router.callback_query(StateFilter(Draft.menu), F.data == "dch:all")
async def toggle_all(cb: CallbackQuery, state: FSMContext, db: Database) -> None:
    """Выбрать все каналы разом или снять выбор, если выбраны все."""
    channels = await db.list_channels(cb.from_user.id)
    data = await state.get_data()
    ids = [c["chat_id"] for c in channels]
    data["selected"] = [] if set(ids) <= set(data["selected"]) else ids
    await state.set_data(data)
    await _render(cb.message, data, channels)
    await cb.answer()


@router.callback_query(StateFilter(Draft.menu), F.data.startswith("dch:"))
async def toggle_channel(cb: CallbackQuery, state: FSMContext, db: Database) -> None:
    chat_id = int(cb.data.split(":", 1)[1])
    channels = await db.list_channels(cb.from_user.id)
    if chat_id not in {c["chat_id"] for c in channels}:
        await cb.answer("Канал не найден", show_alert=True)
        return
    data = await state.get_data()
    selected: list[int] = data["selected"]
    if chat_id in selected:
        selected.remove(chat_id)
    else:
        selected.append(chat_id)
    data["selected"] = selected
    await state.set_data(data)
    await _render(cb.message, data, channels)
    await cb.answer()


@router.callback_query(StateFilter(Draft.menu), F.data.startswith("dop:"))
async def toggle_option(cb: CallbackQuery, state: FSMContext, db: Database) -> None:
    """Переключатели поста: звук, закрепление, защита от копирования."""
    flag = cb.data.split(":", 1)[1]
    data = await state.get_data()
    if flag not in postopts.FLAGS or not data:
        await cb.answer()
        return
    opts = postopts.normalize(data.get("options"))
    opts[flag] = not opts[flag]
    data["options"] = opts
    await state.set_data(data)
    await _render(cb.message, data, await db.list_channels(cb.from_user.id))
    await cb.answer()


BUTTONS_HELP = (
    "🔗 <b>Кнопки под постом</b>\n\n"
    "Пришлите кнопки списком, по одной на строку: текст, тире, ссылка.\n"
    "<blockquote>Кнопка 1 - https://link.com\nКнопка 2 - https://link.com</blockquote>\n"
    "Несколько кнопок в один ряд — через « | » (до 8 в ряду, до 15 рядов):\n"
    "<blockquote>Кнопка 1 - https://link.com | Кнопка 2 - https://link.com</blockquote>\n"
    "Цвет — перед текстом кнопки: <code>!r</code> красная, <code>!g</code> зелёная, <code>!b</code> синяя:\n"
    "<blockquote>!r Кнопка - https://link.com | !g Кнопка - https://link.com</blockquote>\n"
    "Новый список заменит прежний."
)


@router.callback_query(StateFilter(Draft.menu), F.data == "dbt:edit")
async def buttons_edit(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Draft.buttons)
    await cb.message.answer(
        BUTTONS_HELP, parse_mode="HTML", link_preview_options={"is_disabled": True},  # без превью link.com
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="← Назад к черновику", callback_data="dbt:back")]]),
    )
    await cb.answer()


@router.callback_query(StateFilter(Draft.menu), F.data == "dbt:clear")
async def buttons_clear(cb: CallbackQuery, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    opts = postopts.normalize(data.get("options"))
    opts["buttons"] = []
    data["options"] = opts
    await state.set_data(data)
    await _render(cb.message, data, await db.list_channels(cb.from_user.id))
    await cb.answer()


@router.callback_query(StateFilter(Draft.buttons), F.data == "dbt:back")
async def buttons_back(cb: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.set_state(Draft.menu)
    await _send_draft(cb.message.answer, await state.get_data(), await db.list_channels(cb.from_user.id))
    await cb.answer()


@router.message(StateFilter(Draft.buttons), F.text, _not_command)
async def got_buttons(message: Message, state: FSMContext, db: Database) -> None:
    try:
        rows = postopts.parse_buttons(message.text)
    except postopts.ButtonsError as exc:
        await message.answer(f"❌ {exc}\nИсправьте и пришлите список ещё раз.")
        return
    data = await state.get_data()
    opts = postopts.normalize(data.get("options"))
    opts["buttons"] = rows
    data["options"] = opts
    await state.set_state(Draft.menu)
    await state.set_data(data)
    await _send_draft(message.answer, data, await db.list_channels(message.from_user.id))


async def _publish_now(
    bot: Bot, db: Database, user_id: int, data: dict, reader: Optional[Any] = None
) -> tuple[list[str], list[str], list[str]]:
    ok: list[str] = []
    errors: list[str] = []
    notes: list[str] = []
    for chat_id in data["selected"]:
        channel = await db.get_channel(user_id, chat_id)
        if channel is None:
            continue
        try:
            await publish_post(
                bot, chat_id, data["src_chat"], data["src_msg"], data["kind"], data.get("override"), data.get("extra"),
                data.get("payload"), reader, notes, postopts.normalize(data.get("options")),
            )
            ok.append(channel["title"])
        except Exception as exc:  # noqa: BLE001
            log.warning("Публикация в %s не удалась: %s", chat_id, exc)
            errors.append(f"{channel['title']}: {exc}")
        await asyncio.sleep(0.05)
    return ok, errors, list(dict.fromkeys(notes))


@router.callback_query(StateFilter(Draft.menu), F.data.startswith("dgo:"))
async def go(
    cb: CallbackQuery, bot: Bot, state: FSMContext, db: Database, cfg: Config, reader: Optional[Any] = None
) -> None:
    action = cb.data.split(":", 1)[1]
    data = await state.get_data()
    user_id = cb.from_user.id

    if action == "cancel":
        await state.clear()
        await cb.message.edit_text("Черновик отменён.")
        await cb.answer()
        return
    if not data.get("selected"):
        await cb.answer("Отметьте хотя бы один канал", show_alert=True)
        return

    if action == "now":
        await cb.answer()
        ok, errors, notes = await _publish_now(bot, db, user_id, data, reader)
        if ok:
            await db.bump_published(user_id, manual=1)
        await state.clear()
        text = ""
        if ok:
            text += "✅ Опубликовано: " + ", ".join(ok)
        if errors:
            text += ("\n\n" if text else "") + "⚠️ Не удалось:\n" + "\n".join(errors)
        if notes:
            text += "\n\nℹ️ " + "\n".join(notes)
        await cb.message.edit_text(text or "Каналы не найдены — подключите их заново: /channels")
        return

    if action == "later":
        plan = await db.get_plan(user_id)
        if await db.count_pending_posts(user_id) >= plan.max_pending_posts:
            await cb.answer(
                f"В плане уже {plan.max_pending_posts} постов (лимит тарифа {plan.name}). /pro",
                show_alert=True,
            )
            return
        user = await db.ensure_user(user_id, cfg.default_tz)
        await state.set_state(Draft.waiting_time)
        await cb.message.answer(
            f"⏰ Когда опубликовать в {len(data['selected'])} кан.? Выберите кнопку или напишите время сами.\n"
            f"Ваш часовой пояс: {user['tz']} (меняется командой /tz).\n"
            "Свой вариант: 18:30 · завтра 09:00 · 12.10 18:30 · через 2 часа",
            reply_markup=time_kb(user["tz"]),
        )
        await cb.answer()
        return
    await cb.answer()


QUICK_TIMES = {
    "1h": ("Через 1 час", "через 1 час"),
    "3h": ("Через 3 часа", "через 3 часа"),
    "t18": ("Сегодня 18:00", "сегодня 18:00"),
    "m9": ("Завтра 09:00", "завтра 09:00"),
    "m12": ("Завтра 12:00", "завтра 12:00"),
    "m18": ("Завтра 18:00", "завтра 18:00"),
}


def time_kb(tz: str, now: Optional[datetime] = None) -> InlineKeyboardMarkup:
    """Быстрый выбор времени. «Сегодня 18:00» показываем, только если это время ещё впереди."""
    now = now or datetime.now(timezone.utc)
    codes = ["1h", "3h"]
    try:
        parse_when(QUICK_TIMES["t18"][1], now, tz)
        codes.append("t18")
    except TimeParseError:
        pass
    codes += ["m9", "m12", "m18"]
    buttons = [InlineKeyboardButton(text=QUICK_TIMES[c][0], callback_data=f"dtm:{c}") for c in codes]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="↩️ Назад к черновику", callback_data="dtm:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _schedule(
    target: Message, state: FSMContext, db: Database, cfg: Config, user_id: int, when_text: str
) -> bool:
    """Планирует пост. True — запланирован, False — ошибка (она уже показана пользователю)."""
    user = await db.ensure_user(user_id, cfg.default_tz)
    try:
        when = parse_when(when_text, datetime.now(timezone.utc), user["tz"])
    except TimeParseError as exc:
        await target.answer(f"❌ {exc}\nВыберите кнопку выше, напишите время ещё раз или /cancel.")
        return False
    data = await state.get_data()
    if not data.get("selected"):
        await state.clear()
        await target.answer("Черновик потерян, пришлите пост заново.")
        return False
    post_id = await db.add_post(
        user_id, data["src_chat"], data["src_msg"], data["kind"],
        data.get("override"), data["selected"], int(when.timestamp()), data.get("extra"),
        data.get("payload"), data.get("options"),
    )
    titles = {c["chat_id"]: c["title"] for c in await db.list_channels(user_id)}
    names = ", ".join(titles.get(c, str(c)) for c in data["selected"])
    await state.clear()
    await target.answer(
        f"⏰ Пост #{post_id} запланирован на {format_local(int(when.timestamp()), user['tz'])}.\n"
        f"📣 Каналы: {names}\n\n"
        "Все запланированные посты и отмена: /queue. Важно: не удаляйте исходное сообщение из этого чата "
        "до публикации."
    )
    return True


@router.callback_query(StateFilter(Draft.waiting_time), F.data.startswith("dtm:"))
async def quick_time(cb: CallbackQuery, state: FSMContext, db: Database, cfg: Config) -> None:
    code = cb.data.split(":", 1)[1]
    if code == "back":
        data = await state.get_data()
        await state.set_state(Draft.menu)
        channels = await db.list_channels(cb.from_user.id)
        await _send_draft(cb.message.answer, data, channels)
        await cb.answer()
        return
    if code not in QUICK_TIMES:
        await cb.answer()
        return
    await cb.answer()
    await _schedule(cb.message, state, db, cfg, cb.from_user.id, QUICK_TIMES[code][1])


@router.message(StateFilter(Draft.waiting_time), F.text, _not_command)
async def got_time(message: Message, state: FSMContext, db: Database, cfg: Config) -> None:
    await _schedule(message, state, db, cfg, message.from_user.id, message.text)


# ---------- план постов ----------
def _queue_view(posts: list[dict], tz: str, titles: dict[int, str]) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    if not posts:
        return "План постов пуст. Отложенные посты появятся здесь.", None
    lines = ["⏰ Запланированные посты:"]
    rows = []
    for p in posts:
        names = ", ".join(titles.get(c, str(c)) for c in p["chat_ids"])
        lines.append(f"#{p['id']} · {format_local(p['run_at'], tz)} → {names}")
        rows.append([InlineKeyboardButton(text=f"🗑 Отменить #{p['id']}", callback_data=f"pc:{p['id']}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _queue_for(db: Database, user_id: int, cfg: Config) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    user = await db.ensure_user(user_id, cfg.default_tz)
    posts = await db.list_pending_posts(user_id)
    titles = {c["chat_id"]: c["title"] for c in await db.list_channels(user_id)}
    return _queue_view(posts, user["tz"], titles)


@router.message(menu_is(MENU_QUEUE))
@router.message(Command("queue"))
async def cmd_queue(message: Message, db: Database, cfg: Config, state: Optional[FSMContext] = None) -> None:
    if state is not None:
        await state.clear()
    text, kb = await _queue_for(db, message.from_user.id, cfg)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("pc:"))
async def cb_cancel_post(cb: CallbackQuery, db: Database, cfg: Config) -> None:
    await db.cancel_post(cb.from_user.id, int(cb.data.split(":", 1)[1]))
    text, kb = await _queue_for(db, cb.from_user.id, cfg)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()