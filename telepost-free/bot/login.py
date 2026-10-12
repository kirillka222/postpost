"""Одноразовый вход аккаунта-читателя: python -m bot.login

Спросит номер телефона, код из Telegram и (если включён) пароль 2FA. Сессия сохранится
в файл TG_SESSION (по умолчанию data/userbot.session). Используйте отдельный аккаунт.
"""
from __future__ import annotations

import asyncio

from .config import load_config


async def main() -> None:
    cfg = load_config()
    if not cfg.tg_api_id or not cfg.tg_api_hash:
        raise SystemExit("Заполните TG_API_ID и TG_API_HASH в .env (получить: https://my.telegram.org → API development tools)")
    from telethon import TelegramClient

    client = TelegramClient(cfg.tg_session, cfg.tg_api_id, cfg.tg_api_hash)
    await client.start()  # интерактивно спросит телефон/код/пароль
    me = await client.get_me()
    print(f"Готово: вошли как {me.first_name} (id {me.id}). Сессия сохранена: {cfg.tg_session}.session")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
