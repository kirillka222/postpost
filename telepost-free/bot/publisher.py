"""Публикация поста в канал. Не зависит от aiogram: достаточно объекта с методами
copy_message / send_message (поэтому легко тестируется заглушкой).

Главное свойство: без изменения текста пост уходит через copy_message, а значит
Telegram сам сохраняет форматирование, кастомные (премиум) эмодзи, медиа и кнопки.
"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Optional

from . import postopts
from .entities import Entity

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096


class RecentSends:
    """Ограниченная память о сообщениях, которые отправил сам бот.

    Нужна, чтобы зеркала не зацикливались (A→B и B→A), если Telegram пришлёт
    боту channel_post о его же публикации.
    """

    def __init__(self, maxsize: int = 5000):
        self._items: OrderedDict[tuple[int, int], None] = OrderedDict()
        self._maxsize = maxsize

    def add(self, chat_id: int, message_id: int) -> None:
        self._items[(chat_id, message_id)] = None
        while len(self._items) > self._maxsize:
            self._items.popitem(last=False)

    def __contains__(self, key: tuple[int, int]) -> bool:
        return key in self._items


recent_sends = RecentSends()


async def _with_retry(fn: Callable[..., Awaitable[Any]], **kwargs: Any) -> Any:
    """Один повтор при flood-control (у aiogram исключение имеет поле retry_after)."""
    for attempt in range(2):
        try:
            return await fn(**kwargs)
        except Exception as exc:  # noqa: BLE001
            delay = getattr(exc, "retry_after", None)
            if delay is not None and attempt == 0:
                await asyncio.sleep(min(float(delay), 60.0) + 1.0)
                continue
            raise


def _bot_entities(entities: list[Any]) -> list[Any]:
    from aiogram.types import MessageEntity

    return [
        MessageEntity(type=e.type, offset=e.offset, length=e.length, url=e.url, language=e.language,
                      custom_emoji_id=e.custom_emoji_id)
        for e in entities
    ]


async def publish_fetched(
    bot: Any, chat_id: int, items: list[Any], text: str, entities: Optional[list[Any]] = None,
    flags: Optional[dict] = None, markup: Any = None,
) -> list[int]:
    """Публикует пост, скачанный из чужого канала: текст с оформлением и/или файлы (альбом до 10).

    Если Telegram не принял премиум-эмодзи (бот без прав на них), повторяем без эмодзи,
    чтобы пост всё равно вышел.
    """
    from .entities import has_custom_emoji, without_custom_emoji

    entities = list(entities or [])
    try:
        return await _publish_fetched(bot, chat_id, items, text, entities, flags, markup)
    except Exception:  # noqa: BLE001
        if not has_custom_emoji(entities):
            raise
        log.warning("Telegram отклонил премиум-эмодзи в %s, публикую без них", chat_id)
        return await _publish_fetched(bot, chat_id, items, text, without_custom_emoji(entities), flags, markup)


def _video_params(item: Any, input_file: Any) -> dict[str, Any]:
    """Свойства видео для Bot API: длительность, размер кадра, превью, потоковое воспроизведение."""
    if getattr(item, "kind", "") != "video":
        return {}
    kw: dict[str, Any] = {"supports_streaming": True}
    if getattr(item, "duration", 0):
        kw["duration"] = item.duration
    if getattr(item, "width", 0) and getattr(item, "height", 0):
        kw["width"], kw["height"] = item.width, item.height
    if getattr(item, "thumb", None):
        kw["thumbnail"] = input_file(item.thumb, filename="thumb.jpg")
    return kw


async def _publish_fetched(
    bot: Any, chat_id: int, items: list[Any], text: str, entities: list[Any],
    flags: Optional[dict] = None, markup: Any = None,
) -> list[int]:
    from aiogram.types import BufferedInputFile, InputMediaDocument, InputMediaPhoto, InputMediaVideo

    ids: list[int] = []
    text = text or ""
    flags = dict(flags or {})
    kb_kw: dict[str, Any] = {"reply_markup": markup} if markup is not None else {}
    tg_entities = _bot_entities(entities) if entities else []
    if not items:
        if text.strip():
            kw0: dict[str, Any] = {"chat_id": chat_id, "text": text[:MESSAGE_LIMIT], **flags, **kb_kw}
            if tg_entities:
                kw0["entities"] = tg_entities
            res = await _with_retry(bot.send_message, **kw0)
            ids.append(res.message_id)
    else:
        caption = text if len(text) <= CAPTION_LIMIT else ""
        extra = text if len(text) > CAPTION_LIMIT else ""
        cap_kw: dict[str, Any] = {"caption_entities": tg_entities} if (caption and tg_entities) else {}
        senders = {"photo": ("send_photo", "photo"), "video": ("send_video", "video"),
                   "document": ("send_document", "document")}
        kinds = {i.kind for i in items}
        mixed_docs = "document" in kinds and kinds != {"document"}
        if len(items) == 1 or mixed_docs:  # документы нельзя смешивать с фото/видео в альбоме
            for n, item in enumerate(items):
                method, arg = senders[item.kind]
                kw: dict[str, Any] = {
                    "chat_id": chat_id,
                    arg: BufferedInputFile(item.data, filename=item.filename),
                    **_video_params(item, BufferedInputFile),
                    **flags,
                    **(kb_kw if len(items) == 1 else {}),
                }
                if n == 0 and caption:
                    kw["caption"] = caption
                    kw.update(cap_kw)
                res = await _with_retry(getattr(bot, method), **kw)
                ids.append(res.message_id)
        else:
            classes = {"photo": InputMediaPhoto, "video": InputMediaVideo, "document": InputMediaDocument}
            media = [
                classes[item.kind](
                    media=BufferedInputFile(item.data, filename=item.filename),
                    caption=caption if n == 0 and caption else None,
                    **(cap_kw if n == 0 else {}),
                    **_video_params(item, BufferedInputFile),
                )
                for n, item in enumerate(items)
            ]
            sent = await _with_retry(bot.send_media_group, chat_id=chat_id, media=media, **flags)
            ids.extend(m.message_id for m in sent)
        if extra:
            kw1: dict[str, Any] = {"chat_id": chat_id, "text": extra[:MESSAGE_LIMIT], **flags}
            if tg_entities:
                kw1["entities"] = tg_entities
            res = await _with_retry(bot.send_message, **kw1)
            ids.append(res.message_id)

    for mid in ids:
        recent_sends.add(chat_id, mid)
    return ids


async def copy_staged(
    bot: Any, chat_id: int, src_chat: int, message_ids: list[int], forward: bool = False
) -> list[int]:
    """Переносит готовые сообщения из служебного канала: copy* (без шапки) или forward* (с шапкой «Переслано»)."""
    one, many = ("forward_message", "forward_messages") if forward else ("copy_message", "copy_messages")
    if len(message_ids) == 1:
        res = await _with_retry(getattr(bot, one), chat_id=chat_id, from_chat_id=src_chat,
                                message_id=message_ids[0])
        out = [res.message_id]
    else:
        sent = await _with_retry(getattr(bot, many), chat_id=chat_id, from_chat_id=src_chat,
                                 message_ids=message_ids)
        out = [m.message_id if hasattr(m, "message_id") else m for m in sent]
    for mid in out:
        recent_sends.add(chat_id, mid)
    return out


async def _after_publish(
    bot: Any, chat_id: int, ids: list[int], opts: dict, kb: Any, notes: Optional[list[str]],
    via_account: bool, album: bool = False,
) -> None:
    """Дела после публикации: закрепление и то, что не удалось применить (с пояснением в notes)."""

    def note(text: str) -> None:
        if notes is not None:
            notes.append(text)

    if kb is not None and album:
        note("кнопки не добавлены: Telegram не позволяет ставить кнопки под альбомом")
    if via_account:
        # аккаунт не умеет ни кнопки, ни «без звука», ни защиту: кнопки пробуем добавить ботом
        if kb is not None and ids:
            try:
                await bot.edit_message_reply_markup(chat_id=chat_id, message_id=ids[0], reply_markup=kb)
            except Exception as exc:  # noqa: BLE001
                log.warning("Кнопки к посту аккаунта в %s не добавлены: %s", chat_id, exc)
                note(f"кнопки не добавлены к посту с премиум-эмодзи ({exc}). Дайте боту право «Редактирование "
                     "сообщений» или опубликуйте пост без премиум-эмодзи")
        if opts["silent"] or opts["protect"]:
            note("«Без звука» и «Защита от копирования» не применяются к постам с премиум-эмодзи")
    if opts["nocomments"] and ids:
        from . import comments

        problem = await comments.close_comments(bot, chat_id, ids)
        if problem:
            note(problem)
    if opts["pin"] and ids:
        try:
            await bot.pin_chat_message(chat_id=chat_id, message_id=ids[0], disable_notification=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("Закрепить пост в %s не удалось: %s", chat_id, exc)
            note(f"пост не закреплён ({exc}). Дайте боту право «Закрепление сообщений»")


async def _stage_media(bot: Any, reader: Any, staging: int, src_chat: int, message_ids: list[int]) -> tuple[list, list[int]]:
    """Копирует пост в служебный канал и возвращает (медиа для аккаунта, id скопированных сообщений)."""
    if len(message_ids) == 1:
        res = await _with_retry(bot.copy_message, chat_id=staging, from_chat_id=src_chat, message_id=message_ids[0])
        staged = [res.message_id]
    else:
        sent = await _with_retry(bot.copy_messages, chat_id=staging, from_chat_id=src_chat, message_ids=message_ids)
        staged = [m.message_id if hasattr(m, "message_id") else m for m in sent]
    for mid in staged:
        recent_sends.add(staging, mid)
    try:
        return await reader.media_from(staging, staged), staged
    except Exception:
        await _cleanup_stage(bot, reader, staging, staged)
        raise


async def _cleanup_stage(bot: Any, reader: Any, staging: int, staged: list[int]) -> None:
    """Убирает временные копии из служебного канала (ошибки чистки не критичны)."""
    try:
        await bot.delete_messages(chat_id=staging, message_ids=staged)
    except Exception as exc:  # noqa: BLE001
        log.info("Не удалось удалить временные сообщения служебного канала: %s", exc)


async def publish_post(
    bot: Any,
    chat_id: int,
    src_chat: int,
    src_msg: int,
    kind: str,
    text_override: Optional[str] = None,
    extra_msgs: Optional[list[int]] = None,
    payload: Optional[dict] = None,
    reader: Any = None,
    notes: Optional[list[str]] = None,
    options: Optional[dict] = None,
    watermark: Optional[dict] = None,
) -> list[int]:
    """Публикует копию сообщения (src_chat, src_msg) в chat_id.

    kind: "text" — обычный текст, "media" — всё остальное (подпись можно заменить).
    watermark — настройки водяного знака: его накладывают на фото и видео из payload (снимок поста); если знак
    нанести не вышло, пост не публикуется (WatermarkError), чтобы в канал не попал файл без знака.
    """
    from .watermark import WatermarkError, apply_items, has_visual, ready

    ids: list[int] = []
    if watermark is not None and not ready(watermark):
        raise WatermarkError("текст водяного знака не задан (Настройки → Водяной знак)")
    has_media = has_visual(payload)
    if watermark is not None and kind != "text" and not has_media:
        if notes is not None:
            notes.append("водяной знак не наложен: этот тип вложения (стикер, голосовое…) он не поддерживает")
        watermark = None
    opts = postopts.normalize(options)
    flags: dict[str, Any] = {}
    if opts["silent"]:
        flags["disable_notification"] = True
    if opts["protect"]:
        flags["protect_content"] = True
    kb = postopts.markup(opts["buttons"])
    with_kb = {**flags, **({"reply_markup": kb} if kb is not None else {})}

    # Пост с премиум-эмодзи: бот не может поставить их в канал, публикует аккаунт (Premium, админ канала).
    # Если текст переписан ИИ, эмодзи всё равно пропадают, поэтому идём обычным путём.
    if payload and text_override is None and reader is not None and getattr(reader, "premium", False) \
            and hasattr(reader, "send"):
        from .manual import load_payload

        staged: list[int] = []
        staging = 0
        try:
            try:
                text, entities, items = await load_payload(bot, payload)
            except Exception as exc:  # noqa: BLE001
                # Bot API отдаёт боту файлы только до 20 МБ. Большие берём через служебный канал: бот копирует туда
                # пост, а аккаунт отправляет оттуда медиа по ссылке, без скачивания
                staging = int(getattr(reader, "staging_chat", 0) or 0)
                if not staging or not hasattr(reader, "media_from"):
                    raise RuntimeError(
                        f"{exc}. Для больших файлов нужен служебный канал: задайте FEED_STAGING_CHAT в .env"
                        if "big" in str(exc).lower() or "больш" in str(exc).lower() else str(exc)) from exc
                text, entities = payload["text"], [Entity(**e) for e in payload["entities"]]
                items, staged = await _stage_media(bot, reader, staging, src_chat, [src_msg, *(extra_msgs or [])])
                log.info("Файл больше лимита Bot API (%s): беру медиа из служебного канала %s", exc, staging)
            try:
                if watermark is not None:
                    items = await apply_items(items, watermark)
                ids = [int(i) for i in await reader.send(chat_id, text, entities, items)]
            finally:
                if staged:
                    await _cleanup_stage(bot, reader, staging, staged)
            for mid in ids:
                recent_sends.add(chat_id, mid)
            await _after_publish(bot, chat_id, ids, opts, kb, notes, via_account=True)
            return ids
        except WatermarkError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Публикация от имени аккаунта в %s не удалась (%s), публикую ботом", chat_id, exc)
            if notes is not None:
                notes.append(
                    "премиум-эмодзи могли стать обычными: аккаунт не смог опубликовать пост. Проверьте, что "
                    f"Premium-аккаунт — администратор канала с правом «Публикация сообщений». Причина: {exc}")

    if watermark is not None and has_media and text_override is None:
        # водяной знак: файлы скачиваем, накладываем знак и публикуем заново (копия Telegram знака не несёт)
        from .manual import load_payload

        try:
            text, entities, items = await load_payload(bot, payload)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            raise WatermarkError(f"не удалось скачать файлы поста: {exc}") from exc
        items = await apply_items(items, watermark)
        many = len(items) > 1
        ids = await publish_fetched(bot, chat_id, items, text, entities, flags=flags,
                                    markup=None if many else kb)
        await _after_publish(bot, chat_id, ids, opts, kb if many else None, notes, via_account=False, album=many)
        return ids

    if kind == "album" and extra_msgs:
        # альбом копируется целиком, с подписями и группировкой (copy_messages сохраняет альбом)
        sent = await _with_retry(
            bot.copy_messages, chat_id=chat_id, from_chat_id=src_chat, message_ids=[src_msg, *extra_msgs], **flags
        )
        ids = [m.message_id if hasattr(m, "message_id") else m for m in sent]
    elif text_override is None:
        res = await _with_retry(
            bot.copy_message, chat_id=chat_id, from_chat_id=src_chat, message_id=src_msg, **with_kb
        )
        ids.append(res.message_id)
    elif kind == "text":
        res = await _with_retry(
            bot.send_message, chat_id=chat_id, text=text_override[:MESSAGE_LIMIT], **with_kb
        )
        ids.append(res.message_id)
    elif len(text_override) <= CAPTION_LIMIT:
        res = await _with_retry(
            bot.copy_message,
            chat_id=chat_id,
            from_chat_id=src_chat,
            message_id=src_msg,
            caption=text_override,
            **with_kb,
        )
        ids.append(res.message_id)
    else:
        # новый текст не влезает в подпись: медиа без подписи + отдельное сообщение
        res = await _with_retry(
            bot.copy_message,
            chat_id=chat_id,
            from_chat_id=src_chat,
            message_id=src_msg,
            caption="",
            **flags,
        )
        ids.append(res.message_id)
        res2 = await _with_retry(
            bot.send_message, chat_id=chat_id, text=text_override[:MESSAGE_LIMIT], **with_kb
        )
        ids.append(res2.message_id)

    for mid in ids:
        recent_sends.add(chat_id, mid)
    await _after_publish(bot, chat_id, ids, opts, kb, notes, via_account=False, album=kind == "album" and bool(extra_msgs))
    return ids