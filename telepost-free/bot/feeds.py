"""Общие типы нейропостинга: пост из чужого канала, имя источника, сборка текста."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Optional, Protocol

from .entities import Entity, strip_text, truncate, u16len

MAX_MEDIA_BYTES = 20 * 1024 * 1024  # больше — не качаем (лимит бота на отправку 50 МБ, берём с запасом)
MAX_ALBUM = 10
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096


class SourceError(Exception):
    """Канал-источник недоступен или указан неверно (текст можно показать пользователю)."""


@dataclass
class MediaItem:
    kind: str  # photo | video | document
    data: bytes
    filename: str
    # Свойства видео: без них Telegram показывает «00:00», без превью и без автовоспроизведения
    duration: int = 0
    width: int = 0
    height: int = 0
    thumb: Optional[bytes] = None
    # Исходное медиа Telethon: аккаунт может отправить его по ссылке, без перезаливки и с атрибутами
    source: Any = field(default=None, repr=False, compare=False)


@dataclass
class FetchedPost:
    message_ids: list[int]
    text: str
    media: list[MediaItem] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)  # оформление и премиум-эмодзи исходного текста

    @property
    def last_id(self) -> int:
        return max(self.message_ids)


@dataclass
class SourceInfo:
    chat_id: int
    username: str
    title: str


class Reader(Protocol):
    async def resolve(self, username: str) -> SourceInfo: ...

    async def latest_id(self, username: str) -> int: ...

    async def fetch_new(self, username: str, after_id: int) -> list[FetchedPost]: ...


_PREFIX = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/", re.I)
_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_INVITE_HASH = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_TG_JOIN = re.compile(r"^tg://join\?invite=([A-Za-z0-9_-]+)$", re.I)


def is_private(key: str) -> bool:
    """Ключ закрытого канала — «+» и хэш инвайт-ссылки (регистр хэша важен)."""
    return key.startswith("+")


def feed_key(key: str) -> str:
    """Канонический вид ключа: имя публичного канала в нижнем регистре, хэш инвайта как есть."""
    return key if is_private(key) else key.lower()


def source_label(key: str, title: str = "") -> str:
    """Как показывать источник людям: @name или название закрытого канала."""
    if is_private(key):
        return f"🔒 {title}" if title else "закрытый канал"
    return f"@{key}"


def normalize_username(raw: str) -> str:
    """Что прислал пользователь → ключ источника.

    @name, t.me/name, t.me/s/name → «name»; t.me/+HASH, t.me/joinchat/HASH,
    tg://join?invite=HASH → «+HASH» (закрытый канал по инвайт-ссылке).
    """
    raw = raw.strip()
    if m := _TG_JOIN.match(raw):
        s = "+" + m.group(1)
    else:
        s = _PREFIX.sub("", raw)
        if s.lower().startswith("s/"):
            s = s[2:]
        if s.lower().startswith("joinchat/"):
            s = "+" + s.split("/", 1)[1]
        s = s.lstrip("@").split("/")[0].split("?")[0] if not s.startswith("+") else s.split("/")[0].split("?")[0]
    if s.startswith("+"):
        if not _INVITE_HASH.match(s[1:]):
            raise SourceError("Инвайт-ссылка выглядит неверно. Скопируйте её целиком, например t.me/+AbCd…")
        return s
    if not _USERNAME.match(s):
        raise SourceError("Не похоже на канал. Пришлите @username, ссылку t.me/username или инвайт-ссылку t.me/+…")
    return s


def compose_post(
    body: str, entities: list[Entity], username: str, credit: bool,
    limit: int = MESSAGE_LIMIT, title: str = "",
    footer: str = "", footer_entities: Optional[list[Entity]] = None,
) -> tuple[str, list[Entity]]:
    """Текст и сущности для публикации.

    Порядок: тело поста, строка «Источник» (если включена), своя подпись владельца (с гиперссылками и
    эмодзи). Подпись и строка «Источник» не обрезаются, обрезается только тело.
    """
    body, ents = strip_text(body or "", entities)
    foot, foot_ents = strip_text(footer or "", footer_entities or [])
    tail = ""
    if credit:
        if is_private(username):
            label = f"«{title}»" if title else "закрытый канал"
        else:
            label = f"@{username}"
        tail = f"\n\nИсточник: {label}" if body else f"Источник: {label}"
    foot_sep = "\n\n" if (body or tail) else ""
    reserve = len(tail) + (len(foot_sep) + len(foot) if foot else 0)
    body, ents = truncate(body, ents, max(limit - reserve, 0))
    text = body + tail
    if foot:
        shift = u16len(text + foot_sep)
        text = text + foot_sep + foot
        ents = ents + [replace(e, offset=e.offset + shift) for e in foot_ents]
    return text, ents


def compose_text(body: str, username: str, credit: bool, limit: int = MESSAGE_LIMIT, title: str = "") -> str:
    """То же, что compose_post, но только текст."""
    return compose_post(body, [], username, credit, limit, title)[0]