"""Общие помощники ИИ-рерайта: квоты, уведомления об ошибках, рерайтер по умолчанию (используются нейропостингом)."""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Optional

from . import llm
from .config import Config
from .db import Database, today_str

log = logging.getLogger(__name__)

Rewriter = Callable[[str, str], Awaitable[str]]

_NOTICE_COOLDOWN = 3600.0
_last_notice: dict[tuple[str, int], float] = {}


def _should_notify(kind: str, key: int, now: float) -> bool:
    last = _last_notice.get((kind, key))
    if last is not None and now - last < _NOTICE_COOLDOWN:
        return False
    _last_notice[(kind, key)] = now
    return True


async def _safe_dm(bot: Any, user_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id=user_id, text=text)
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось написать пользователю %s: %s", user_id, exc)


def default_rewriter(cfg: Config) -> Rewriter:
    async def _rw(text: str, style: str) -> str:
        return await llm.rewrite(
            text, url=cfg.llm_url, model=cfg.llm_model, timeout=cfg.llm_timeout, style=style,
            api_key=cfg.llm_api_key,
        )

    return _rw


async def rewrite_with_quota(
    bot: Any, db: Database, owner: int, text: str, style: str, rewriter: Rewriter
) -> Optional[str]:
    """Переписывает текст за счёт дневной квоты владельца. None — отправлять оригинал.

    Квота возвращается, если модель не ответила; о закончившемся лимите пишем раз в сутки.
    """
    day = today_str()
    plan = await db.get_plan(owner)
    ok, _used = await db.try_consume_rewrite(owner, day, plan.rewrites_per_day)
    if not ok:
        if await db.claim_limit_notice(owner, day):
            await _safe_dm(
                bot, owner,
                "⚠️ Дневной лимит нейро-рерайта исчерпан. Посты продолжают публиковаться "
                "без изменений. Лимит можно расширить: /pro",
            )
        return None
    try:
        return await rewriter(text, style)
    except Exception as exc:  # noqa: BLE001
        log.warning("Рерайт не удался (%s), публикую без изменений", exc)
        await db.refund_rewrite(owner, day)
        if _should_notify("rewrite", owner, time.time()):
            await _safe_dm(
                bot, owner,
                f"⚠️ ИИ-рерайт не удался, пост опубликован без изменений. Причина: {exc or type(exc).__name__}\n"
                "Лимит не потрачен. Если так повторяется, проверьте, что модель запущена и отвечает.",
            )
        return None