"""Обычный постинг и отложка с премиум-эмодзи: пост публикует аккаунт-читатель (Premium, админ канала).

Бот не может ставить премиум-эмодзи в каналы (Telegram разрешает это только Premium-аккаунтам), поэтому для постов,
где такие эмодзи есть, мы запоминаем текст, оформление и файлы сообщения и отправляем их от имени аккаунта.
Остальные посты по-прежнему копирует бот: это точная копия без перезаливки.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional

from .entities import Entity, from_bot, has_custom_emoji
from .feeds import MAX_MEDIA_BYTES, MediaItem


def message_text(message: Any) -> tuple[str, list[Entity]]:
    """Текст и оформление сообщения (для фото/видео/файла — подпись)."""
    if getattr(message, "text", None) is not None:
        return message.text, from_bot(getattr(message, "entities", None))
    return (getattr(message, "caption", None) or ""), from_bot(getattr(message, "caption_entities", None))


def message_media(message: Any) -> Optional[dict]:
    """Описание файла сообщения для повторной отправки; None — если файла нет или такой тип не поддержан."""
    if getattr(message, "photo", None):
        return {"kind": "photo", "file_id": message.photo[-1].file_id, "filename": "photo.jpg"}
    video = getattr(message, "video", None) or getattr(message, "animation", None)
    if video is not None:
        thumb = getattr(video, "thumbnail", None)
        return {"kind": "video", "file_id": video.file_id, "filename": getattr(video, "file_name", None) or "video.mp4",
                "duration": int(getattr(video, "duration", 0) or 0), "width": int(getattr(video, "width", 0) or 0),
                "height": int(getattr(video, "height", 0) or 0),
                "thumb_id": getattr(thumb, "file_id", None) if thumb is not None else None}
    for attr in ("document", "audio"):
        doc = getattr(message, attr, None)
        if doc is not None:
            return {"kind": "document", "file_id": doc.file_id,
                    "filename": getattr(doc, "file_name", None) or "file"}
    return None


def build_payload(messages: list[Any], force: bool = False) -> Optional[dict]:
    """Снимок поста (одно сообщение или альбом) для публикации от имени аккаунта.

    None — если в тексте нет премиум-эмодзи или в посте есть то, что аккаунту не передать (стикер, голосовое и т.п.).
    force=True — снимок нужен и без премиум-эмодзи (например, чтобы наложить водяной знак на файлы поста).
    """
    text, entities = "", []
    for m in messages:
        t, e = message_text(m)
        if t:
            text, entities = t, e
            break
    if not force and not has_custom_emoji(entities):
        return None
    media: list[dict] = []
    for m in messages:
        has_file = any(getattr(m, a, None) for a in ("photo", "video", "animation", "document", "audio",
                                                       "voice", "sticker", "video_note"))
        if not has_file:
            continue
        if any(getattr(m, a, None) for a in ("voice", "sticker", "video_note")):
            return None
        item = message_media(m)
        if item is None:
            return None
        media.append(item)
    return {"text": text, "entities": [asdict(e) for e in entities], "media": media}


async def _download(bot: Any, file_id: str) -> bytes:
    buf = await bot.download(file_id)
    data = buf.read() if hasattr(buf, "read") else bytes(buf)
    if len(data) > MAX_MEDIA_BYTES:
        raise ValueError("файл слишком большой для передачи аккаунту")
    return data


async def load_payload(bot: Any, payload: dict) -> tuple[str, list[Entity], list[MediaItem]]:
    """Скачивает файлы снимка поста: (текст, оформление, файлы) для TelethonReader.send."""
    items: list[MediaItem] = []
    for m in payload.get("media", []):
        thumb = None
        if m.get("thumb_id"):
            try:
                thumb = await _download(bot, m["thumb_id"])
            except Exception:  # noqa: BLE001 — без превью видео всё равно уйдёт
                thumb = None
        items.append(MediaItem(
            kind=m["kind"], data=await _download(bot, m["file_id"]), filename=m.get("filename") or "file",
            duration=m.get("duration", 0), width=m.get("width", 0), height=m.get("height", 0), thumb=thumb))
    return payload["text"], [Entity(**e) for e in payload["entities"]], items


__all__ = ["build_payload", "load_payload", "message_media", "message_text"]