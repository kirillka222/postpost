"""/admin — панель администратора: статистика и карточки пользователей (доступ по ADMIN_IDS в .env)."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..access import feeds_allowed, feeds_open_to_everyone
from ..config import Config
from ..db import Database, today_str
from ..timeparse import format_local, get_zone

PAGE = 8

router = Router(name="admin")
router.message.filter(F.chat.type == "private")


class AdminFind(StatesGroup):
    query = State()


def not_admin(event: Any, cfg: Config) -> bool:
    return not cfg.is_admin(event.from_user.id)


def is_admin(event: Any, cfg: Config) -> bool:
    return cfg.is_admin(event.from_user.id)


def day_start(now: int, tz: str) -> int:
    local = datetime.fromtimestamp(now, tz=timezone.utc).astimezone(get_zone(tz))
    return int(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def ago(seconds: int) -> str:
    if seconds < 60:
        return "только что"
    if seconds < 3600:
        return f"{seconds // 60} мин назад"
    if seconds < 86400:
        return f"{seconds // 3600} ч назад"
    return f"{seconds // 86400} дн назад"


def dashboard_text(s: dict) -> str:
    return (
        "🛠 Админ-панель\n\n"
        f"👥 Пользователей всего: {s['users']}\n"
        f"🆕 Новых сегодня: {s['new_today']}\n"
        f"🟢 Заходили сегодня: {s['active_today']}\n"
        f"📅 Активны за 7 дней: {s['active_7d']}\n"
        f"⭐ С Pro: {s['pro']}\n"
        f"🤖 С доступом к нейропостингу: {s['access']} · 🚫 Забанено: {s['banned']}\n\n"
        f"📣 Подключено каналов: {s['channels']}\n"
        f"📋 Постов в плане: {s['pending']}\n"
        f"📝 Опубликовано вручную/по плану: {s['posts_manual']}\n"
        f"🤖 Опубликовано нейропостингом: {s['posts_feed']}\n"
        f"🔗 Активных источников: {s['feeds']}\n\n"
        "Команды: /grant /revoke @user — доступ к нейропостингу, /ban /unban @user [причина], /banned, /access, /user @user.\n"
        "Время «заходил» считается с момента обновления бота: раньше оно не записывалось."
    )


def home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="ad:list:0"),
         InlineKeyboardButton(text="🔎 Найти", callback_data="ad:find")],
        [InlineKeyboardButton(text="📋 Копирование", callback_data="cp:home")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="ad:home")],
    ])


def _name(u: dict) -> str:
    return f"@{u['username']}" if u.get("username") else (u.get("full_name") or "без имени")


def profile_text(u: dict, ov: dict, now: int, tz: str, pro: bool, feeds_ok: bool = True, open_all: bool = False) -> str:
    seen = f"{format_local(u['last_seen'], tz)} · {ago(now - u['last_seen'])}" if u["last_seen"] else "—"
    status = f"⭐ Pro до {format_local(u['pro_until'], tz)}" if pro else (
        "Free" + (" (Pro закончился)" if u["pro_until"] else ""))
    lines = [
        f"👤 {_name(u)}",
        f"ID: {u['user_id']}" + (f" · {u['full_name']}" if u.get("full_name") and u.get("username") else ""),
        f"Статус: {status}",
        ("🚫 ЗАБАНЕН" + (f": {u['ban_reason']}" if u.get("ban_reason") else "")) if u.get("banned") else "Бан: нет",
        "Нейропостинг: " + ("доступен всем (ограничение не включено)" if open_all else ("доступ открыт" if feeds_ok else "доступ закрыт")),
        f"Регистрация: {format_local(u['created_at'], tz)}",
        f"Последняя активность: {seen}",
        f"Часовой пояс пользователя: {u['tz']}",
        "",
        f"📝 Опубликовано постов: {u['posts_manual']} (по плану и сразу)",
        f"🤖 Опубликовано нейропостингом: {u['posts_feed']}",
        f"📋 В плане сейчас: {ov['pending']} · выполнено по плану: {ov['scheduled_done']}",
        f"✨ ИИ-рерайтов сегодня: {ov['rewrites_today']}",
        "",
        f"📣 Каналы ({len(ov['channels'])}):",
    ]
    lines += [
        f"• {c['title']}" + (f" (@{c['username']})" if c["username"] else "") + f" · {c['chat_id']}"
        for c in ov["channels"]
    ] or ["• нет"]
    lines += ["", f"🔗 Источники нейропостинга ({len(ov['feeds'])}):"]
    lines += [
        f"• {f['title'] or f['username']}" + ("" if f["active"] else " (выключен)") for f in ov["feeds"]
    ] or ["• нет"]
    return "\n".join(lines)


def profile_kb(uid: int, pro: bool, access: bool = False, banned: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="⭐ +30 дней Pro", callback_data=f"adp:{uid}:30")]]
    if pro:
        rows[0].append(InlineKeyboardButton(text="❌ Снять Pro", callback_data=f"adp:{uid}:0"))
    rows.append([
        InlineKeyboardButton(text="🤖 Закрыть нейропостинг" if access else "🤖 Открыть нейропостинг",
                             callback_data=f"adf:{uid}:{0 if access else 1}"),
        InlineKeyboardButton(text="✅ Разбанить" if banned else "🚫 Забанить",
                             callback_data=f"adb:{uid}:{0 if banned else 1}"),
    ])
    rows.append([InlineKeyboardButton(text="◀️ К списку", callback_data="ad:list:0"),
                 InlineKeyboardButton(text="🏠 Панель", callback_data="ad:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _stats_view(db: Database, cfg: Config) -> tuple[str, InlineKeyboardMarkup]:
    now = int(time.time())
    return dashboard_text(await db.admin_stats(now, day_start(now, cfg.default_tz))), home_kb()


async def _profile_view(db: Database, cfg: Config, uid: int) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    user = await db.get_user(uid)
    if user is None:
        return None
    now = int(time.time())
    ov = await db.user_overview(uid, today_str())
    pro = user["pro_until"] > now
    open_all = feeds_open_to_everyone(cfg)
    ok = await feeds_allowed(db, cfg, uid, user)
    text = profile_text(user, ov, now, cfg.default_tz, pro, ok, open_all)
    return text, profile_kb(uid, pro, bool(user.get("feeds_access")), bool(user.get("banned")))


@router.message(Command("admin"), is_admin)
async def cmd_admin(message: Message, db: Database, cfg: Config, state: Optional[FSMContext] = None) -> None:
    if state is not None:
        await state.clear()
    text, kb = await _stats_view(db, cfg)
    await message.answer(text, reply_markup=kb)


@router.message(Command("user"), is_admin)
async def cmd_user(message: Message, command: CommandObject, db: Database, cfg: Config) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer("Использование: /user @username или /user 123456789")
        return
    await _send_profile(message, db, cfg, query)


async def _send_profile(message: Message, db: Database, cfg: Config, query: str) -> bool:
    found = await db.find_user(query)
    view = await _profile_view(db, cfg, found["user_id"]) if found else None
    if view is None:
        await message.answer("Пользователь не найден. Ищу по @username (если он писал боту после обновления) "
                             "или по числовому ID.")
        return False
    await message.answer(view[0], reply_markup=view[1])
    return True


@router.callback_query(F.data == "ad:home", is_admin)
async def cb_home(cb: CallbackQuery, db: Database, cfg: Config, state: Optional[FSMContext] = None) -> None:
    if state is not None:
        await state.clear()
    text, kb = await _stats_view(db, cfg)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("ad:list:"), is_admin)
async def cb_list(cb: CallbackQuery, db: Database, cfg: Config) -> None:
    page = max(0, int(cb.data.split(":")[2]))
    users = await db.recent_users(PAGE + 1, page * PAGE)
    more = len(users) > PAGE
    users = users[:PAGE]
    now = int(time.time())
    rows = []
    for u in users:
        mark = "⭐ " if u["pro_until"] > now else ""
        seen = ago(now - u["last_seen"]) if u["last_seen"] else "давно"
        rows.append([InlineKeyboardButton(
            text=f"{mark}{_name(u)} · {seen}"[:60], callback_data=f"adu:{u['user_id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"ad:list:{page - 1}"))
    if more:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"ad:list:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🏠 Панель", callback_data="ad:home")])
    await cb.message.edit_text(
        "👥 Пользователи (недавно активные сверху). Нажмите, чтобы открыть профиль:" if users
        else "Пользователей пока нет.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("adu:"), is_admin)
async def cb_user(cb: CallbackQuery, db: Database, cfg: Config) -> None:
    view = await _profile_view(db, cfg, int(cb.data.split(":")[1]))
    if view is None:
        await cb.answer("Пользователь не найден", show_alert=True)
        return
    await cb.message.edit_text(view[0], reply_markup=view[1])
    await cb.answer()


@router.callback_query(F.data.startswith("adp:"), is_admin)
async def cb_pro(cb: CallbackQuery, db: Database, cfg: Config) -> None:
    _, raw_uid, raw_days = cb.data.split(":")
    uid, days = int(raw_uid), int(raw_days)
    if await db.get_user(uid) is None:
        await cb.answer("Пользователь не найден", show_alert=True)
        return
    if days > 0:
        await db.extend_pro(uid, days)
        await cb.answer()
    else:
        await db.revoke_pro(uid)
        await cb.answer()
    view = await _profile_view(db, cfg, uid)
    if view is not None:
        await cb.message.edit_text(view[0], reply_markup=view[1])


async def _notify(bot: Bot, uid: int, text: str) -> None:
    try:
        await bot.send_message(chat_id=uid, text=text)
    except Exception:  # noqa: BLE001 — пользователь мог заблокировать бота
        pass


async def set_access(db: Database, bot: Bot, uid: int, allowed: bool) -> None:
    await db.set_feeds_access(uid, allowed)
    await _notify(bot, uid, "🤖 Вам открыт доступ к нейропостингу: раздел «Нейропостинг» в меню." if allowed
                  else "🤖 Доступ к нейропостингу закрыт.")


async def set_ban(db: Database, cfg: Config, uid: int, banned: bool, reason: str = "") -> Optional[str]:
    """Возвращает текст ошибки или None. Админов банить нельзя."""
    if banned and cfg.is_admin(uid):
        return "Нельзя забанить администратора."
    await db.set_banned(uid, banned, reason)
    return None


@router.callback_query(F.data.startswith("adf:"), is_admin)
async def cb_access(cb: CallbackQuery, bot: Bot, db: Database, cfg: Config) -> None:
    _, raw_uid, flag = cb.data.split(":")
    uid = int(raw_uid)
    if await db.get_user(uid) is None:
        await cb.answer("Пользователь не найден", show_alert=True)
        return
    await set_access(db, bot, uid, flag == "1")
    await cb.answer()
    view = await _profile_view(db, cfg, uid)
    if view is not None:
        await cb.message.edit_text(view[0], reply_markup=view[1])


@router.callback_query(F.data.startswith("adb:"), is_admin)
async def cb_ban(cb: CallbackQuery, db: Database, cfg: Config) -> None:
    _, raw_uid, flag = cb.data.split(":")
    uid = int(raw_uid)
    if await db.get_user(uid) is None:
        await cb.answer("Пользователь не найден", show_alert=True)
        return
    err = await set_ban(db, cfg, uid, flag == "1")
    if err:
        await cb.answer(err, show_alert=True)
        return
    await cb.answer()
    view = await _profile_view(db, cfg, uid)
    if view is not None:
        await cb.message.edit_text(view[0], reply_markup=view[1])


async def _target(message: Message, command: CommandObject, db: Database, cfg: Config, usage: str) -> Optional[tuple[int, str]]:
    """Достаёт пользователя из «/команда @username|id [текст]». По числовому id создаёт запись, если её ещё нет."""
    parts = (command.args or "").split(maxsplit=1)
    if not parts:
        await message.answer(f"Использование: {usage}")
        return None
    rest = parts[1] if len(parts) > 1 else ""
    q = parts[0].lstrip("@")
    if q.lstrip("-").isdigit():
        uid = int(q)
        if await db.get_user(uid) is None:
            await db.ensure_user(uid, cfg.default_tz)
        return uid, rest
    found = await db.find_user(q)
    if found is None:
        await message.answer("Пользователь с таким @username не писал боту. Используйте числовой ID.")
        return None
    return found["user_id"], rest


def _label(u: Optional[dict], uid: int = 0) -> str:
    return f"{_name(u)} · {u['user_id']}" if u else str(uid)


@router.message(Command("grant"), is_admin)
async def cmd_grant(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Config) -> None:
    t = await _target(message, command, db, cfg, "/grant @username или /grant 123456789")
    if t:
        await set_access(db, bot, t[0], True)
        await message.answer(f"🤖 Доступ к нейропостингу открыт: {_label(await db.get_user(t[0]), t[0])}")


@router.message(Command("revoke"), is_admin)
async def cmd_revoke(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Config) -> None:
    t = await _target(message, command, db, cfg, "/revoke @username или /revoke 123456789")
    if t:
        await set_access(db, bot, t[0], False)
        await message.answer(f"🤖 Доступ к нейропостингу закрыт: {_label(await db.get_user(t[0]), t[0])}")


@router.message(Command("ban"), is_admin)
async def cmd_ban(message: Message, command: CommandObject, db: Database, cfg: Config) -> None:
    t = await _target(message, command, db, cfg, "/ban @username [причина] или /ban 123456789 [причина]")
    if not t:
        return
    err = await set_ban(db, cfg, t[0], True, t[1])
    await message.answer(err or f"🚫 Забанен: {_label(await db.get_user(t[0]), t[0])}" + (f"\nПричина: {t[1]}" if t[1] else ""))


@router.message(Command("unban"), is_admin)
async def cmd_unban(message: Message, command: CommandObject, db: Database, cfg: Config) -> None:
    t = await _target(message, command, db, cfg, "/unban @username или /unban 123456789")
    if t:
        await set_ban(db, cfg, t[0], False)
        await message.answer(f"✅ Разбанен: {_label(await db.get_user(t[0]), t[0])}")


@router.message(Command("banned"), is_admin)
async def cmd_banned(message: Message, db: Database) -> None:
    users = await db.users_where("banned")
    await message.answer("🚫 Забаненные:\n" + "\n".join(
        f"• {_label(u)}" + (f" — {u['ban_reason']}" if u["ban_reason"] else "") for u in users) if users
        else "Забаненных нет.")


@router.message(Command("access"), is_admin)
async def cmd_access(message: Message, db: Database, cfg: Config) -> None:
    users = await db.users_where("feeds_access")
    head = "🤖 Доступ к нейропостингу открыт:\n"
    if feeds_open_to_everyone(cfg):
        head = "ℹ️ Ограничение не включено (нет ADMIN_IDS и FEEDS_ADMINS): нейропостинг доступен всем.\n" + head
    await message.answer(head + ("\n".join(f"• {_label(u)}" for u in users) if users else "никому не выдавали"))


@router.callback_query(F.data == "ad:find", is_admin)
async def cb_find(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminFind.query)
    await cb.message.answer("Пришлите @username или числовой ID пользователя. /cancel — отмена.")
    await cb.answer()


@router.message(StateFilter(AdminFind.query), F.text, ~F.text.startswith("/"), is_admin)
async def got_query(message: Message, state: FSMContext, db: Database, cfg: Config) -> None:
    if await _send_profile(message, db, cfg, message.text):
        await state.clear()


@router.callback_query(F.data.regexp(r"^(ad:|adu:|adp:|adf:|adb:)"), not_admin)
async def cb_denied(cb: CallbackQuery) -> None:
    await cb.answer("Нет доступа", show_alert=True)