"""Настройки бота: переменные окружения + необязательный файл .env."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional


def load_dotenv(path: str = ".env") -> None:
    """Минимальный загрузчик .env (без внешних зависимостей)."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Config:
    bot_token: str
    db_path: str = "data/bot.sqlite3"
    llm_url: str = "http://localhost:11434"
    llm_model: str = "qwen3:4b"
    llm_timeout: int = 180
    default_tz: str = "Europe/Moscow"
    pro_price_stars: int = 150
    pro_days: int = 30
    support_contact: str = ""
    # Нейропостинг из чужих каналов (Telethon). Выключен, пока не заданы api_id/api_hash.
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_session: str = "data/userbot"
    feed_poll_seconds: int = 120
    # Как публиковать посты с премиум-эмодзи: auto — через аккаунт-читатель (если он Premium и админ
    # канала), иначе ботом; bot — всегда ботом.
    feed_publish_via: str = "auto"
    # Служебный закрытый канал-«витрина» (id вида -100…): аккаунт-читатель (Premium) публикует туда пост с
    # премиум-эмодзи, а бот копирует его в каналы пользователей. Тогда им не нужно добавлять аккаунт в админы.
    feed_staging_chat: int = 0
    # 1 — не удалять посты из витрины после копирования (для диагностики)
    feed_staging_keep: bool = False
    # copy — бот копирует пост из витрины (без подписи «переслано»); forward — пересылает (сохраняет эмодзи надёжнее,
    # но у поста есть шапка «Переслано из …»)
    feed_staging_mode: str = "copy"
    # Кому доступен нейропостинг (Telegram id через запятую). Пусто — всем.
    feeds_admins: tuple[int, ...] = ()
    # Администраторы бота (ADMIN_IDS): команда /admin. Если не заданы — берутся из FEEDS_ADMINS.
    admin_ids: tuple[int, ...] = ()
    # Премиум-иконки кнопок нижнего меню: {"post": "id", ...} (MENU_EMOJI в .env)
    menu_emoji: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MENU_EMOJI))

    @property
    def admins(self) -> tuple[int, ...]:
        return self.admin_ids or self.feeds_admins

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admins

    def feeds_allowed(self, user_id: int) -> bool:
        return not self.feeds_admins or user_id in self.feeds_admins


# Премиум-иконки кнопок меню (id документов премиум-эмодзи, выбраны владельцем бота)
DEFAULT_MENU_EMOJI: dict[str, str] = {
    "post": "5258073068852485953",      # Создать пост
    "queue": "5257965174979042426",     # План постов
    "channels": "5260268501515377807",  # Каналы
    "feeds": "5258093637450866522",     # Нейропостинг
    "settings": "5258096772776991776",  # Настройки
    "pro": "5258165702707125574",       # Тариф Pro
}


def parse_ids(raw: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.split(r"[,\s;]+", raw.strip()) if x.strip().lstrip("-").isdigit())


def parse_menu_emoji(raw: str) -> dict[str, str]:
    """«post=5368…,queue=5258…» → {"post": "5368…", ...}; всё лишнее пропускается."""
    if raw.strip().lower() in ("off", "none", "0", "false"):
        return {}
    out: dict[str, str] = dict(DEFAULT_MENU_EMOJI)
    for part in re.split(r"[,;\s]+", raw.strip()):
        key, _, val = part.partition("=")
        if key.strip() and val.strip().isdigit():
            out[key.strip().lower()] = val.strip()
    return out


def load_config(env: Optional[Mapping[str, str]] = None) -> Config:
    if env is None:
        load_dotenv()
        env = os.environ
    token = env.get("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Не задан BOT_TOKEN (см. .env.example)")
    return Config(
        bot_token=token,
        db_path=env.get("DB_PATH", "data/bot.sqlite3"),
        llm_url=env.get("LLM_URL", "http://localhost:11434"),
        llm_model=env.get("LLM_MODEL", "qwen3:4b"),
        llm_timeout=int(env.get("LLM_TIMEOUT", "180")),
        default_tz=env.get("DEFAULT_TZ", "Europe/Moscow"),
        pro_price_stars=int(env.get("PRO_PRICE_STARS", "150")),
        pro_days=int(env.get("PRO_DAYS", "30")),
        support_contact=env.get("SUPPORT_CONTACT", ""),
        tg_api_id=int(env.get("TG_API_ID", "0") or 0),
        tg_api_hash=env.get("TG_API_HASH", "").strip(),
        tg_session=env.get("TG_SESSION", "data/userbot"),
        feed_poll_seconds=int(env.get("FEED_POLL_SECONDS", "120")),
        feed_publish_via=env.get("FEED_PUBLISH_VIA", "auto").strip().lower() or "auto",
        feed_staging_chat=int(env.get("FEED_STAGING_CHAT", "0").strip() or 0),
        feed_staging_mode=(env.get("FEED_STAGING_MODE", "copy").strip().lower() or "copy"),
        feeds_admins=parse_ids(env.get("FEEDS_ADMINS", "")),
        admin_ids=parse_ids(env.get("ADMIN_IDS", "")),
        menu_emoji=parse_menu_emoji(env.get("MENU_EMOJI", "")),
        feed_staging_keep=env.get("FEED_STAGING_KEEP", "").strip().lower() in ("1", "true", "yes"),
    )