"""Параметры поста при публикации: URL-кнопки (в т.ч. цветные) и переключатели (звук, закрепление, защита)."""
from __future__ import annotations

import re
from typing import Any, Optional

POST_EMOJI_ID = "5361692603727252420"  # премиум-эмодзи заголовка черновика и раздела «Создать пост»
MAX_PER_ROW = 8
MAX_ROWS = 15
COLORS = {"r": "danger", "g": "success", "b": "primary"}  # красная, зелёная, синяя (Bot API 9.4: style)
FLAGS = ("silent", "pin", "protect", "nocomments")

_COLOR = re.compile(r"^!([rgbRGB])\s+")
_DASH = re.compile(r"\s[-–—]\s")


class ButtonsError(ValueError):
    pass


def _url(raw: str) -> str:
    url = raw.strip()
    if re.match(r"^(?:www\.)?t\.me/|^(?:www\.)?telegram\.me/", url):
        url = "https://" + url
    if not re.match(r"^(?:https?|tg)://\S+$", url):
        raise ButtonsError(f"Не похоже на ссылку: «{raw.strip()[:60]}». Она должна начинаться с http:// или https://")
    return url


def parse_buttons(text: str) -> list[list[dict]]:
    """Строки — ряды, « | » — кнопки в одном ряду. Кнопка: «[!r|!g|!b] Текст - ссылка»."""
    rows: list[list[dict]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        row: list[dict] = []
        for chunk in line.split(" | "):
            chunk = chunk.strip()
            if not chunk:
                continue
            style: Optional[str] = None
            if m := _COLOR.match(chunk):
                style = COLORS[m.group(1).lower()]
                chunk = chunk[m.end():]
            parts = list(_DASH.finditer(chunk))
            if not parts:
                raise ButtonsError(f"В кнопке «{chunk[:40]}» нет « - ссылка». Формат: Текст - https://ссылка")
            cut = parts[-1]  # тире в тексте допустимо: делим по последнему
            label, url = chunk[:cut.start()].strip(), chunk[cut.end():]
            if not label:
                raise ButtonsError(f"У кнопки со ссылкой {url.strip()[:40]} нет текста.")
            if len(label) > 64:
                raise ButtonsError(f"Текст кнопки «{label[:30]}…» длиннее 64 символов.")
            row.append({"text": label, "url": _url(url), "style": style})
        if len(row) > MAX_PER_ROW:
            raise ButtonsError(f"В одном ряду может быть до {MAX_PER_ROW} кнопок.")
        if row:
            rows.append(row)
    if not rows:
        raise ButtonsError("Не нашёл ни одной кнопки. Формат: Текст - https://ссылка")
    if len(rows) > MAX_ROWS:
        raise ButtonsError(f"Рядов может быть до {MAX_ROWS}.")
    return rows


def count_buttons(rows: Optional[list[list[dict]]]) -> int:
    return sum(len(r) for r in rows or [])


def markup(rows: Optional[list[list[dict]]]) -> Any:
    """InlineKeyboardMarkup для публикации (None — кнопок нет)."""
    if not rows:
        return None
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    kb = []
    for row in rows:
        line = []
        for b in row:
            kw: dict[str, Any] = {"text": b["text"], "url": b["url"]}
            if b.get("style"):
                kw["style"] = b["style"]
            line.append(InlineKeyboardButton(**kw))
        kb.append(line)
    return InlineKeyboardMarkup(inline_keyboard=kb)


def normalize(options: Optional[dict]) -> dict:
    o = options or {}
    return {"buttons": o.get("buttons") or [], **{f: bool(o.get(f)) for f in FLAGS}}


def is_empty(options: Optional[dict]) -> bool:
    o = normalize(options)
    return not o["buttons"] and not any(o[f] for f in FLAGS)