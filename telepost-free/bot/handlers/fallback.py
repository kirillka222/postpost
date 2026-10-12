"""Последний роутер: фиксирует в логе текст, который не взял ни один обработчик (помогает искать «молчащие» кнопки)."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import Message

log = logging.getLogger(__name__)

router = Router(name="fallback")
router.message.filter(F.chat.type == "private")


@router.message(F.text)
async def unhandled_text(message: Message) -> None:
    log.warning("Необработанное сообщение от %s: %r", message.from_user.id, message.text)