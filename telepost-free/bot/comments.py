"""Выключение комментариев к посту.

У канала комментарии живут в связанной группе обсуждения: каждый пост автоматически пересылается туда, и под ним
появляется кнопка «Комментарии». Если удалить эту автопересылку в группе, комментарии к посту закрываются.
Бот (админ группы с правом удаления сообщений) делает это сразу после публикации.

Автопересылка приходит боту отдельным событием и может прийти раньше, чем мы узнаем id опубликованного поста,
поэтому обе стороны запоминаются: «пришла пересылка» (seen) и «нужно закрыть» (pending).
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any, Optional

log = logging.getLogger(__name__)

TTL = 300  # секунд: столько ждём вторую сторону
NO_RIGHTS_NOTE = (
    "комментарии не закрыты: добавьте бота в группу обсуждения канала администратором с правом удаления сообщений"
)


class _Memo:
    def __init__(self, limit: int = 1000) -> None:
        self.limit = limit
        self.items: OrderedDict[tuple[int, int], tuple[float, Any]] = OrderedDict()

    def put(self, key: tuple[int, int], value: Any = True) -> None:
        self.items[key] = (time.monotonic(), value)
        while len(self.items) > self.limit:
            self.items.popitem(last=False)

    def pop(self, key: tuple[int, int]) -> Any:
        item = self.items.pop(key, None)
        if item is not None and time.monotonic() - item[0] <= TTL:
            return item[1]
        return None


seen = _Memo()     # (канал, пост) -> (группа обсуждения, сообщение с автопересылкой)
pending = _Memo()  # (канал, пост) -> нужно закрыть комментарии


async def _delete(bot: Any, group_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id=group_id, message_id=message_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось удалить автопересылку %s в группе %s: %s", message_id, group_id, exc)


async def close_comments(bot: Any, chat_id: int, post_ids: list[int]) -> Optional[str]:
    """Закрывает комментарии к постам канала. Возвращает пояснение для пользователя, если сделать это нельзя."""
    try:
        linked = getattr(await bot.get_chat(chat_id), "linked_chat_id", None)
    except Exception as exc:  # noqa: BLE001
        return f"не удалось проверить комментарии канала ({exc})"
    if not linked:
        return None  # группы обсуждения нет, комментариев и так нет
    try:
        me = await bot.get_chat_member(linked, (await bot.me()).id)
    except Exception:  # noqa: BLE001
        return NO_RIGHTS_NOTE
    if me.status == "creator":
        allowed = True
    else:
        allowed = me.status == "administrator" and bool(getattr(me, "can_delete_messages", False))
    if not allowed:
        return NO_RIGHTS_NOTE
    for post_id in post_ids:
        key = (chat_id, post_id)
        hit = seen.pop(key)
        if hit is not None:
            await _delete(bot, *hit)
        else:
            pending.put(key)
    return None


async def on_automatic_forward(bot: Any, group_id: int, message_id: int, channel_id: int, post_id: int) -> None:
    """Автопересылка поста появилась в группе обсуждения: удаляем её, если комментарии к посту нужно закрыть."""
    key = (channel_id, post_id)
    if pending.pop(key):
        await _delete(bot, group_id, message_id)
    else:
        seen.put(key, (group_id, message_id))