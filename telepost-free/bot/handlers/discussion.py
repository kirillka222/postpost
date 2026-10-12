"""Группы обсуждения каналов: ловим автопересылку поста, чтобы закрыть к нему комментарии (если так выбрано)."""
from __future__ import annotations

from typing import Any

from aiogram import Bot, F, Router
from aiogram.types import Message

from .. import comments

router = Router(name="discussion")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))


@router.message(F.is_automatic_forward)
async def automatic_forward(message: Message, bot: Bot) -> None:
    origin: Any = getattr(message, "forward_origin", None)
    if getattr(origin, "type", None) != "channel":
        return
    await comments.on_automatic_forward(bot, message.chat.id, message.message_id, origin.chat.id, origin.message_id)