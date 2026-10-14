"""Планировщик отложенных постов: раз в несколько секунд берёт созревшие посты из БД."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from .db import Database
from .publisher import publish_post

log = logging.getLogger(__name__)

POLL_SECONDS = 10


async def run_due_posts(bot: Any, db: Database, now: Optional[int] = None, reader: Any = None) -> int:
    """Публикует все созревшие посты. Возвращает число обработанных постов."""
    now = int(time.time()) if now is None else now
    posts = await db.claim_due_posts(now)
    for post in posts:
        ok_titles: list[str] = []
        errors: list[str] = []
        notes: list[str] = []
        wm = await db.get_watermark(post["owner_id"]) if (post.get("options") or {}).get("watermark") else None
        for chat_id in post["chat_ids"]:
            channel = await db.get_channel(post["owner_id"], chat_id)
            title = channel["title"] if channel else str(chat_id)
            try:
                await publish_post(
                    bot, chat_id, post["src_chat"], post["src_msg"],
                    post["kind"], post["text_override"], post.get("extra_msgs"),
                    post.get("payload"), reader, notes, post.get("options"), wm,
                )
                ok_titles.append(title)
            except Exception as exc:  # noqa: BLE001
                log.warning("Пост #%s → %s не отправлен: %s", post["id"], chat_id, exc)
                errors.append(f"{title}: {exc}")
            await asyncio.sleep(0.05)

        status = "done" if ok_titles else "failed"
        if ok_titles:
            await db.bump_published(post["owner_id"], manual=1)
        await db.finish_post(post["id"], status, "; ".join(errors))

        lines = []
        if ok_titles:
            lines.append(f"✅ Отложенный пост #{post['id']} опубликован: " + ", ".join(ok_titles))
        if errors:
            lines.append("⚠️ Ошибки:\n" + "\n".join(errors))
        if notes:
            lines.append("ℹ️ " + "\n".join(dict.fromkeys(notes)))
        try:
            await bot.send_message(chat_id=post["owner_id"], text="\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось уведомить владельца поста #%s: %s", post["id"], exc)
    return len(posts)


async def scheduler_loop(bot: Any, db: Database, reader: Any = None) -> None:
    restored = await db.recover_stuck_posts()
    if restored:
        log.info("Возвращено в очередь после перезапуска: %s", restored)
    while True:
        try:
            await run_due_posts(bot, db, reader=reader)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Ошибка планировщика")
        await asyncio.sleep(POLL_SECONDS)