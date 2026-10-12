"""Форматирование сообщений (жирный, ссылки, премиум-эмодзи и т.д.) как «сущности» Telegram.

Смещения (offset/length) в Telegram считаются в единицах UTF-16, а не в символах Python,
поэтому везде используется u16len(). Модуль не зависит ни от aiogram, ни от Telethon:
типы библиотек передаются снаружи.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class Entity:
    type: str  # bold, italic, underline, strikethrough, spoiler, code, pre, text_link,
    # blockquote, expandable_blockquote, custom_emoji
    offset: int  # UTF-16
    length: int  # UTF-16
    url: Optional[str] = None
    language: Optional[str] = None
    custom_emoji_id: Optional[str] = None


def u16len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


# Автоматические сущности (ссылки, @упоминания, #хештеги, почта) клиенты находят сами,
# их не переносим. Переносим только оформление, текстовые ссылки и премиум-эмодзи.
_TL_TO_BOT = {
    "MessageEntityBold": "bold",
    "MessageEntityItalic": "italic",
    "MessageEntityUnderline": "underline",
    "MessageEntityStrike": "strikethrough",
    "MessageEntitySpoiler": "spoiler",
    "MessageEntityCode": "code",
    "MessageEntityPre": "pre",
    "MessageEntityTextUrl": "text_link",
    "MessageEntityBlockquote": "blockquote",
    "MessageEntityCustomEmoji": "custom_emoji",
}


def from_telethon(entities: Optional[Iterable[Any]]) -> list[Entity]:
    """Сущности сообщения Telethon → наши. Определяем тип по имени класса."""
    out: list[Entity] = []
    for e in entities or []:
        kind = _TL_TO_BOT.get(type(e).__name__)
        if kind is None:
            continue
        if kind == "blockquote" and getattr(e, "collapsed", False):
            kind = "expandable_blockquote"
        out.append(
            Entity(
                kind,
                int(e.offset),
                int(e.length),
                url=getattr(e, "url", None) if kind == "text_link" else None,
                language=(getattr(e, "language", None) or None) if kind == "pre" else None,
                custom_emoji_id=str(e.document_id) if kind == "custom_emoji" else None,
            )
        )
    return out


def to_telethon(entities: Iterable[Entity], types: Any) -> list[Any]:
    """Наши сущности → объекты Telethon. `types` — модуль telethon.tl.types."""
    out: list[Any] = []
    for e in entities:
        kw: dict[str, Any] = {"offset": e.offset, "length": e.length}
        if e.type == "bold":
            out.append(types.MessageEntityBold(**kw))
        elif e.type == "italic":
            out.append(types.MessageEntityItalic(**kw))
        elif e.type == "underline":
            out.append(types.MessageEntityUnderline(**kw))
        elif e.type == "strikethrough":
            out.append(types.MessageEntityStrike(**kw))
        elif e.type == "spoiler":
            out.append(types.MessageEntitySpoiler(**kw))
        elif e.type == "code":
            out.append(types.MessageEntityCode(**kw))
        elif e.type == "pre":
            out.append(types.MessageEntityPre(language=e.language or "", **kw))
        elif e.type == "text_link":
            out.append(types.MessageEntityTextUrl(url=e.url or "", **kw))
        elif e.type == "blockquote":
            out.append(types.MessageEntityBlockquote(**kw))
        elif e.type == "expandable_blockquote":
            try:
                out.append(types.MessageEntityBlockquote(collapsed=True, **kw))
            except TypeError:  # старая версия Telethon без флага collapsed
                out.append(types.MessageEntityBlockquote(**kw))
        elif e.type == "custom_emoji" and e.custom_emoji_id:
            out.append(types.MessageEntityCustomEmoji(document_id=int(e.custom_emoji_id), **kw))
    return out


def _clip(entities: Iterable[Entity], start16: int, total16: int) -> list[Entity]:
    """Сдвигает сущности на start16 назад и обрезает по длине текста."""
    out: list[Entity] = []
    for e in entities:
        s = max(e.offset - start16, 0)
        end = min(e.offset + e.length - start16, total16)
        if end <= s:
            continue
        if e.type == "custom_emoji" and (end - s) != e.length:
            continue  # эмодзи должно целиком оборачивать один символ — обрезанное бессмысленно
        out.append(replace(e, offset=s, length=end - s))
    return out


def strip_text(text: str, entities: Iterable[Entity]) -> tuple[str, list[Entity]]:
    """Убирает пробелы по краям, подгоняя сущности."""
    lead = len(text) - len(text.lstrip())
    body = text.strip()
    return body, _clip(entities, u16len(text[:lead]), u16len(body))


def truncate(text: str, entities: Iterable[Entity], max_chars: int) -> tuple[str, list[Entity]]:
    if len(text) <= max_chars:
        return text, list(entities)
    body = text[:max_chars]
    return body, _clip(entities, 0, u16len(body))


def remap_custom_emoji(original: str, entities: Iterable[Entity], new_text: str) -> list[Entity]:
    """Переносит премиум-эмодзи в переписанный нейросетью текст.

    Нейросеть меняет текст, и смещения старых сущностей становятся неверными. Но обычный
    эмодзи-«заменитель» (😀 и т.п.) модель, как правило, сохраняет. Поэтому k-му такому
    символу в новом тексте возвращаем то же премиум-эмодзи, что стояло на k-м месте в оригинале.
    Остальное форматирование после рерайта не переносится.
    """
    raw = original.encode("utf-16-le")
    queues: dict[str, list[str]] = {}
    for e in sorted((x for x in entities if x.type == "custom_emoji" and x.custom_emoji_id),
                    key=lambda x: x.offset):
        ch = raw[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le", errors="ignore")
        if ch:
            queues.setdefault(ch, []).append(e.custom_emoji_id or "")
    if not queues:
        return []
    pattern = re.compile("|".join(re.escape(k) for k in sorted(queues, key=len, reverse=True)))
    used = {k: 0 for k in queues}
    out: list[Entity] = []
    for m in pattern.finditer(new_text):
        key = m.group(0)
        i = used[key]
        if i < len(queues[key]):
            used[key] = i + 1
            out.append(Entity("custom_emoji", u16len(new_text[:m.start()]), u16len(key),
                              custom_emoji_id=queues[key][i]))
    return out


def has_custom_emoji(entities: Iterable[Entity]) -> bool:
    return any(e.type == "custom_emoji" for e in entities)


def without_custom_emoji(entities: Iterable[Entity]) -> list[Entity]:
    return [e for e in entities if e.type != "custom_emoji"]


_BOT_KEEP = {"bold", "italic", "underline", "strikethrough", "spoiler", "code", "pre", "text_link",
             "blockquote", "expandable_blockquote", "custom_emoji"}


def from_bot(entities: Optional[Iterable[Any]]) -> list[Entity]:
    """Сущности сообщения aiogram → наши (для подписи, которую владелец присылает боту)."""
    out: list[Entity] = []
    for e in entities or []:
        kind = str(getattr(e, "type", ""))
        if kind in _BOT_KEEP:
            out.append(Entity(kind, int(e.offset), int(e.length), url=getattr(e, "url", None),
                              language=getattr(e, "language", None),
                              custom_emoji_id=getattr(e, "custom_emoji_id", None)))
    return out


def _delete_ranges(text: str, entities: list[Entity], ranges: list[tuple[int, int]]) -> tuple[str, list[Entity]]:
    """Вырезает диапазоны (индексы символов Python) и пересчитывает смещения сущностей (UTF-16)."""
    if not ranges:
        return text, entities
    merged: list[list[int]] = []
    for a, b in sorted(ranges):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    cut16 = [(u16len(text[:a]), u16len(text[:b])) for a, b in merged]

    def new16(o: int) -> int:
        removed = 0
        for a16, b16 in cut16:
            if o >= b16:
                removed += b16 - a16
            elif o > a16:
                removed += o - a16
        return o - removed

    out_text, last = [], 0
    for a, b in merged:
        out_text.append(text[last:a])
        last = b
    out_text.append(text[last:])
    result: list[Entity] = []
    for e in entities:
        start, end = new16(e.offset), new16(e.offset + e.length)
        if end > start:
            result.append(replace(e, offset=start, length=end - start))
    return "".join(out_text), result


_LINK = re.compile(
    r"(?:(?:https?://|tg://|www\.)\S+"
    r"|(?<![\w@/.])(?:t\.me|telegram\.me|telegra\.ph)/\S+"
    r"|(?<![\w@])@[A-Za-z][A-Za-z0-9_]{3,31}\b)",
    re.I,
)
_LINK_TRAIL = ".,;:!?)»”\"'"
# Сколько слов может остаться в строке после удаления ссылки, чтобы строку считали рекламной и убрали целиком:
# обычно 2, а если перед ссылкой стоит «подводка» («Подписаться на канал: …», «Читать 👉 …») — 4.
_MAX_RESIDUE_WORDS = 2
_MAX_RESIDUE_WORDS_LEAD_IN = 4
_LEAD_IN = (":", "-", "—", "–", "👉", "→", "➡️", "⬇️", "👇", "▶️", "➡")


def strip_links(text: str, entities: Iterable[Entity]) -> tuple[str, list[Entity]]:
    """Убирает из поста ссылки, @упоминания и «хвосты» вроде «Подписаться: @канал».

    Скрытые гиперссылки (text_link) теряют ссылку. Строка, в которой после удаления осталось совсем мало слов
    (до двух, а после подводки вроде «Подписаться: …» — до четырёх), удаляется целиком: это реклама или «Источник: …». Из длинной строки вырезаются
    только сами ссылки и упоминания, текст вокруг остаётся. Если стоит сомневаться, чистку можно выключить.
    """
    ents = list(entities)
    pos = 0
    ranges: list[tuple[int, int]] = []
    for line in text.split("\n"):
        start, end = pos, pos + len(line)
        pos = end + 1
        found: list[tuple[int, int]] = []
        for m in _LINK.finditer(line):
            tail = len(m.group(0)) - len(m.group(0).rstrip(_LINK_TRAIL))
            found.append((start + m.start(), start + m.end() - tail))
        u0, u1 = u16len(text[:start]), u16len(text[:end])
        hidden = [(max(e.offset, u0), min(e.offset + e.length, u1)) for e in ents
                  if e.type == "text_link" and e.offset < u1 and e.offset + e.length > u0]
        if not found and not hidden:
            continue
        residue = line
        for a, b in sorted(found, reverse=True):
            residue = residue[: a - start] + residue[b - start:]
        for a16, b16 in hidden:  # смещения UTF-16 → индексы символов внутри строки
            ca = _idx_from_u16(text, a16)
            cb = _idx_from_u16(text, b16)
            residue = residue.replace(text[ca:cb], "", 1)
        limit = _MAX_RESIDUE_WORDS_LEAD_IN if residue.rstrip().endswith(_LEAD_IN) else _MAX_RESIDUE_WORDS
        if len(re.findall(r"\w+", residue)) <= limit:
            ranges.append((start, min(end + 1, len(text))))  # строка целиком вместе с переводом строки
        else:
            ranges.extend(found)
    ents = [e for e in ents if e.type != "text_link"]
    text, ents = _delete_ranges(text, ents, ranges)
    # уборка: пробелы в конце строк и больше одной пустой строки подряд
    tidy = [(m.start(), m.end()) for m in re.finditer(r"[ \t]+(?=\n|$)", text)]
    tidy += [(m.start() + 2, m.end()) for m in re.finditer(r"\n{3,}", text)]
    text, ents = _delete_ranges(text, ents, tidy)
    return strip_text(text, ents)


def _idx_from_u16(text: str, offset16: int) -> int:
    units = 0
    for i, ch in enumerate(text):
        if units >= offset16:
            return i
        units += 2 if ord(ch) > 0xFFFF else 1
    return len(text)