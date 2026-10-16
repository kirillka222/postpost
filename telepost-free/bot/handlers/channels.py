from __future__ import annotations

import html
import re
from typing import Any, Awaitable, Callable, Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..config import Config
from ..db import Database
from ..feeds import SourceError
from .albums import album_already_handled
from .menu import MENU_CHANNELS, menu_is, not_menu

router = Router(name="channels")
router.message.filter(F.chat.type == "private")


class AddChannel(StatesGroup):
    waiting = State()


def _short(title: str, n: int = 28) -> str:
    return title if len(title) <= n else title[: n - 1] + "…"


CHANNELS_EMOJI_ID = "5260268501515377807"  # премиум-эмодзи перед заголовком списка каналов


def channels_text(channels: list[dict], rich: bool = True) -> str:
    """Текст списка каналов в HTML. rich=False — с обычным эмодзи вместо премиум."""
    if not channels:
        return "У вас пока нет подключённых каналов. Нажмите «Добавить канал»."
    icon = f'<tg-emoji emoji-id="{CHANNELS_EMOJI_ID}">📣</tg-emoji>' if rich else "📣"
    lines = [f"{icon} Ваши каналы:"]
    for ch in channels:
        suffix = f" (@{ch['username']})" if ch["username"] else ""
        lines.append(f"• {html.escape(ch['title'])}{html.escape(suffix)}")
    lines.append("\nНажмите на канал, чтобы открыть его карточку.")
    return "\n".join(lines)


def channels_kb(channels: list[dict]) -> InlineKeyboardMarkup:
    """Каналы по два в ряд, без эмодзи в названиях кнопок; «Добавить канал» отдельной строкой."""
    buttons = [InlineKeyboardButton(text=_short(ch["title"], 22), callback_data=f"chv:{ch['chat_id']}")
               for ch in channels]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="chadd")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_channels(send: Callable[..., Awaitable[Any]], channels: list[dict]) -> None:
    """Показывает список (send — message.answer или message.edit_text). Если Telegram не принял премиум-эмодзи — обычный."""
    kb = channels_kb(channels)
    try:
        await send(channels_text(channels), reply_markup=kb, parse_mode="HTML")
    except Exception:  # noqa: BLE001 — например, у владельца бота нет Premium
        await send(channels_text(channels, rich=False), reply_markup=kb, parse_mode="HTML")


async def verify_channel(bot: Bot, chat: Chat, user_id: int) -> Optional[str]:
    """None — всё хорошо, иначе текст ошибки для пользователя."""
    if chat.type != "channel":
        return "Это не канал. Нужен именно канал (не группа и не чат)."
    try:
        me = await bot.get_chat_member(chat.id, (await bot.me()).id)
    except TelegramAPIError:
        return "Бота нет в этом канале. Добавьте его администратором и повторите."
    if me.status != "administrator" or not getattr(me, "can_post_messages", False):
        return "Сделайте бота администратором канала с правом публиковать сообщения."
    try:
        member = await bot.get_chat_member(chat.id, user_id)
    except TelegramAPIError:
        return "Не получилось проверить ваши права в канале."
    if member.status not in ("creator", "administrator"):
        return "Подключать канал может только его администратор."
    return None


_REF = re.compile(r"^(?:https?://)?(?:t\.me/|telegram\.me/)?@?([A-Za-z0-9_]{4,32})/?$")


async def resolve_chat(bot: Bot, message: Message) -> Optional[Chat]:
    origin = getattr(message, "forward_origin", None)
    if origin is not None and getattr(origin, "type", None) == "channel":
        return origin.chat
    text = (message.text or "").strip()
    if not text:
        return None
    try:
        if re.fullmatch(r"-100\d+", text):
            return await bot.get_chat(int(text))
        if m := _REF.match(text):
            return await bot.get_chat("@" + m.group(1))
    except TelegramAPIError:
        return None
    return None


@router.message(menu_is(MENU_CHANNELS))
@router.message(Command("channels"))
async def cmd_channels(message: Message, db: Database, cfg: Config, state: Optional[FSMContext] = None) -> None:
    if state is not None:
        await state.clear()
    await db.ensure_user(message.from_user.id, cfg.default_tz)
    channels = await db.list_channels(message.from_user.id)
    await show_channels(message.answer, channels)


CONNECT_EMOJI_ID = "5312365458383474419"


async def bot_mention(bot: Any) -> str:
    """«@username» бота (берётся у Telegram, а не из кода: имя бота можно менять) или пустая строка."""
    try:
        name = getattr(await bot.me(), "username", "") or ""
    except Exception:  # noqa: BLE001
        return ""
    return f"@{name}" if name else ""


@router.callback_query(F.data == "chadd")
async def cb_add(cb: CallbackQuery, state: FSMContext, bot: Optional[Bot] = None) -> None:
    await state.set_state(AddChannel.waiting)
    me = await bot_mention(bot) if bot is not None else ""
    body = (
        "Подключение канала:\n"
        f"1. Добавьте бота {me + ' ' if me else ''}в канал администратором с правами «Публикация сообщений», «Добавление "
        "администраторов» и «Добавление участников».\n"
        "2. Перешлите сюда любой пост из канала — или пришлите @username канала.\n\n"
        "{mark} Premium-бот будет добавлен автоматически для корректной работы премиум-эмодзи. "
        "Без этих прав они работать не будут.\n\n"
        "/cancel — отмена."
    )
    rich = f'<tg-emoji emoji-id="{CONNECT_EMOJI_ID}">💎</tg-emoji>'
    try:
        await cb.message.answer(body.format(mark=rich), parse_mode="HTML")
    except Exception:  # noqa: BLE001 — например, у владельца бота нет Premium
        await cb.message.answer(body.format(mark="💎"))
    await cb.answer()


@router.message(StateFilter(AddChannel.waiting), not_menu)
async def got_channel(message: Message, bot: Bot, state: FSMContext, db: Database, cfg: Config,
                      reader: Optional[Any] = None) -> None:
    if album_already_handled(message):
        return  # переслали альбом: остальные его сообщения пропускаем молча
    user_id = message.from_user.id
    chat = await resolve_chat(bot, message)
    if chat is None:
        await message.answer(
            "Не получилось определить канал. Перешлите пост из канала или пришлите @username. "
            "Для отмены — /cancel."
        )
        return
    error = await verify_channel(bot, chat, user_id)
    if error:
        await message.answer(f"❌ {error}\nИсправьте и пришлите канал ещё раз (или /cancel).")
        return
    await db.ensure_user(user_id, cfg.default_tz)
    existing = await db.get_channel(user_id, chat.id)
    plan = await db.get_plan(user_id)
    if existing is None and await db.count_channels(user_id) >= plan.max_channels:
        await state.clear()
        await message.answer(
            f"На тарифе {plan.name} можно подключить до {plan.max_channels} каналов. "
            "Расширить лимит: /pro"
        )
        return
    await db.add_channel(user_id, chat.id, chat.title or str(chat.id), chat.username or "")
    await state.clear()
    status, problem = "skip", ""
    if _can_emoji(reader):  # добавляем Premium-аккаунт сами, чтобы премиум-эмодзи работали сразу
        status, problem = await ensure_publisher(bot, reader, chat.id)
    await _channel_connected(message, chat.title or str(chat.id))
    if status in ("no_rights", "error"):  # обычному пользователю — только если что-то не так с правами
        await message.answer(problem)


CHANNEL_OK_EMOJI_ID = "5318863168376709909"


async def _channel_connected(message: Message, title: str) -> None:
    """Короткое уведомление об успешном подключении (с премиум-эмодзи, при отказе Telegram — с обычным)."""
    body = f"Канал «{html.escape(title)}» подключён. Теперь пришлите мне пост, и я опубликую его."
    try:
        await message.answer(f'<tg-emoji emoji-id="{CHANNEL_OK_EMOJI_ID}">✅</tg-emoji> {body}', parse_mode="HTML")
    except Exception:  # noqa: BLE001 — например, у владельца бота нет Premium
        await message.answer(f"✅ {body}", parse_mode="HTML")


async def _owned(cb: CallbackQuery, db: Database, chat_id: int) -> Optional[dict]:
    channel = await db.get_channel(cb.from_user.id, chat_id)
    if channel is None:
        await cb.answer("Канал не найден", show_alert=True)
    return channel


@router.callback_query(F.data.startswith("chv:"))
async def cb_channel_card(cb: CallbackQuery, db: Database, reader: Optional[Any] = None) -> None:
    """Карточка канала. Ничего не удаляется, пока владелец явно не подтвердит."""
    chat_id = int(cb.data.split(":", 1)[1])
    channel = await _owned(cb, db, chat_id)
    if channel is None:
        return
    handle = f" (@{channel['username']})" if channel["username"] else ""
    rows = []
    rows += [
        [InlineKeyboardButton(text="🧹 Чистка от ботов и бурж-аудитории", callback_data=f"au:{chat_id}")],
        [InlineKeyboardButton(text="🗑 Отключить канал", callback_data=f"chdel:{chat_id}")],
        [InlineKeyboardButton(text="◀️ К списку каналов", callback_data="chlist")],
    ]
    await cb.message.edit_text(
        f"📣 {channel['title']}{handle}\n\nКанал подключён: в него можно публиковать посты и использовать "
        "нейропостинг.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await cb.answer()


@router.callback_query(F.data == "chlist")
async def cb_list(cb: CallbackQuery, db: Database) -> None:
    channels = await db.list_channels(cb.from_user.id)
    await show_channels(cb.message.edit_text, channels)
    await cb.answer()


@router.callback_query(F.data.startswith("chdel:"))
async def cb_delete_ask(cb: CallbackQuery, db: Database) -> None:
    chat_id = int(cb.data.split(":", 1)[1])
    channel = await _owned(cb, db, chat_id)
    if channel is None:
        return
    await cb.message.edit_text(
        f"Отключить канал «{channel['title']}» от бота?\n"
        "Связанные источники нейропостинга будут удалены. Сам канал не изменится.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, отключить", callback_data=f"chdelok:{chat_id}")],
            [InlineKeyboardButton(text="↩️ Нет, назад", callback_data=f"chv:{chat_id}")],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("chdelok:"))
async def cb_delete(cb: CallbackQuery, db: Database) -> None:
    chat_id = int(cb.data.split(":", 1)[1])
    await db.remove_channel(cb.from_user.id, chat_id)
    channels = await db.list_channels(cb.from_user.id)
    await show_channels(cb.message.edit_text, channels)
    await cb.answer()


def _can_emoji(reader: Optional[Any]) -> bool:
    return bool(reader is not None and getattr(reader, "premium", False) and getattr(reader, "me_id", 0))


MANUAL_RIGHTS = (
    "⚠️ Telegram не позволил боту выдать права Premium-аккаунту: скорее всего, он уже добавлен в этот канал "
    "администратором вручную, а менять права таких администраторов может только владелец канала.\n\n"
    "Откройте канал → Управление → Администраторы → Premium-аккаунт сервиса и включите ему право "
    "«Публикация сообщений». Если оно уже включено, ничего делать не нужно: премиум-эмодзи работают."
)


RIGHT_NAMES = (
    ("can_post_messages", "Публикация сообщений"), ("can_edit_messages", "Изменение чужих публикаций"),
    ("can_delete_messages", "Удаление чужих публикаций"), ("can_change_info", "Изменение профиля канала"),
    ("can_manage_video_chats", "Управление трансляциями"), ("can_invite_users", "Добавление участников"),
    ("can_promote_members", "Добавление администраторов"), ("can_restrict_members", "Блокировка пользователей"),
    ("can_post_stories", "Публикация историй"), ("can_edit_stories", "Изменение историй"),
    ("can_delete_stories", "Удаление историй"), ("can_manage_direct_messages", "Доступ к сообщениям канала"),
)


def bot_rights_report(me: Any) -> str:
    """Какие права у бота включены и выключены (по данным Telegram) — чтобы понять, чего не хватает."""
    if me is None:
        return ""
    on = [name for attr, name in RIGHT_NAMES if getattr(me, attr, False)]
    off = [name for attr, name in RIGHT_NAMES if not getattr(me, attr, False) and hasattr(me, attr)]
    return ("\n\nПрава бота сейчас.\nВключены: " + (", ".join(on) or "нет")
            + ("\nВыключены: " + ", ".join(off) if off else ""))


def rights_text(missing: list[str], bot_name: str = "") -> str:
    """Что именно включить боту (названия — как в настройках администратора канала в Telegram)."""
    return (
        "⚠️ Premium-аккаунт не добавлен, поэтому премиум-эмодзи в этом канале работать не будут.\n\n"
        f"Включите боту {bot_name + ' ' if bot_name else ''}(канал → Управление → Администраторы → бот) права:\n"
        + "\n".join(f"• «{name}»" for name in missing)
        + "\n\nЗатем отключите канал и подключите его заново."
    )


def _invite_expired(exc: Exception) -> bool:
    """Telegram сообщил, что пригласительная ссылка истекла или отозвана."""
    return type(exc).__name__.startswith("InviteHash") or "has expired" in str(exc).lower()


async def ensure_publisher(bot: Bot, reader: Any, chat_id: int) -> tuple[str, str]:
    """Делает Premium-аккаунт админом канала (только публикация). Возвращает (статус, текст для пользователя).

    Статусы: already — уже подключён, ok — подключили сейчас, no_rights — у бота нет нужных прав, error — сбой.
    """
    try:
        member = await bot.get_chat_member(chat_id, reader.me_id)
    except TelegramAPIError:
        member = None  # аккаунта в канале ещё нет: это обычный случай
    if member is not None and member.status in ("administrator", "creator") \
            and getattr(member, "can_post_messages", True):
        return "already", "✅ Премиум-эмодзи в этом канале уже включены."
    if member is not None and member.status == "administrator":
        # админа, назначенного не ботом, бот редактировать не может (RIGHT_FORBIDDEN): права меняет владелец
        return "error", MANUAL_RIGHTS + "\n\n(Причина: аккаунт уже админ канала, но без права «Публикация сообщений».)"
    try:
        me = await bot.get_chat_member(chat_id, (await bot.me()).id)
    except TelegramAPIError:
        me = None
    missing = [name for name, attr in (("Добавление администраторов", "can_promote_members"),
                                       ("Добавление участников", "can_invite_users"))
               if me is None or not getattr(me, attr, False)]
    if missing:
        return "no_rights", rights_text(missing, await bot_mention(bot))
    link = None
    try:
        is_member = getattr(reader, "is_member", None)
        if not (is_member is not None and await is_member(chat_id)):  # уже в канале: вступать не нужно
            for attempt in (0, 1):
                # вторая попытка — свежая ссылка без лимита: одноразовая могла «сгореть» при прошлой попытке
                link = await bot.create_chat_invite_link(
                    chat_id, name="premium-emoji", member_limit=1 if attempt == 0 else None)
                try:
                    await reader.join_invite(link.invite_link)
                    break
                except Exception as exc:  # noqa: BLE001
                    if attempt == 0 and _invite_expired(exc):
                        try:
                            await bot.revoke_chat_invite_link(chat_id, link.invite_link)
                        except TelegramAPIError:
                            pass
                        link = None
                        continue
                    raise
        # can_restrict_members=False обязательно: для каналов Bot API сам включает «Блокировку пользователей»,
        # если параметр не передан, а у бота этого права может не быть (тогда RIGHT_FORBIDDEN)
        await bot.promote_chat_member(chat_id, reader.me_id, can_post_messages=True, can_restrict_members=False)
    except SourceError as exc:
        return "error", f"❌ Аккаунт не смог вступить в канал: {exc}"
    except TelegramAPIError as exc:
        if "RIGHT_FORBIDDEN" in str(exc):
            return "error", (
                MANUAL_RIGHTS + f"\n\n(Причина: Telegram ответил на назначение админом: {exc})"
                + bot_rights_report(me)
                + "\n\nЕсли Premium-аккаунта в администраторах канала нет, включите боту все права на время "
                  "подключения: Telegram разрешает боту выдавать только те права, которые есть у него самого. "
                  "После подключения лишние права можно снова выключить.")
        return "error", f"❌ Telegram не дал выполнить действие: {exc}"
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "seconds", None) is not None:
            return "error", "⏳ Telegram временно ограничил вступления аккаунта. Повторите позже."
        if _invite_expired(exc):
            return "error", (
                "❌ Telegram сразу счёл пригласительную ссылку недействительной (истекла или отозвана). Повторите "
                "через минуту. Если повторяется: проверьте, что Premium-аккаунт не в списке удалённых/заблокированных "
                "канала, или добавьте его в канал вручную администратором с правом «Публикация сообщений».")
        return "error", f"❌ Не удалось подключить аккаунт: {exc}"
    finally:
        if link is not None:
            try:
                await bot.revoke_chat_invite_link(chat_id, link.invite_link)
            except TelegramAPIError:
                pass
    return "ok", (
        "✅ Premium-аккаунт добавлен в канал администратором только с правом публикации. "
        "Теперь посты выйдут с премиум-эмодзи. Права «Добавление администраторов» и "
        "«Добавление участников» у бота можно отключить."
    )


@router.callback_query(F.data.startswith("chemoji:"))
async def cb_emoji(cb: CallbackQuery, bot: Bot, db: Database, reader: Optional[Any] = None) -> None:
    """Повторная попытка подключить аккаунт-публикатор (из «Нейропостинга»)."""
    chat_id = int(cb.data.split(":", 1)[1])
    await cb.answer()
    if await db.get_channel(cb.from_user.id, chat_id) is None:
        await cb.message.answer("Этот канал не подключён.")
        return
    if reader is None or not _can_emoji(reader):
        await cb.message.answer("Premium-аккаунт сейчас не подключён к боту, функция недоступна.")
        return
    await cb.message.answer((await ensure_publisher(bot, reader, chat_id))[1])