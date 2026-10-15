"""Нейропостинг: раз в N секунд проверяет каналы-источники и постит новое в наши каналы."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any, Optional

from .access import feeds_allowed
from . import schedule, watermark
from .config import Config
from .db import Database
from .entities import (
    Entity, has_custom_emoji, mask_custom_emoji, remap_custom_emoji, restore_custom_emoji, strip_links,
)
from .feeds import MESSAGE_LIMIT, FetchedPost, Reader, compose_post, drop_last_visual, source_label
from .mirror_engine import Rewriter, _safe_dm, _should_notify, default_rewriter, rewrite_with_quota
from .publisher import copy_staged, publish_fetched, recent_sends

log = logging.getLogger(__name__)

# После долгого простоя не заливаем каналы десятками старых постов: берём только последние
MAX_BACKLOG = 10
# Чтение источника (вместе со скачиванием файлов) не должно подвешивать весь цикл навсегда
FETCH_TIMEOUT = 600.0
# Админское копирование: все существующие посты от первого к последнему, с паузой между постами
COPY_DELAY = 5.0
COPY_BATCH = 5  # постов за одно чтение: файлы лежат в памяти, пачку делаем небольшой
COPY_MAX_TRIES = 3  # столько раз пробуем недоставляемый пост, потом пропускаем, чтобы очередь не встала навсегда
_copy_fails: dict[int, tuple[int, int]] = {}  # feed_id → (id поста, неудачных попыток)
_copy_active: set[int] = set()  # источники, из которых уже копировали в этом запуске (для уведомления о конце)


async def deliver(
    bot: Any, reader: Reader, cfg: Optional[Config], feed: dict, target: int,
    post: FetchedPost, text: str, entities: list[Entity], stage: Optional[dict] = None,
) -> None:
    """Публикует пост в канал.

    Боты не умеют ставить премиум-эмодзи в каналы (ни отправкой, ни copyMessage), поэтому такие посты, как и
    файлы больше 20 МБ, публикует аккаунт-читатель (Premium, админ канала) прямо в канал получателя.
    Служебный канал (FEED_STAGING_CHAT) — запасной путь, если аккаунт не админ: бот копирует пост оттуда
    (эмодзи при этом пропадают, зато пост выходит). Иначе публикует бот.
    """
    via = (cfg.feed_publish_via if cfg is not None else "auto")
    # файлы больше 20 МБ боту не по силам (в том числе уже со знаком, скачанные аккаунтом)
    big = any(getattr(m, "big", False) or getattr(m, "force_account", False) for m in post.media)
    emoji = has_custom_emoji(entities)
    if hasattr(reader, "send") and (
        big or (via == "auto" and emoji and getattr(reader, "premium", False))
    ):
        try:
            ids = await reader.send(target, text, entities, post.media)  # type: ignore[attr-defined]
            for mid in ids:
                recent_sends.add(target, mid)
            return
        except Exception as exc:  # noqa: BLE001
            direct_error = exc
            log.warning("Публикация от имени аккаунта в %s не удалась: %s", target, exc)
        staging = cfg.feed_staging_chat if cfg is not None else 0
        copied_ok = False
        if staging and stage is not None and not stage.get("failed"):
            try:
                if "ids" not in stage:
                    stage["ids"] = await reader.send(staging, text, entities, post.media)  # type: ignore[attr-defined]
                    log.info("Витрина %s: аккаунт опубликовал %s (премиум-эмодзи в посте: %d)", staging,
                             stage["ids"], sum(1 for e in entities if e.type == "custom_emoji"))
                copied = await copy_staged(bot, target, staging, stage["ids"],
                                           forward=(cfg is not None and cfg.feed_staging_mode == "forward"))
                log.info("Витрина → %s: бот скопировал %s", target, copied)
                copied_ok = True
            except Exception as exc:  # noqa: BLE001
                if "ids" not in stage:
                    stage["failed"] = True  # не пробуем заново для остальных получателей этого поста
                log.warning("Публикация через служебный канал %s в %s не удалась: %s", staging, target, exc)
        if emoji and _should_notify(
            "acct", feed["id"] * 1_000_000_000 + abs(target) % 1_000_000_000, time.time()
        ):
            await _safe_dm(
                bot, feed["owner_id"],
                f"ℹ️ Пост с премиум-эмодзи ушёл в канал {target} не от аккаунта, и Telegram мог заменить "
                "эмодзи обычными. Чтобы они сохранялись, сделайте аккаунт-читатель (Premium) админом "
                f"этого канала с правом публикации. Причина: {direct_error}",
            )
        if copied_ok:
            return
    media = [m for m in post.media if not getattr(m, "big", False)]
    if len(media) != len(post.media):
        log.warning("В %s не отправлены файлы больше 20 МБ (%d): аккаунт-читатель должен быть админом канала",
                    target, len(post.media) - len(media))
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


async def _skip_to_latest(db: Database, reader: Reader, feed: dict) -> None:
    """Пауза: посты, вышедшие за это время, не копим и не публикуем после включения."""
    try:
        latest = await reader.latest_id(feed["username"])
    except Exception as exc:  # noqa: BLE001
        log.info("Не удалось пропустить посты на паузе: %s", exc)
        return
    if latest > feed["last_msg_id"]:
        await db.update_feed_last(feed["id"], latest)


async def _gate(db: Database, reader: Reader, feed: dict, now_ts: int) -> str:
    """Расписание владельца: 'free' / 'go' — можно читать источник, остальное — пропускаем цикл."""
    owner = feed["owner_id"]
    sched = await db.get_feed_schedule(owner)
    user = await db.get_user(owner)
    tz = (user or {}).get("tz") or "UTC"
    last = int(feed.get("sched_last") or 0)
    if sched["mode"] == "times" and not last and int(sched.get("paused_until") or 0) <= now_ts:
        await db.set_feed_sched_last(feed["id"], now_ts)  # источник добавлен/режим включён: ждём ближайшее время
        feed["sched_last"] = now_ts
        return "wait"
    state = schedule.check(sched, last, now_ts, tz)
    if state == "paused":
        await _skip_to_latest(db, reader, feed)
    return state


async def process_feed(
    bot: Any, db: Database, reader: Reader, feed: dict, rewriter: Rewriter, cfg: Optional[Config] = None,
    now: Optional[float] = None,
) -> int:
    """Обрабатывает один источник. Возвращает число успешных публикаций."""
    owner, username = feed["owner_id"], feed["username"]
    label = source_label(username, feed["title"])
    now_ts = int(time.time() if now is None else now)
    if not feed["targets"]:
        return 0  # каналы не выбраны: посты не читаем и не теряем, они дождутся выбора
    copy_mode = feed.get("kind") == "copy"
    # у копирования своя пауза (кнопка в карточке) и своя скорость: расписание и тихие часы на него не действуют
    state = "free" if copy_mode else await _gate(db, reader, feed, now_ts)
    if state in ("paused", "quiet", "wait"):
        return 0
    scheduled = state == "go"  # по расписанию: один пост за раз
    consumed = False
    try:
        scanned = 0
        if copy_mode and hasattr(reader, "fetch_oldest"):  # с первого поста: от старых к новым небольшими пачками
            posts, scanned = await asyncio.wait_for(
                reader.fetch_oldest(username, feed["last_msg_id"], COPY_BATCH), FETCH_TIMEOUT)  # type: ignore[attr-defined]
        else:
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
    if copy_mode and not posts and scanned > feed["last_msg_id"]:
        # в пачке были только служебные или пустые сообщения: двигаем курсор дальше и сразу читаем следующую
        await db.update_feed_last(feed["id"], scanned)
        _copy_active.add(feed["id"])
        return 1
    if copy_mode:
        if posts:
            _copy_active.add(feed["id"])
        elif feed["id"] in _copy_active:
            try:  # «конец» — только если курсор правда дошёл до последнего сообщения канала
                latest = await reader.latest_id(username)
            except Exception:  # noqa: BLE001
                latest = 0
            if latest > feed["last_msg_id"] and scanned <= feed["last_msg_id"]:
                log.warning("Источник %s: чтение остановилось на id %d, а в канале есть id %d: пробую снова", label,
                            feed["last_msg_id"], latest)
                return 0
            _copy_active.discard(feed["id"])
            log.info("Источник %s: все существующие посты скопированы", label)
            await _safe_dm(bot, owner, f"✅ Копирование из {label} завершено: все существующие посты опубликованы. "
                                       "Новые посты этого канала тоже будут копироваться; чтобы остановить, "
                                       "поставьте источник на паузу или удалите его в разделе «Копирование».")
    prefs = await db.get_feed_prefs(owner)
    if copy_mode:  # текст как есть: без ИИ, подписи и чистки ссылок
        prefs = {**prefs, "strip_links": False, "footer": "", "footer_entities": []}
    footer_entities = [Entity(**d) for d in prefs["footer_entities"]]
    wm = await db.get_watermark(owner)
    wm_on = wm["feeds"] and watermark.ready(wm)
    delivered = 0
    stopped = False
    for post in (posts if copy_mode else posts[-MAX_BACKLOG:]):
        text, entities = post.text, post.entities
        if prefs["strip_links"]:
            text, entities = strip_links(text, entities)  # чужие ссылки и @упоминания до ИИ
        if feed["rewrite"] and not copy_mode and text.strip():
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

        if copy_mode and len(post.media) > 1:
            trimmed = drop_last_visual(post.media)
            if len(trimmed) != len(post.media):
                log.info("Источник %s: в посте %s несколько фото/видео, последнее отброшено", label,
                         post.message_ids)
                post = replace(post, media=trimmed)
        clean = feed.get("clean") if copy_mode else None  # убрать чужой водяной знак (только копирование)
        if (wm_on or clean) and any(m.kind in ("photo", "video") for m in post.media):
            try:
                post = replace(post, media=await watermark.apply_items(
                    post.media, wm if wm_on else None, getattr(reader, "download_item", None), remove=clean))
            except Exception as exc:  # noqa: BLE001 — пост с нетронутым чужим знаком или без нашего не публикуем
                log.warning("Источник %s: фото/видео поста %s не обработаны: %s", label, post.message_ids, exc)
                if _should_notify("wm", feed["id"], time.time()):
                    await _safe_dm(bot, owner, f"⚠️ Пост из {label} пропущен: не удалось обработать фото/видео "
                                               f"(водяной знак или удаление чужого знака): {exc}")
                await db.update_feed_last(feed["id"], post.last_id)
                consumed = scheduled
                if scheduled:
                    break
                continue

        stage: dict = {}
        post_ok = False
        consumed = scheduled
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
        if copy_mode and not post_ok:
            prev = _copy_fails.get(feed["id"])
            tries = prev[1] + 1 if prev and prev[0] == post.last_id else 1
            if tries < COPY_MAX_TRIES:  # не двигаем курсор: этот пост повторится в следующий цикл
                _copy_fails[feed["id"]] = (post.last_id, tries)
                log.warning("Источник %s: пост %s не опубликован (попытка %d из %d), повторю позже", label,
                            post.message_ids, tries, COPY_MAX_TRIES)
                stopped = True
                break
            _copy_fails.pop(feed["id"], None)
            log.warning("Источник %s: пост %s пропущен после %d попыток", label, post.message_ids, tries)
        elif copy_mode:
            _copy_fails.pop(feed["id"], None)
        if post_ok:
            await db.bump_published(owner, feed=1)
        # id двигаем всегда: иначе недоставляемый пост будет повторяться бесконечно
        await db.update_feed_last(feed["id"], post.last_id)
        if scheduled:
            break  # один пост за слот расписания, остальные дождутся своей очереди
        if copy_mode and COPY_DELAY:
            await asyncio.sleep(COPY_DELAY)  # пауза между постами

    if consumed:
        await db.set_feed_sched_last(feed["id"], now_ts)
    elif posts and not copy_mode and not stopped:
        await db.update_feed_last(feed["id"], max(p.last_id for p in posts))
    return len(posts) if copy_mode and not stopped else delivered


async def run_feeds_once(
    bot: Any, db: Database, cfg: Config, reader: Reader,
    rewriter: Optional[Rewriter] = None, pause: float = 1.5, kind: Optional[str] = None,
) -> int:
    """Один проход по источникам. kind: 'feed' — обычные, 'copy' — админское копирование, None — все."""
    rewriter = rewriter or default_rewriter(cfg)
    total = 0
    allowed: dict[int, bool] = {}
    feeds = []
    for f in await db.list_active_feeds():
        if kind is not None and f.get("kind", "feed") != kind:
            continue
        owner = f["owner_id"]
        if owner not in allowed:
            allowed[owner] = await feeds_allowed(db, cfg, owner)
        if allowed[owner]:
            feeds.append(f)
    if kind != "copy" or feeds:
        log.info("Нейропостинг: проверка источников%s, активных: %d", " (копирование)" if kind == "copy" else "",
                 len(feeds))
    for feed in feeds:
        total += await process_feed(bot, db, reader, feed, rewriter, cfg)
        if pause and feed.get("kind") != "copy":
            await asyncio.sleep(pause)  # не частим к Telegram
    return total


async def _loop(bot: Any, db: Database, cfg: Config, reader: Reader, kind: str, idle: float) -> None:
    while True:
        busy = 0
        try:
            busy = await run_feeds_once(bot, db, cfg, reader, kind=kind)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Ошибка цикла нейропостинга (%s)", kind)
        # копирование с остатком постов продолжается сразу: паузу между постами уже выдержал process_feed
        await asyncio.sleep(0.5 if (kind == "copy" and busy) else idle)


async def feed_loop(bot: Any, db: Database, cfg: Config, reader: Reader) -> None:
    log.info("Нейропостинг запущен: опрос каждые %d с", max(cfg.feed_poll_seconds, 30))
    # копирование идёт отдельной задачей: долгая выкладка истории канала не задерживает обычные источники
    await asyncio.gather(
        _loop(bot, db, cfg, reader, "feed", max(cfg.feed_poll_seconds, 30)),
        _loop(bot, db, cfg, reader, "copy", 30),
    )