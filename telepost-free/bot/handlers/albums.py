"""Альбом приходит несколькими отдельными сообщениями, и Telegram обрабатывает их одновременно.

Без защиты обработчик, который ждёт одно сообщение, отвечал бы на каждое сообщение альбома
(например, десять раз «Канал подключён»). Здесь запоминаем уже обработанные альбомы.
"""
from __future__ import annotations

from typing import Any

_seen: set[str] = set()
_MAX = 2000


def album_already_handled(message: Any) -> bool:
    """True, если это не первое сообщение альбома, который мы уже обрабатываем: такое надо молча пропустить."""
    gid = getattr(message, "media_group_id", None)
    if not gid:
        return False
    key = f"{message.from_user.id}:{gid}"
    if key in _seen:
        return True
    if len(_seen) >= _MAX:
        _seen.clear()
    _seen.add(key)
    return False