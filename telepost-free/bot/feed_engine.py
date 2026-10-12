"""Нейропостинг: раз в N секунд проверяет каналы-источники и постит новое в наши каналы."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from .access import feeds_allowed
from .config import Config
from .db import Database
from .entities import (
    Entity, has_custom_emoji, mask_custom_emoji, remap_custom_emoji, restore_custom_emoji, strip_links,
)
from .feeds import MESSAGE_LIMIT, FetchedPost, Reader, compose_post, source_label
from .mirror_engine import Rewriter, _safe_dm, _should_notify, default_rewriter, rewrite_with_quota
from .publisher import copy_staged, publish_fetched, recent_sends

log = logging.getLogger(__name__)

# После долгого простоя не заливаем каналы десятками старых постов: берём только последние
MAX_BACKLOG = 10
# Чтение источника (вместе со скачиванием файлов) не должно подвешивать весь цикл навсегда
FETCH_TIMEOUT = 600.0


async def deliver(
    bot: Any, reader: Reader, cfg: Optional[Config], feed: dict, target: int,
    post: FetchedPost, text: str, entities: list[Entity], stage: Optional[dict] = None,
) -> None:
    """Публикует пост в канал. Посты с премиум-эмодзи по возможности шлёт аккаунт-читатель (Premium,
    админ канала): бот не может надёжно ставить премиум-эмодзи в каналы. Иначе публикует бот."""
    via = (cfg.feed_publish_via if cfg is not None else "auto")
    big = any(getattr(m, "big", False) for m in post.media)  # файлы больше 20 МБ боту не по силам
    emoji = has_custom_emoji(entities)
    if hasattr(reader, "send") and (
        big or (via == "auto" and emoji and getattr(reader, "premium", False))
    ):
        staging = cfg.feed_staging_chat if cfg is not None else 0
        if staging and stage is not None and not stage.get("failed"):
            # Витрина: аккаунт один раз публикует пост в служебный канал, бот копирует его пользователю.
            try:
                if "ids" not in stage:
                    stage["ids"] = await reader.send(staging, text, entities, post.media)  # type: ignore[attr-defined]
                    log.info("Витрина %s: аккаунт опубликовал %s (премиум-эмодзи в посте: %d)", staging,
                             stage["ids"], sum(1 for e in entities if e.type == "custom_emoji"))
                copied = await copy_staged(bot, target, staging, stage["ids"],
                                           forward=(cfg is not None and cfg.feed_staging_mode == "forward"))
                log.info("Витрина → %s: бот скопировал %s", target, copied)
                return
            except Exception as exc:  # noqa: BLE001
                if "ids" not in stage:
                    stage["failed"] = True  # не пробуем заново для остальных получателей этого поста
                log.warning("Публикация через служебный канал %s в %s не удалась: %s", staging, target, exc)
        try:
            ids = await reader.send(target, text, entities, post.media)  # type: ignore[attr-defined]
            for mid in ids:
                recent_sends.add(target, mid)
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("Публикация от имени аккаунта в %s не удалась (%s), публикую ботом", target, exc)
            if emoji and _should_notify("acct", feed["id"] * 1_000_000_000 + abs(target) % 1_000_000_000,
                                        time.time()):
                await _safe_dm(
                    bot, feed["owner_id"],
                    f"ℹ️ Пост с премиум-эмодзи ушёл в канал {target} через бота, и Telegram мог заменить "
                    "эмодзи обычными. Чтобы они сохранялись, сделайте аккаунт-читатель (Premium) админом "
                    f"этого канала с правом публикации. Причина: {exc}",
                )
    media = [m for m in post.media if not getattr(m, "big", False)]
    if len(media) != len(post.media):
        log.warning("В %s не отправлены файлы больше 20 МБ (%d): нужен служебный канал FEED_STAGING_CHAT и "
                    "аккаунт-читатель", target, len(post.media) - len(media))
    await publish_fetched(bot, target, media, text, entities)


async def _clear_stage(bot: Any, reader: Reader, cfg: Optional[Config], stage: dict) -> None:
    """Убирает посты из служебного канала, чтобы он не разрастался."""
    ids, staging = stage.get("ids"), (cfg.feed_staging_chat if cfg is not None else 0)
    if not ids or not staging:
        return
    if cfg is not None and cfg.feed_staging_keep:
        log.info("Посты %s оставлены в витрине (FEED_STAGING_KEEP)", ids)
        return
    try:
        if hasattr(reader, "delete"):
            await reader.delete(staging, ids)  # type: ignore[attr-defined]
        else:
            await bot.delete_messages(chat_id=staging, message_ids=ids)
    except Exception as exc:  # noqa: BLE001
        log.info("Не удалось очистить служебный канал: %s", exc)


async def process_feed(
    bot: Any, db: Database, reader: Reader, feed: dict, rewriter: Rewriter, cfg: Optional[Config] = None
) -> int:
    """Обрабатывает один источник. Возвращает число успешных публикаций."""
    owner, username = feed["owner_id"], feed["username"]
    label = source_label(username, feed["title"])
    try:
        posts = await asyncio.wait_for(reader.fetch_new(username, feed["last_msg_id"]), FETCH_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        delay = getattr(exc, "seconds", None)  # FloodWait у Telethon
        log.warning("Источник %s не прочитан: %s", label, str(exc) or type(exc).__name__)
        if delay is not None:
            await asyncio.sleep(min(float(delay), 300.0))
        elif _should_notify("feed", feed["id"], time.time()):
            await _safe_dm(bot, owner, f"⚠️ Не удалось прочитать {label}: {exc}")
        return 0

    log.info("Источник %s: новых постов %d (после id %d)", label, len(posts), feed["last_msg_id"])
    prefs = await db.get_feed_prefs(owner)
    footer_entities = [Entity(**d) for d in prefs["footer_entities"]]
    delivered = 0
    for post in posts[-MAX_BACKLOG:]:
        text, entities = post.text, post.entities
        if prefs["strip_links"]:
            text, entities = strip_links(text, entities)  # чужие ссылки и @упоминания до ИИ
        if feed["rewrite"] and text.strip():
            log.info("Источник %s: ИИ переписывает пост %s (модель отвечает долго, это нормально)...", label,
                     post.message_ids)
            started = time.monotonic()
            # премиум-эмодзи прячем под метки [e1]...: так нейросеть их не теряет
            masked, marks = mask_custom_emoji(text, entities) if has_custom_emoji(entities) else (text, {})
            new = await rewrite_with_quota(bot, db, owner, masked, feed["style"], rewriter)
            log.info("Источник %s: рерайт %s за %.0f с", label,
                     "готов" if new is not None else "не получен (пост уйдёт без изменений)",
                     time.monotonic() - started)
            if new is not None:
                # текст изменился: форматирование не переносим, а премиум-эмодзи возвращаем по порядку
                restored = restore_custom_emoji(new, marks) if marks else None
                if restored is not None:
                    text, entities = restored
                else:
                    entities = remap_custom_emoji(text, entities, new)  # запасной путь: по символам-заменителям
                    text = new
                if prefs["strip_links"]:
                    text, entities = strip_links(text, entities)  # модель могла дописать ссылку сама
        if not text.strip() and not post.media:
            log.info("Источник %s: в посте %s после чистки ничего не осталось, пропущен", label, post.message_ids)
            await db.update_feed_last(feed["id"], post.last_id)
            continue
        final, final_entities = compose_post(text, entities, username, feed["credit"], MESSAGE_LIMIT,
                                             title=feed["title"], footer=prefs["footer"],
                                             footer_entities=footer_entities)

        stage: dict = {}
        post_ok = False
        for target in feed["targets"]:
            try:
                await deliver(bot, reader, cfg, feed, target, post, final, final_entities, stage)
                delivered += 1
                post_ok = True
                log.info("Источник %s: пост %s опубликован в %s (медиа: %d)", label, post.message_ids, target,
                         len(post.media))
            except Exception as exc:  # noqa: BLE001
                log.warning("Источник %s → %s не доставлен: %s", label, target, exc)
                if _should_notify("feedtarget", feed["id"] * 1_000_000_000 + abs(target) % 1_000_000_000,
                                  time.time()):
                    await _safe_dm(
                        bot, owner,
                        f"⚠️ Не удалось опубликовать пост из {label} в канал {target}: {exc}\n"
                        "Проверьте, что бот админ с правом публикации.",
                    )
        await _clear_stage(bot, reader, cfg, stage)
        if post_ok:
            await db.bump_published(owner, feed=1)
        # id двигаем всегда: иначе недоставляемый пост будет повторяться бесконечно
        await db.update_feed_last(feed["id"], post.last_id)

    if posts:
        await db.update_feed_last(feed["id"], max(p.last_id for p in posts))
    return delivered


async def run_feeds_once(
    bot: Any, db: Database, cfg: Config, reader: Reader,
    rewriter: Optional[Rewriter] = None, pause: float = 1.5,
) -> int:
    rewriter = rewriter or default_rewriter(cfg)
    total = 0
    allowed: dict[int, bool] = {}
    feeds = []
    for f in await db.list_active_feeds():
        owner = f["owner_id"]
        if owner not in allowed:
            allowed[owner] = await feeds_allowed(db, cfg, owner)
        if allowed[owner]:
            feeds.append(f)
    log.info("Нейропостинг: проверка источников, активных: %d", len(feeds))
    for feed in feeds:
        total += await process_feed(bot, db, reader, feed, rewriter, cfg)
        if pause:
            await asyncio.sleep(pause)  # не частим к Telegram
    return total


async def feed_loop(bot: Any, db: Database, cfg: Config, reader: Reader) -> None:
    log.info("Нейропостинг запущен: опрос каждые %d с", max(cfg.feed_poll_seconds, 30))
    while True:
        try:
            await run_feeds_once(bot, db, cfg, reader)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Ошибка цикла нейропостинга")
        await asyncio.sleep(max(cfg.feed_poll_seconds, 30))