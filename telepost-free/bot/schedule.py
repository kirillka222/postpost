"""Расписание нейропостинга: интервал, заданное время, тихие часы и пауза.

Чистые функции без БД и Telegram: проще проверять тестами. Время считается в часовом поясе пользователя.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from .timeparse import TimeParseError, get_zone

FOREVER = 4_102_444_800  # 2100-01-01: «пауза до включения»
MIN_EVERY, MAX_EVERY = 5, 1440
INTERVALS = (30, 60, 120, 180, 360)
PAUSES = ((1, "1 час"), (3, "3 часа"), (12, "12 часов"))
MAX_TIMES = 24

DEFAULT: dict = {"mode": "off", "every": 60, "times": [], "quiet_from": "", "quiet_to": "", "paused_until": 0}
MODES = ("off", "interval", "times")


class ScheduleError(ValueError):
    """Понятное пользователю сообщение о неверном вводе."""


_TIME = re.compile(r"^(\d{1,2})(?:[:.](\d{1,2}))?$")
_WINDOW = re.compile(r"^\s*(\S+?)\s*(?:-|–|—|до)\s*(\S+?)\s*$")
_EVERY = re.compile(r"^(\d+(?:[.,]\d+)?)\s*(мин\w*|м|m|min|ч\w*|h|hour\w*)?$", re.I)


def parse_time(token: str) -> str:
    """'9' → '09:00', '9.30' / '9:30' → '09:30'."""
    m = _TIME.match(token.strip())
    if not m:
        raise ScheduleError(f"Не понял время «{token}». Пример: 09:00")
    h = int(m.group(1))
    mi = int(m.group(2)) if m.group(2) is not None else 0
    if m.group(2) is not None and len(m.group(2)) != 2:
        raise ScheduleError(f"Минуты пишите двумя цифрами: «{token}» → например 09:05")
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ScheduleError(f"Такого времени не бывает: «{token}»")
    return f"{h:02d}:{mi:02d}"


def parse_times(text: str) -> list[str]:
    tokens = [t for t in re.split(r"[,;\s]+", text.strip()) if t]
    if not tokens:
        raise ScheduleError("Пришлите время через запятую. Пример: 09:00, 13:30, 18:00")
    times = sorted({parse_time(t) for t in tokens})
    if len(times) > MAX_TIMES:
        raise ScheduleError(f"Слишком много: максимум {MAX_TIMES} значений в сутки")
    return times


def parse_window(text: str) -> tuple[str, str]:
    m = _WINDOW.match(text)
    if not m:
        raise ScheduleError("Пришлите промежуток так: 23:00-08:00")
    a, b = parse_time(m.group(1)), parse_time(m.group(2))
    if a == b:
        raise ScheduleError("Начало и конец совпадают. Пример: 23:00-08:00")
    return a, b


def parse_every(text: str) -> int:
    """'45', '45 мин', '2 ч', '1,5ч' → минуты."""
    m = _EVERY.match(text.strip())
    if not m:
        raise ScheduleError("Пришлите интервал числом: 45 (минут) или 2 ч")
    value = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or "").lower()
    minutes = int(round(value * 60)) if unit.startswith(("ч", "h")) else int(round(value))
    if not MIN_EVERY <= minutes <= MAX_EVERY:
        raise ScheduleError(f"Интервал от {MIN_EVERY} минут до {MAX_EVERY // 60} часов")
    return minutes


def _mins(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def in_quiet(sched: dict, local: datetime) -> bool:
    a, b = sched.get("quiet_from") or "", sched.get("quiet_to") or ""
    if not a or not b:
        return False
    now_m = local.hour * 60 + local.minute
    lo, hi = _mins(a), _mins(b)
    return lo <= now_m < hi if lo < hi else (now_m >= lo or now_m < hi)


def last_slot(times: list[str], now_ts: int, tz_name: str) -> Optional[int]:
    """Последнее запланированное время, которое уже наступило (epoch), либо None."""
    zone = get_zone(tz_name)
    local = datetime.fromtimestamp(now_ts, tz=timezone.utc).astimezone(zone)
    best: Optional[int] = None
    for day in (local.date(), local.date() - timedelta(days=1)):
        for t in times:
            dt = datetime(day.year, day.month, day.day, int(t[:2]), int(t[3:5]), tzinfo=zone)
            ts = int(dt.timestamp())
            if ts <= now_ts and (best is None or ts > best):
                best = ts
    return best


def check(sched: dict, last_ts: int, now_ts: int, tz_name: str) -> str:
    """Можно ли сейчас публиковать из источника.

    'paused' — пауза; 'quiet' — тихие часы; 'wait' — ещё не время; 'go' — опубликовать ОДИН пост;
    'free' — расписание не ограничивает (как раньше: всё новое сразу).
    """
    if int(sched.get("paused_until") or 0) > now_ts:
        return "paused"
    try:
        local = datetime.fromtimestamp(now_ts, tz=timezone.utc).astimezone(get_zone(tz_name))
    except TimeParseError:
        local = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    if in_quiet(sched, local):
        return "quiet"
    mode = sched.get("mode", "off")
    if mode == "interval":
        every = max(int(sched.get("every") or 60), MIN_EVERY) * 60
        return "go" if not last_ts or now_ts - last_ts >= every else "wait"
    if mode == "times":
        slot = last_slot(list(sched.get("times") or []), now_ts, tz_name)
        return "go" if slot is not None and last_ts < slot else "wait"
    return "free"


def fmt_every(minutes: int) -> str:
    if minutes % 60 == 0:
        h = minutes // 60
        return f"{h} ч"
    if minutes > 60:
        return f"{minutes // 60} ч {minutes % 60} мин"
    return f"{minutes} мин"


def describe(sched: dict, now_ts: int, tz_name: str) -> list[str]:
    """Строки для экрана «Расписание»."""
    mode = sched.get("mode", "off")
    if mode == "interval":
        mode_line = f"🔁 по одному посту раз в {fmt_every(int(sched.get('every') or 60))}"
    elif mode == "times":
        times = ", ".join(sched.get("times") or []) or "время не задано"
        mode_line = f"🕒 по одному посту в: {times}"
    else:
        mode_line = "⚡ сразу, как только пост вышел в источнике"
    quiet = (f"🌙 {sched['quiet_from']}–{sched['quiet_to']}: посты не публикуются"
             if sched.get("quiet_from") else "🌙 тихое время не задано")
    until = int(sched.get("paused_until") or 0)
    if until > now_ts:
        if until >= FOREVER:
            pause = "⏸ нейропостинг приостановлен до включения"
        else:
            zone = get_zone(tz_name)
            local = datetime.fromtimestamp(until, tz=timezone.utc).astimezone(zone)
            pause = f"⏸ пауза до {local.strftime('%d.%m %H:%M')}"
    else:
        pause = "▶️ работает"
    return [mode_line, quiet, pause]


def brief(sched: dict, now_ts: int, tz_name: str) -> str:
    """Одна короткая строка для главного экрана: «сразу · 🌙 23:00–08:00 · ⏸ пауза до 15:30»."""
    mode = sched.get("mode", "off")
    if mode == "interval":
        parts = [f"раз в {fmt_every(int(sched.get('every') or 60))}"]
    elif mode == "times":
        parts = [", ".join(sched.get("times") or []) or "время не задано"]
    else:
        parts = ["сразу"]
    if sched.get("quiet_from"):
        parts.append(f"🌙 {sched['quiet_from']}–{sched['quiet_to']}")
    until = int(sched.get("paused_until") or 0)
    if until > now_ts:
        if until >= FOREVER:
            parts.append("⏸ приостановлен")
        else:
            local = datetime.fromtimestamp(until, tz=timezone.utc).astimezone(get_zone(tz_name))
            parts.append(f"⏸ пауза до {local.strftime('%d.%m %H:%M')}")
    return " · ".join(parts)