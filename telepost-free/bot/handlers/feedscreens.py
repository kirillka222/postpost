"""Экраны нейропостинга: чистые функции «данные → (текст, кнопки)», без обработчиков.

Источники и каналы показываются отдельными списками по 8 штук на страницу, а связи «источник → каналы»
настраиваются флажками внутри карточки источника или канала: 10 источников × 10 каналов — это
не 100 кнопок, а два коротких списка.
"""
from __future__ import annotations

import time
from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import llm, schedule
from ..feeds import source_label

PAGE = 8


def short(title: str, n: int = 26) -> str:
    return title if len(title) <= n else title[: n - 1] + "…"


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def paginate(items: list, page: int) -> tuple[list, int, int]:
    pages = max(1, (len(items) + PAGE - 1) // PAGE)
    page = min(max(page, 0), pages - 1)
    return items[page * PAGE:(page + 1) * PAGE], page, pages


def nav_row(prefix: str, page: int, pages: int) -> list[InlineKeyboardButton]:
    if pages <= 1:
        return []
    return [
        btn("◀️" if page > 0 else "·", f"{prefix}:{page - 1}" if page > 0 else "fv:noop"),
        btn(f"{page + 1}/{pages}", "fv:noop"),
        btn("▶️" if page < pages - 1 else "·", f"{prefix}:{page + 1}" if page < pages - 1 else "fv:noop"),
    ]


def _targets_text(feed: dict, titles: dict[int, str]) -> str:
    names = [titles.get(t, str(t)) for t in feed["targets"]]
    if len(names) > 3:
        return f"{len(names)} каналов"
    return ", ".join(names) or "—"


# ---------- главный экран ----------
def main_view(
    feeds: list[dict], titles: dict[int, str], sched: Optional[dict] = None, tz: str = "UTC",
    now: Optional[int] = None,
) -> tuple[str, InlineKeyboardMarkup]:
    now_ts = int(time.time()) if now is None else now
    sched = sched or dict(schedule.DEFAULT)
    lines = [
        "📡 Нейропостинг",
        "Бот следит за чужими каналами и сам публикует их новые посты в ваши.",
        "",
        f"📥 Источников: {len(feeds)}   📤 Каналов: {len(titles)}",
        f"⏰ {schedule.brief(sched, now_ts, tz)}",
    ]
    if not titles:
        lines += ["", "Сначала подключите свой канал: раздел «Каналы»."]
    elif not feeds:
        lines += ["", "Добавьте первый источник — бот начнёт работать."]
    rows = [
        [btn(f"📥 Источники ({len(feeds)})", "fv:ls:0"), btn(f"📤 Мои каналы ({len(titles)})", "fv:lc:0")],
        [btn("➕ Добавить источник", "fn:start")],
        [btn("⏰ Расписание и пауза", "fs:open")],
        [btn("🎨 Оформление постов", "fv:set")],
    ]
    return "\n".join(lines), kb(rows)


# ---------- оформление ----------
def settings_view(prefs: dict) -> tuple[str, InlineKeyboardMarkup]:
    footer = prefs.get("footer") or ""
    text = (
        "🎨 Оформление постов\n\n"
        "🧹 Чужие ссылки и @упоминания: " + ("убираются" if prefs["strip_links"] else "остаются") + "\n"
        "📌 Своя подпись: " + (f"«{short(footer, 60)}»" if footer else "не задана")
    )
    rows = [
        [btn("🧹 Убирать чужие ссылки: " + ("ВКЛ" if prefs["strip_links"] else "выкл"), "fp:strip")],
        [btn("✏️ Своя подпись", "fp:footer")] + ([btn("❌ Убрать подпись", "fp:clear")] if footer else []),
        [btn("◀️ Назад", "fv:home")],
    ]
    return text, kb(rows)


# ---------- источники ----------
def sources_view(feeds: list[dict], page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    chunk, page, pages = paginate(feeds, page)
    text = "📥 Источники\nНажмите на источник, чтобы настроить его."
    if not feeds:
        text = "📥 Источники\nПока пусто. Добавьте первый источник."
    rows = []
    for f in chunk:
        mark = "" if f["active"] else "⏸ "
        rows.append([btn(f"{mark}{short(source_label(f['username'], f['title']), 30)} · {len(f['targets'])} 📤",
                         f"fv:s:{f['id']}")])
    nav = nav_row("fv:ls", page, pages)
    if nav:
        rows.append(nav)
    rows.append([btn("➕ Добавить источник", "fn:start")])
    rows.append([btn("◀️ Назад", "fv:home")])
    return text, kb(rows)


def source_view(feed: dict, titles: dict[int, str], total_channels: int) -> tuple[str, InlineKeyboardMarkup]:
    ai = ("ИИ: " + llm.STYLE_LABELS[feed["style"]].split(" ", 1)[1]) if feed["rewrite"] else "без ИИ"
    credit = "со ссылкой на источник" if feed["credit"] else "без ссылки"
    state = "работает" if feed["active"] else "на паузе"
    text = (
        f"📥 {source_label(feed['username'], feed['title'])}\n"
        f"→ {_targets_text(feed, titles)}\n\n"
        f"{ai} · {credit} · {state}"
    )
    if not feed["targets"]:
        text += "\n\n⚠️ Каналы не выбраны: посты не публикуются."
    fid = feed["id"]
    rows = [[
        btn("🤖 ИИ: " + ("вкл" if feed["rewrite"] else "выкл"), f"fv:ai:{fid}"),
        btn("🔗 Ссылка: " + ("вкл" if feed["credit"] else "выкл"), f"fv:cr:{fid}"),
    ]]
    if feed["rewrite"]:
        rows.append([
            btn(("• " if feed["style"] == s else "") + label.split(" ", 1)[1], f"fv:st:{fid}:{s}")
            for s, label in llm.STYLE_LABELS.items()
        ])
    rows.append([btn(f"📤 Каналы: {len(feed['targets'])} из {total_channels}", f"fv:tg:{fid}:0")])
    rows.append([
        btn("⏸ Пауза" if feed["active"] else "▶️ Включить", f"fv:act:{fid}"),
        btn("🗑 Удалить", f"fv:del:{fid}"),
    ])
    rows.append([btn("◀️ К источникам", "fv:ls:0")])
    return text, kb(rows)


def delete_view(feed: dict) -> tuple[str, InlineKeyboardMarkup]:
    text = f"Удалить источник {source_label(feed['username'], feed['title'])}?"
    return text, kb([[btn("🗑 Да, удалить", f"fv:delok:{feed['id']}"), btn("✖️ Отмена", f"fv:s:{feed['id']}")]])


def targets_view(feed: dict, channels: list[dict], page: int = 0, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    """Куда публиковать источник: флажки по каналам, «все каналы»."""
    chunk, page, pages = paginate(channels, page)
    chosen = set(feed["targets"])
    text = f"📤 Куда публиковать {source_label(feed['username'], feed['title'])}\nОтметьте каналы."
    if note:
        text += f"\n\n{note}"
    fid = feed["id"]
    rows = [[btn(("✅ " if c["chat_id"] in chosen else "❌ ") + short(c["title"]), f"fv:tt:{fid}:{c['chat_id']}:{page}")]
            for c in chunk]
    nav = nav_row(f"fv:tg:{fid}", page, pages)
    if nav:
        rows.append(nav)
    everyone = bool(channels) and all(c["chat_id"] in chosen for c in channels)
    rows.append([btn("❌ Снять все каналы" if everyone else "✅ Выбрать все каналы", f"fv:ta:{fid}:{page}")])
    rows.append([btn("◀️ Готово", f"fv:s:{fid}")])
    return text, kb(rows)


# ---------- каналы ----------
def channels_view(channels: list[dict], feeds: list[dict], page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    chunk, page, pages = paginate(channels, page)
    text = "📤 Мои каналы\nНажмите на канал, чтобы выбрать, откуда брать для него посты."
    if not channels:
        text = "📤 Мои каналы\nКаналов пока нет. Подключите канал в разделе «Каналы»."
    rows = []
    for c in chunk:
        n = sum(1 for f in feeds if c["chat_id"] in f["targets"])
        rows.append([btn(f"{short(c['title'], 30)} · {n} 📥", f"fv:c:{c['chat_id']}:0")])
    nav = nav_row("fv:lc", page, pages)
    if nav:
        rows.append(nav)
    rows.append([btn("◀️ Назад", "fv:home")])
    return text, kb(rows)


def channel_view(channel: dict, feeds: list[dict], page: int = 0, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    """Источники канала: флажки по источникам, «все» и «снять все»."""
    chunk, page, pages = paginate(feeds, page)
    chat = channel["chat_id"]
    n = sum(1 for f in feeds if chat in f["targets"])
    text = f"📤 {channel['title']}\nИсточников подключено: {n} из {len(feeds)}."
    if not feeds:
        text = f"📤 {channel['title']}\nИсточников пока нет. Добавьте первый, и его можно будет подключить сюда."
    if note:
        text += f"\n\n{note}"
    rows = [[btn(("✅ " if chat in f["targets"] else "❌ ") + short(source_label(f["username"], f["title"]), 30),
                 f"fv:ct:{chat}:{f['id']}:{page}")] for f in chunk]
    nav = nav_row(f"fv:c:{chat}", page, pages)
    if nav:
        rows.append(nav)
    if feeds:
        everyone = all(chat in f["targets"] for f in feeds)
        rows.append([btn("❌ Снять все источники" if everyone else "✅ Выбрать все источники",
                         f"fv:{'cn' if everyone else 'ca'}:{chat}:{page}")])
    rows.append([btn("➕ Добавить источник", "fn:start")])
    rows.append([btn("◀️ К каналам", "fv:lc:0")])
    return text, kb(rows)