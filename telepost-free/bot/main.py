from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from .config import load_config
from .db import Database
from .feed_engine import feed_loop
from .handlers import (
    admin, billing, channel_audit, channels, common, copyfeeds, discussion, fallback, feeds, posting, watermark_settings,
)
from .middleware import activity_middleware
from .scheduler import scheduler_loop

COMMANDS = [
    BotCommand(command="start", description="Начало работы"),
    BotCommand(command="menu", description="Показать кнопки меню"),
    BotCommand(command="channels", description="Мои каналы"),
    BotCommand(command="queue", description="План постов"),
    BotCommand(command="feeds", description="Нейропостинг из других каналов"),
    BotCommand(command="watermark", description="Водяной знак на фото и видео"),
    BotCommand(command="tz", description="Часовой пояс"),
    BotCommand(command="pro", description="Тарифы и лимиты"),
    BotCommand(command="cancel", description="Отменить действие"),
]


async def _check_staging(bot: Bot, reader: object, chat_id: int) -> None:
    """Проверяет служебный канал-витрину: аккаунт должен уметь писать в него, а бот — читать и копировать."""
    try:
        await reader._peer(chat_id)  # type: ignore[attr-defined]
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
        if getattr(member, "status", "") not in ("administrator", "creator"):
            raise RuntimeError("бот не администратор канала")
        logging.info("Служебный канал %s готов: премиум-эмодзи публикуются через него.", chat_id)
    except Exception as exc:  # noqa: BLE001
        logging.warning(
            "FEED_STAGING_CHAT=%s не работает (%s): добавьте аккаунт-читатель и бота администраторами "
            "этого канала. Пока пойдёт запасной путь.", chat_id, exc,
        )


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    db = Database(cfg.db_path)
    await db.connect()

    bot = Bot(cfg.bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    dp["db"] = db
    dp["cfg"] = cfg

    # Нейропостинг: аккаунт-читатель на Telethon (необязателен)
    reader = None
    if cfg.tg_api_id and cfg.tg_api_hash:
        try:
            from .telethon_reader import TelethonReader

            candidate = TelethonReader.create(cfg.tg_api_id, cfg.tg_api_hash, cfg.tg_session, cfg.tg_session_string)
            if await candidate.start():
                reader = candidate
                if not candidate.premium:
                    logging.warning(
                        "У аккаунта-читателя нет Telegram Premium: посты с премиум-эмодзи будут публиковаться "
                        "ботом, и Telegram может заменить эмодзи обычными."
                    )
            else:
                logging.warning("Аккаунт-читатель не авторизован: выполните python -m bot.login. Нейропостинг выключен.")
        except ImportError:
            logging.warning("Не установлен telethon (pip install telethon). Нейропостинг выключен.")
    else:
        logging.info("TG_API_ID/TG_API_HASH не заданы: нейропостинг выключен.")
    dp["reader"] = reader
    if reader is not None:
        reader.staging_chat = cfg.feed_staging_chat  # type: ignore[attr-defined]
    if reader and cfg.feed_staging_chat:
        await _check_staging(bot, reader, cfg.feed_staging_chat)

    # порядок важен: posting ловит «любое сообщение» и должен идти последним
    dp.message.outer_middleware(activity_middleware)
    dp.callback_query.outer_middleware(activity_middleware)
    dp.include_routers(discussion.router, admin.router, common.router, channels.router, feeds.router, copyfeeds.router, channel_audit.router, billing.router, watermark_settings.router, posting.router, fallback.router)

    scheduler = asyncio.create_task(scheduler_loop(bot, db, reader))
    feed_task = asyncio.create_task(feed_loop(bot, db, cfg, reader)) if reader else None
    try:
        await bot.set_my_commands(COMMANDS)
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.cancel()
        if feed_task:
            feed_task.cancel()
        if reader:
            await reader.stop()
        await db.close()
        await bot.session.close()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()