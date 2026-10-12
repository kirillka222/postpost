"""Запоминает, кто и когда пользовался ботом (для админки) и не пускает забаненных."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

BANNED_TEXT = "⛔ Доступ к боту закрыт."


async def activity_middleware(handler: Callable[[Any, dict], Awaitable[Any]], event: Any, data: dict) -> Any:
    chat = getattr(event, "chat", None)
    if chat is not None and getattr(chat, "type", "private") != "private":
        return await handler(event, data)  # группы обсуждения: пользователей не учитываем
    user = getattr(event, "from_user", None)
    db, cfg = data.get("db"), data.get("cfg")
    if user is not None and db is not None and not getattr(user, "is_bot", False):
        row = None
        try:
            row = await db.touch_user(
                user.id, getattr(user, "username", "") or "", getattr(user, "full_name", "") or "",
                cfg.default_tz if cfg is not None else "Europe/Moscow",
            )
        except Exception as exc:  # noqa: BLE001 — статистика не должна ломать работу бота
            log.warning("Не удалось обновить активность пользователя %s: %s", user.id, exc)
        if row and row.get("banned") and not (cfg is not None and cfg.is_admin(user.id)):
            reason = f"\nПричина: {row['ban_reason']}" if row.get("ban_reason") else ""
            try:
                if hasattr(event, "data"):  # нажатие на кнопку
                    await event.answer(BANNED_TEXT, show_alert=True)
                else:
                    await event.answer(BANNED_TEXT + reason)
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось сообщить о бане пользователю %s: %s", user.id, exc)
            return None
    return await handler(event, data)