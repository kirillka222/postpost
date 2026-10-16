"""Чистка канала от ботов и не русскоязычной аудитории.

Что делаем: аккаунт-читатель (он должен быть админом канала с правом «Блокировка пользователей») получает список
подписчиков, мы раскладываем их по категориям и по выбору владельца удаляем (блокируем) выбранные категории.

Как определяем «не ру». Telegram не отдаёт ни страну, ни телефон, ни язык интерфейса чужих аккаунтов, поэтому
остаётся то, что видно у каждого: имя и фамилия. Смотрим, каким алфавитом они написаны:
  • есть кириллица  → «ру» (Россия, Украина, Беларусь; казахи и прочие на кириллице тоже попадают сюда);
  • есть арабское письмо → «арабы/восток»;
  • только латиница → «eng»;
  • другие письменности (китайская, тайская, иврит, грузинская и т.д.) → «другое»;
  • в имени нет букв (только эмодзи/цифры) → «неопределено», не трогаем.
Отдельно считаем удалённые аккаунты, ботов и аккаунты, которые сам Telegram пометил как фейк/скам.
Латиница в имени бывает и у настоящих русскоязычных людей, поэтому каждую категорию можно включать и выключать,
а перед удалением бот показывает примеры имён.
"""
from __future__ import annotations

import asyncio
import logging
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)

# порядок = порядок на экране
CATEGORIES = ("deleted", "bot", "fake", "arabic", "latin", "other")
RU, UNKNOWN = "ru", "unknown"
LABELS = {
    "deleted": "👻 Удалённые аккаунты",
    "bot": "🤖 Боты",
    "fake": "🚩 Фейк/скам (пометка Telegram)",
    "arabic": "🕌 Арабы и восток (арабское письмо)",
    "latin": "🇬🇧 Eng (имя только латиницей)",
    "other": "🌏 Другие письменности",
    RU: "🇷🇺🇺🇦🇧🇾 Русскоязычные (кириллица)",
    UNKNOWN: "❔ Имя без букв (не трогаем)",
}
# что включено по умолчанию: «ру» и «неопределено» удалять нельзя вообще
DEFAULT_ON = frozenset(CATEGORIES)
FLOOD_RETRIES = 3
RU_LANGS = frozenset({"ru", "uk", "be"})  # Россия, Украина, Беларусь
ARABIC_LANGS = frozenset({"ar", "fa", "ur", "ps", "ku"})


@dataclass
class Subscriber:
    id: int
    name: str = ""
    username: str = ""
    deleted: bool = False
    bot: bool = False
    fake: bool = False
    is_admin: bool = False
    lang: str = ""  # языковой код аккаунта по данным Telegram (если отдал): ru, en, ar...
    raw: Any = field(default=None, repr=False, compare=False)


def script_of(text: str) -> str:
    """Алфавит имени: cyrillic / arabic / latin / other / '' (нет букв). Кириллица побеждает: смесь = «ру»."""
    found: set[str] = set()
    for ch in text or "":
        if not ch.isalpha():
            continue
        try:
            word = unicodedata.name(ch).split()[0]
        except ValueError:
            continue
        if word == "CYRILLIC":
            found.add("cyrillic")
        elif word == "ARABIC":
            found.add("arabic")
        elif word == "LATIN":
            found.add("latin")
        else:
            found.add("other")
    for key in ("cyrillic", "arabic", "other", "latin"):  # приоритет при смеси
        if key in found:
            return key
    return ""


def classify(sub: Subscriber) -> str:
    """Категория подписчика (одна, самая «сильная»)."""
    if sub.deleted:
        return "deleted"
    if sub.bot:
        return "bot"
    if sub.fake:
        return "fake"
    code = (sub.lang or "").lower().split("-")[0]
    if code:  # язык аккаунта надёжнее имени
        if code in RU_LANGS:
            return RU
        if code in ARABIC_LANGS:
            return "arabic"
        if code == "en":
            return "latin"
        return "other"
    s = script_of(sub.name)
    if s == "cyrillic":
        return RU
    if s == "arabic":
        return "arabic"
    if s == "latin":
        return "latin"
    if s == "other":
        return "other"
    return UNKNOWN


def analyze(subs: list[Subscriber]) -> dict[str, list[Subscriber]]:
    """Раскладывает подписчиков по категориям. Администраторов канала не трогаем и не считаем."""
    groups: dict[str, list[Subscriber]] = {k: [] for k in (*CATEGORIES, RU, UNKNOWN)}
    for sub in subs:
        if sub.is_admin:
            continue
        groups[classify(sub)].append(sub)
    return groups


def pick(groups: dict[str, list[Subscriber]], selected: set[str]) -> list[Subscriber]:
    """Кого удалять: только из категорий CATEGORIES, которые включены. «ру» и «неопределено» сюда не попадут никогда."""
    out: list[Subscriber] = []
    for cat in CATEGORIES:
        if cat in selected:
            out.extend(groups.get(cat, []))
    return out


def display(sub: Subscriber) -> str:
    base = sub.name.strip() or "(без имени)"
    return f"{base} (@{sub.username})" if sub.username else base


def report(groups: dict[str, list[Subscriber]], total: int, selected: set[str]) -> str:
    seen = sum(len(v) for v in groups.values())
    by_lang = sum(1 for v in groups.values() for x in v if x.lang)
    lines = [f"🔍 Анализ аудитории: проверено {seen} из {total or seen} (без администраторов)",
             f"Определено по языку аккаунта: {by_lang}, по алфавиту имени: {seen - by_lang}", ""]
    for cat in (RU, *CATEGORIES, UNKNOWN):
        n = len(groups.get(cat, []))
        if cat == RU or cat == UNKNOWN:
            lines.append(f"{LABELS[cat]}: {n}")
        else:
            lines.append(f"{'✅' if cat in selected else '⬜️'} {LABELS[cat]}: {n}")
    n = len(pick(groups, selected))
    lines += ["", f"К удалению сейчас: {n}"]
    if seen < (total or 0):
        lines.append(f"⚠️ Telegram отдал не всех подписчиков ({seen} из {total}): остальные не проверены.")
    lines += ["", "Нажмите на категорию, чтобы включить или выключить её. Русскоязычные никогда не удаляются."]
    return "\n".join(lines)


def link(sub: Subscriber) -> str:
    """Ссылка на аккаунт: по нику, а без ника — tg://user?id= (открывается в приложении, где аккаунт виден)."""
    return f"https://t.me/{sub.username}" if sub.username else f"tg://user?id={sub.id}"


def export_text(groups: dict[str, list[Subscriber]], total: int = 0) -> str:
    """Полный список не русскоязычных аккаунтов по категориям: имя, ник, язык и ссылка."""
    seen = sum(len(v) for v in groups.values())
    out = [f"Анализ аудитории: проверено {seen} из {total or seen}",
           f"Русскоязычных (не в списке): {len(groups.get(RU, []))}", ""]
    for cat in CATEGORIES:
        items = groups.get(cat, [])
        out.append(f"=== {LABELS[cat]}: {len(items)} ===")
        for s in items:
            nick = f"@{s.username}" if s.username else "(без ника)"
            how = f"язык {s.lang}" if s.lang else "по имени"
            out.append(f"{s.name.strip() or '(без имени)'} | {nick} | {how} | {link(s)}")
        out.append("")
    return "\n".join(out)


@dataclass
class CleanResult:
    done: int = 0
    failed: int = 0
    stopped: bool = False
    errors: list[str] = field(default_factory=list)


async def clean(
    reader: Any,
    chat_id: int,
    subs: list[Subscriber],
    progress: Optional[Callable[[int, int], Awaitable[None]]] = None,
    stop: Optional[Callable[[], bool]] = None,
    delay: float = 1.0,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    step: int = 20,
) -> CleanResult:
    """Блокирует подписчиков по одному с паузой (Telegram ограничивает частоту). FloodWait пережидаем."""
    res = CleanResult()
    total = len(subs)
    for i, sub in enumerate(subs, 1):
        if stop is not None and stop():
            res.stopped = True
            break
        for attempt in range(FLOOD_RETRIES + 1):
            try:
                await reader.ban(chat_id, sub)
                res.done += 1
                break
            except Exception as exc:  # noqa: BLE001
                wait = getattr(exc, "seconds", None)
                if wait is not None and attempt < FLOOD_RETRIES:
                    await sleep(min(int(wait) + 1, 3600))
                    continue
                res.failed += 1
                if len(res.errors) < 5:
                    res.errors.append(f"{display(sub)}: {exc}")
                break
        if progress is not None and (i % step == 0 or i == total):
            await progress(i, total)
        if delay and i < total:
            await sleep(delay)
    return res