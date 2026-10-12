"""Минимальная заглушка aiogram — ТОЛЬКО для тестов в окружении без интернета.

Если настоящий aiogram установлен, `install_if_missing()` ничего не делает, и те же
тесты работают с настоящей библиотекой. Заглушка проверяет логику наших хэндлеров
(ключи данных, клавиатуры, переходы состояний), но НЕ совместимость с реальным API
aiogram/Telegram.
"""
from __future__ import annotations

import enum
import sys
import types


class _Observer:
    def __call__(self, *filters, **kwargs):
        def deco(fn):
            return fn

        return deco

    def filter(self, *a, **k):
        return None


class _Router:
    def __init__(self, name=None):
        self.message = _Observer()
        self.callback_query = _Observer()
        self.channel_post = _Observer()
        self.pre_checkout_query = _Observer()


class _Magic:
    """Объект, на котором работают .a.b == x, .startswith(), ~ и т.п. при импорте."""

    def __getattr__(self, name):
        return _Magic()

    def __call__(self, *a, **k):
        return _Magic()

    def __eq__(self, other):
        return _Magic()

    def __invert__(self):
        return _Magic()

    def __and__(self, other):
        return _Magic()

    __hash__ = object.__hash__


class _State:
    def __init__(self, *a, **k):
        pass


class _StatesGroup:
    pass


class _Plain:
    def __init__(self, *a, **kwargs):
        self.__dict__.update(kwargs)


class _Buffered:
    def __init__(self, data, filename=None):
        self.data, self.filename = data, filename


class _ContentType(str, enum.Enum):
    TEXT = "text"
    PHOTO = "photo"
    VIDEO = "video"
    ANIMATION = "animation"
    AUDIO = "audio"
    DOCUMENT = "document"
    VOICE = "voice"
    VIDEO_NOTE = "video_note"
    STICKER = "sticker"
    POLL = "poll"
    LOCATION = "location"
    VENUE = "venue"
    CONTACT = "contact"
    DICE = "dice"
    PINNED_MESSAGE = "pinned_message"


class TelegramAPIError(Exception):
    pass


def _mod(name: str, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


def install_if_missing() -> bool:
    """True, если поставили заглушку (настоящего aiogram нет)."""
    try:
        import aiogram  # noqa: F401

        return False
    except ImportError:
        pass

    root = _mod("aiogram", Bot=_Plain, Dispatcher=_Plain, F=_Magic(), Router=_Router)
    root.__path__ = []  # делаем «пакетом»
    _mod("aiogram.exceptions", TelegramAPIError=TelegramAPIError)
    _mod("aiogram.enums", ContentType=_ContentType)
    _mod(
        "aiogram.filters",
        Command=_Plain, CommandObject=_Plain, CommandStart=_Plain, StateFilter=_Plain,
    )
    fsm = _mod("aiogram.fsm")
    fsm.__path__ = []
    _mod("aiogram.fsm.context", FSMContext=_Plain)
    _mod("aiogram.fsm.state", State=_State, StatesGroup=_StatesGroup)
    storage = _mod("aiogram.fsm.storage")
    storage.__path__ = []
    _mod("aiogram.fsm.storage.memory", MemoryStorage=_Plain)
    _mod(
        "aiogram.types",
        CallbackQuery=_Plain, Chat=_Plain, InlineKeyboardButton=_Plain, InlineKeyboardMarkup=_Plain,
        Message=_Plain, LabeledPrice=_Plain, PreCheckoutQuery=_Plain, BotCommand=_Plain,
        BufferedInputFile=_Buffered, InputMediaPhoto=_Plain, InputMediaVideo=_Plain, InputMediaDocument=_Plain,
    )
    return True
