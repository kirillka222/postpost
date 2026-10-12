"""Главное меню: постоянные кнопки под полем ввода (как у популярных ботов-постеров)."""
from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Optional

MENU_POST = "📝 Создать пост"
MENU_QUEUE = "📋 План постов"
MENU_CHANNELS = "📣 Каналы"
MENU_FEEDS = "🤖 Нейропостинг"
MENU_SETTINGS = "⚙️ Настройки"
MENU_PRO = "⭐ Тариф Pro"

_ALL = (MENU_POST, MENU_QUEUE, MENU_CHANNELS, MENU_FEEDS, MENU_SETTINGS, MENU_PRO)
# Ключи для MENU_EMOJI в .env: id премиум-эмодзи перед кнопкой
KEYS = {
    MENU_POST: "post", MENU_QUEUE: "queue", MENU_CHANNELS: "channels",
    MENU_FEEDS: "feeds", MENU_SETTINGS: "settings", MENU_PRO: "pro",
}


def plain(label: str) -> str:
    """Название кнопки без начального эмодзи: «📝 Создать пост» → «Создать пост»."""
    return label.split(" ", 1)[1] if " " in label else label


def both(label: str) -> list[str]:
    """Оба варианта текста кнопки: с обычным эмодзи и без него (когда стоит премиум-иконка)."""
    return [label, plain(label)]


# Кнопка приходит боту текстом: либо «📝 Создать пост», либо «Создать пост» (если иконка премиум)
MENU_TEXTS = tuple(t for label in _ALL for t in both(label))

def _norm(text: Optional[str]) -> str:
    """Текст кнопки для сравнения: только буквы и цифры в нижнем регистре.

    Устойчиво к эмодзи, невидимым символам и пробелам, которые клиент Telegram может добавить к тексту кнопки.
    """
    return re.sub(r"[\W_]+", "", text or "").casefold()


def menu_is(label: str) -> Callable[[Any], bool]:
    """Фильтр: сообщение — нажатие кнопки меню label (с любым оформлением эмодзи)."""
    target = _norm(plain(label))

    def check(message: Any) -> bool:
        return _norm(getattr(message, "text", None)) == target

    return check


_NORMALIZED = {_norm(plain(label)) for label in _ALL}

# Кнопка «Тариф Pro» из меню убрана; /pro работает, а старая кнопка на экране у пользователя продолжает отвечать
LAYOUT = [
    [MENU_POST, MENU_QUEUE],
    [MENU_CHANNELS, MENU_FEEDS],
    [MENU_SETTINGS],
]

SHOWN = tuple(label for row in LAYOUT for label in row)  # кнопки, которые реально показываются


def main_menu(emoji: Optional[Mapping[str, str]] = None) -> Any:
    """emoji — {ключ: id премиум-эмодзи}; для кнопок с id эмодзи ставится иконкой, а не символом в тексте."""
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    def button(label: str) -> Any:
        icon = (emoji or {}).get(KEYS[label])
        if icon:
            return KeyboardButton(text=plain(label), icon_custom_emoji_id=icon)
        return KeyboardButton(text=label)

    return ReplyKeyboardMarkup(
        keyboard=[[button(t) for t in row] for row in LAYOUT],
        resize_keyboard=True,
        is_persistent=False,  # не закреплена: в поле ввода есть кнопка «свернуть/развернуть меню»
        input_field_placeholder="Пришлите пост или выберите раздел",
    )


def not_menu(message: Any) -> bool:
    """Фильтр: сообщение — не нажатие кнопки меню (иначе её текст приняли бы за ввод в текущем шаге)."""
    return _norm(getattr(message, "text", None)) not in _NORMALIZED


FEEDS_DENIED = "🔒 Нейропостинг сейчас доступен только администратору бота."


async def feeds_denied(event, cfg, db) -> bool:  # type: ignore[no-untyped-def]
    """Фильтр: True, если у пользователя нет доступа к нейропостингу."""
    from ..access import feeds_allowed

    return not await feeds_allowed(db, cfg, event.from_user.id)