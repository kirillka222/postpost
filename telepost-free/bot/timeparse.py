"""Разбор времени отложенной публикации на русском языке.

Понимает:
  через 2 часа / через 30 минут / через день / через час
  18:30                      (сегодня, а если уже прошло — завтра)
  сегодня 18:30 / завтра 09:00 / послезавтра 10:15
  12.10 18:30 / 12.10.2026 18:30
  2026-10-12 18:30
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class TimeParseError(ValueError):
    pass


_REL = re.compile(r"^через\s+(?:(\d+)\s*)?([а-яa-z]+)$")
_TIME = re.compile(r"^(\d{1,2})[:.](\d{2})$")
_DAYWORD = re.compile(r"^(сегодня|завтра|послезавтра)\s+(\d{1,2})[:.](\d{2})$")
_DATE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s+(\d{1,2})[:.](\d{2})$")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ t](\d{1,2}):(\d{2})$")

MIN_LEAD = timedelta(seconds=30)


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise TimeParseError(f"Неизвестный часовой пояс: {name}") from exc


_OFFSET = re.compile(r"^(?:utc|gmt)?\s*([+-]\d{1,2})$", re.I)


def normalize_tz(arg: str) -> str:
    """'Europe/Moscow' остаётся как есть, 'UTC+3' / '+3' → 'Etc/GMT-3' (знак в IANA инвертирован)."""
    arg = arg.strip()
    if m := _OFFSET.match(arg):
        n = int(m.group(1))
        if not -12 <= n <= 14:
            raise TimeParseError("Смещение от UTC должно быть от -12 до +14")
        if n == 0:
            return "UTC"
        return f"Etc/GMT{'-' if n > 0 else '+'}{abs(n)}"
    get_zone(arg)  # проверка существования
    return arg


def _mk(zone: ZoneInfo, y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    try:
        return datetime(y, mo, d, h, mi, tzinfo=zone)
    except ValueError as exc:
        raise TimeParseError("Такой даты или времени не существует") from exc


def parse_when(text: str, now: datetime, tz_name: str) -> datetime:
    """Возвращает момент публикации в UTC. `now` — aware-datetime."""
    if now.tzinfo is None:
        raise ValueError("now должен быть aware-datetime")
    zone = get_zone(tz_name)
    s = " ".join(text.strip().lower().replace("ё", "е").split())
    if not s:
        raise TimeParseError("Пустое время")
    local_now = now.astimezone(zone)
    result: Optional[datetime] = None

    if m := _REL.match(s):
        n = int(m.group(1)) if m.group(1) else 1
        unit = m.group(2)
        if unit.startswith("мес"):
            raise TimeParseError("Месяцы не поддерживаются, укажите дату: 12.11 10:00")
        if unit.startswith("м"):
            delta = timedelta(minutes=n)
        elif unit.startswith("ч"):
            delta = timedelta(hours=n)
        elif unit.startswith("д"):
            delta = timedelta(days=n)
        else:
            raise TimeParseError("Не понял единицу времени (минуты, часы, дни)")
        result = now + delta
    elif m := _TIME.match(s):
        h, mi = int(m.group(1)), int(m.group(2))
        result = _mk(zone, local_now.year, local_now.month, local_now.day, h, mi)
        if result - now < MIN_LEAD:
            nxt = local_now + timedelta(days=1)
            result = _mk(zone, nxt.year, nxt.month, nxt.day, h, mi)
    elif m := _DAYWORD.match(s):
        shift = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[m.group(1)]
        day = local_now + timedelta(days=shift)
        result = _mk(zone, day.year, day.month, day.day, int(m.group(2)), int(m.group(3)))
    elif m := _DATE.match(s):
        d, mo = int(m.group(1)), int(m.group(2))
        h, mi = int(m.group(4)), int(m.group(5))
        if m.group(3):
            y = int(m.group(3))
            if y < 100:
                y += 2000
            result = _mk(zone, y, mo, d, h, mi)
        else:
            result = _mk(zone, local_now.year, mo, d, h, mi)
            if result - now < MIN_LEAD:
                result = _mk(zone, local_now.year + 1, mo, d, h, mi)
    elif m := _ISO.match(s):
        result = _mk(zone, int(m.group(1)), int(m.group(2)), int(m.group(3)),
                     int(m.group(4)), int(m.group(5)))
    else:
        raise TimeParseError(
            "Не понял время. Примеры: 18:30, завтра 09:00, 12.10 18:30, через 2 часа"
        )

    if result - now < MIN_LEAD:
        raise TimeParseError("Это время уже прошло")
    return result.astimezone(timezone.utc)


def format_local(epoch: int, tz_name: str) -> str:
    zone = get_zone(tz_name)
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(zone)
    return dt.strftime("%d.%m.%Y %H:%M") + f" ({tz_name})"
