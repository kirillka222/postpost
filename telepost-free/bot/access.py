"""Права доступа: кому открыт нейропостинг и кто забанен."""
from __future__ import annotations

from typing import Any, Optional

from .config import Config


def feeds_open_to_everyone(cfg: Config) -> bool:
    """Ни ADMIN_IDS, ни FEEDS_ADMINS не заданы — ограничений нет (как у только что установленного бота)."""
    return not cfg.admins and not cfg.feeds_admins


async def feeds_allowed(db: Any, cfg: Config, user_id: int, user: Optional[dict] = None) -> bool:
    """Нейропостинг доступен: всем (если ограничения не включены), админам, id из FEEDS_ADMINS и тем, кому выдали доступ."""
    if feeds_open_to_everyone(cfg) or cfg.is_admin(user_id) or user_id in cfg.feeds_admins:
        return True
    user = user if user is not None else await db.get_user(user_id)
    return bool(user and user.get("feeds_access"))