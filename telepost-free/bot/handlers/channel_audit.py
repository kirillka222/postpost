"""🧹 Чистка канала от ботов и не русскоязычной аудитории (кнопка в карточке канала).

Поток: «Проанализировать» → отчёт по категориям (их можно включать/выключать) → подтверждение с примерами имён →
удаление (блокировка) с прогрессом и кнопкой «Остановить». Удаляет аккаунт-читатель: ему нужно право
«Блокировка пользователей» в канале. Названия модуля и обработчика разные (bot/audit.py и этот файл) намеренно.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, CallbackQuery

from .. import audit
from ..db import Database
from .feedscreens import btn, kb

log = logging.getLogger(__name__)

router = Router(name="channel_audit")

SAMPLE = 12
# (владелец, канал) → результат анализа; хранится в памяти: после перезапуска бота анализ делается заново
_scans: dict[tuple[int, int], dict[str, Any]] = {}
_jobs: dict[tuple[int, int], dict[str, Any]] = {}

NO_READER = "Для чистки нужен подключённый аккаунт-читатель (TG_API_ID / TG_API_HASH и вход python -m bot.login)."

INTRO = (
    "🧹 Чистка канала\n\n"
    "Бот проверит подписчиков и покажет, сколько среди них русскоязычных (кириллица: Россия, Украина, Беларусь), "
    "арабов, eng, других иностранных, удалённых аккаунтов и ботов. Страну Telegram не раскрывает, поэтому "
    "определяем по алфавиту имени и фамилии. Потом вы выберете, кого удалить: удалённые блокируются в канале и не "
    "смогут вернуться по ссылке.\n\n"
    "Важно: латиница в имени бывает и у настоящих русскоязычных людей. Перед удалением бот покажет примеры имён, "
    "а категории можно выключать. Администраторы канала не затрагиваются никогда.\n\n"
    "Для анализа Premium-аккаунт сервиса должен быть администратором канала; для удаления ему нужно ещё право «Блокировка пользователей». Сначала можно просто посмотреть анализ и скачать список аккаунтов: ничего не удаляется."
)

NEED_RIGHT = (
    "⚠️ У аккаунта сервиса нет права «Блокировка пользователей» в этом канале.\n\n"
    "Нажмите «Выдать право» (бот сам включит его, если у него самого есть это право и право добавлять "
    "администраторов) или включите вручную: канал → Управление → Администраторы → Premium-аккаунт сервиса → "
    "«Блокировка пользователей». Затем нажмите «Проверить снова»."
)


def _key(cb: CallbackQuery, chat_id: int) -> tuple[int, int]:
    return cb.from_user.id, chat_id


def _chat_id(cb: CallbackQuery, index: int = 1) -> int:
    return int(cb.data.split(":")[index])


async def _owned(cb: CallbackQuery, db: Database, chat_id: int) -> Optional[dict]:
    channel = await db.get_channel(cb.from_user.id, chat_id)
    if channel is None:
        await cb.answer("Канал не найден", show_alert=True)
    return channel


def intro_view(chat_id: int) -> tuple[str, Any]:
    return INTRO, kb([
        [btn("🔍 Проанализировать аудиторию", f"aus:{chat_id}")],
        [btn("◀️ К каналу", f"chv:{chat_id}")],
    ])


def report_view(chat_id: int, scan: dict[str, Any]) -> tuple[str, Any]:
    groups, selected = scan["groups"], scan["selected"]
    rows = [[btn(("✅ " if c in selected else "⬜️ ") + audit.LABELS[c].split(" ", 1)[1] + f" · {len(groups[c])}",
                 f"aut:{chat_id}:{c}")] for c in audit.CATEGORIES if groups[c]]
    n = len(audit.pick(groups, selected))
    rows.append([btn("📄 Скачать список аккаунтов (файл)", f"aul:{chat_id}")])
    if n:
        rows.append([btn(f"🗑 Удалить выбранных ({n})", f"aud:{chat_id}")])
    rows.append([btn("🔄 Проанализировать заново", f"aus:{chat_id}")])
    rows.append([btn("◀️ К каналу", f"chv:{chat_id}")])
    return audit.report(groups, scan["total"], selected), kb(rows)


def confirm_view(chat_id: int, scan: dict[str, Any]) -> tuple[str, Any]:
    victims = audit.pick(scan["groups"], scan["selected"])
    sample = "\n".join("• " + audit.display(s) for s in victims[:SAMPLE])
    text = (
        f"Удалить из канала {len(victims)} подписчиков?\n\nПримеры:\n{sample}\n\n"
        "Они будут заблокированы в канале и уберутся из подписчиков. Отменить это бот не сможет (разблокировать "
        "можно только вручную в настройках канала). Скорость примерно 1 человек в секунду."
    )
    return text, kb([
        [btn(f"✅ Да, удалить {len(victims)}", f"auok:{chat_id}")],
        [btn("↩️ Нет, назад к отчёту", f"aur:{chat_id}")],
    ])


@router.callback_query(F.data.startswith("au:"))
async def cb_intro(cb: CallbackQuery, db: Database) -> None:
    chat_id = _chat_id(cb)
    if await _owned(cb, db, chat_id) is None:
        return
    text, markup = intro_view(chat_id)
    await cb.message.edit_text(text, reply_markup=markup)
    await cb.answer()


@router.callback_query(F.data.startswith("auf:"))
async def cb_grant(cb: CallbackQuery, db: Database, bot: Bot, reader: Optional[Any] = None) -> None:
    """Пробуем выдать аккаунту право блокировки от имени бота."""
    chat_id = _chat_id(cb)
    await cb.answer()
    if await _owned(cb, db, chat_id) is None or reader is None:
        return
    try:
        await bot.promote_chat_member(chat_id, reader.me_id, can_post_messages=True, can_restrict_members=True)
    except TelegramAPIError as exc:
        await cb.message.answer(
            f"❌ Telegram не позволил боту выдать право: {exc}\nВключите его вручную: канал → Управление → "
            "Администраторы → Premium-аккаунт сервиса → «Блокировка пользователей».")
        return
    await cb.message.answer("✅ Право выдано. Нажмите «Проанализировать аудиторию».")


@router.callback_query(F.data.startswith("aus:"))
async def cb_scan(cb: CallbackQuery, db: Database, reader: Optional[Any] = None) -> None:
    chat_id = _chat_id(cb)
    if await _owned(cb, db, chat_id) is None:
        return
    await cb.answer()
    if reader is None:
        await cb.message.edit_text(NO_READER, reply_markup=kb([[btn("◀️ К каналу", f"chv:{chat_id}")]]))
        return
    if _key(cb, chat_id) in _jobs:
        await cb.message.answer("Удаление уже идёт, дождитесь окончания.")
        return
    await cb.message.edit_text("🔍 Анализирую подписчиков… Для больших каналов это занимает несколько минут.")

    async def progress(n: int) -> None:
        try:
            await cb.message.edit_text(f"🔍 Анализирую подписчиков… собрано {n}")
        except TelegramAPIError:
            pass

    try:
        subs, total = await reader.list_subscribers(chat_id, progress)
    except Exception as exc:  # noqa: BLE001
        log.warning("Анализ канала %s не удался: %s", chat_id, exc)
        await cb.message.edit_text(f"❌ Не удалось получить подписчиков: {exc}",
                                   reply_markup=kb([[btn("◀️ К каналу", f"chv:{chat_id}")]]))
        return
    scan = {"groups": audit.analyze(subs), "total": total, "selected": set(audit.DEFAULT_ON)}
    _scans[_key(cb, chat_id)] = scan
    text, markup = report_view(chat_id, scan)
    await cb.message.edit_text(text, reply_markup=markup)


async def _scan_or_expired(cb: CallbackQuery, chat_id: int) -> Optional[dict[str, Any]]:
    scan = _scans.get(_key(cb, chat_id))
    if scan is None:
        await cb.message.edit_text("Результат анализа устарел (бот перезапускался). Проанализируйте заново.",
                                   reply_markup=kb([[btn("🔍 Проанализировать", f"aus:{chat_id}")]]))
    return scan


@router.callback_query(F.data.startswith("aut:"))
async def cb_toggle(cb: CallbackQuery, db: Database) -> None:
    chat_id, cat = _chat_id(cb), cb.data.split(":")[2]
    await cb.answer()
    if await _owned(cb, db, chat_id) is None or cat not in audit.CATEGORIES:
        return
    scan = await _scan_or_expired(cb, chat_id)
    if scan is None:
        return
    scan["selected"] ^= {cat}
    text, markup = report_view(chat_id, scan)
    await cb.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("aul:"))
async def cb_list_file(cb: CallbackQuery, db: Database) -> None:
    """Список всех не ру аккаунтов файлом: категории, имена, ники, языки и ссылки. Ничего не удаляет."""
    chat_id = _chat_id(cb)
    await cb.answer()
    if await _owned(cb, db, chat_id) is None:
        return
    scan = await _scan_or_expired(cb, chat_id)
    if scan is None:
        return
    data = audit.export_text(scan["groups"], scan["total"]).encode("utf-8")
    await cb.message.answer_document(BufferedInputFile(data, filename="audience.txt"),
                                     caption="Список аккаунтов по категориям. Ничего не удалено.")


@router.callback_query(F.data.startswith("aur:"))
async def cb_report(cb: CallbackQuery, db: Database) -> None:
    chat_id = _chat_id(cb)
    await cb.answer()
    if await _owned(cb, db, chat_id) is None:
        return
    scan = await _scan_or_expired(cb, chat_id)
    if scan is None:
        return
    text, markup = report_view(chat_id, scan)
    await cb.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("aud:"))
async def cb_confirm(cb: CallbackQuery, db: Database) -> None:
    chat_id = _chat_id(cb)
    await cb.answer()
    if await _owned(cb, db, chat_id) is None:
        return
    scan = await _scan_or_expired(cb, chat_id)
    if scan is None:
        return
    if not audit.pick(scan["groups"], scan["selected"]):
        text, markup = report_view(chat_id, scan)
        await cb.message.edit_text(text, reply_markup=markup)
        return
    text, markup = confirm_view(chat_id, scan)
    await cb.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("aux:"))
async def cb_stop(cb: CallbackQuery) -> None:
    job = _jobs.get(_key(cb, _chat_id(cb)))
    if job is not None:
        job["stop"] = True
    await cb.answer()


@router.callback_query(F.data.startswith("auok:"))
async def cb_run(cb: CallbackQuery, db: Database, reader: Optional[Any] = None) -> None:
    chat_id = _chat_id(cb)
    key = _key(cb, chat_id)
    await cb.answer()
    if await _owned(cb, db, chat_id) is None or reader is None:
        return
    if key in _jobs:  # повторное нажатие: уже работаем
        return
    if not await reader.can_ban(chat_id):  # право блокировки нужно только для удаления, не для анализа
        await cb.message.edit_text(NEED_RIGHT, reply_markup=kb([
            [btn("🔑 Выдать право", f"auf:{chat_id}")],
            [btn("🔄 Проверить снова", f"aud:{chat_id}")],
            [btn("◀️ К каналу", f"chv:{chat_id}")],
        ]))
        return
    scan = await _scan_or_expired(cb, chat_id)
    if scan is None:
        return
    victims = audit.pick(scan["groups"], scan["selected"])
    if not victims:
        return
    job = {"stop": False}
    _jobs[key] = job
    stop_kb = kb([[btn("⏹ Остановить", f"aux:{chat_id}")]])
    await cb.message.edit_text(f"🧹 Удаляю: 0 из {len(victims)}…", reply_markup=stop_kb)

    async def progress(done: int, total: int) -> None:
        try:
            await cb.message.edit_text(f"🧹 Удаляю: {done} из {total}…", reply_markup=stop_kb)
        except TelegramAPIError:
            pass

    try:
        res = await audit.clean(reader, chat_id, victims, progress=progress, stop=lambda: job["stop"])
    finally:
        _jobs.pop(key, None)
        _scans.pop(key, None)  # список устарел: после чистки нужен новый анализ
    lines = [("⏹ Остановлено." if res.stopped else "✅ Чистка завершена.") + f" Удалено: {res.done}."]
    if res.failed:
        lines.append(f"Не удалось: {res.failed}.")
        lines += [f"• {e}" for e in res.errors]
    await cb.message.edit_text("\n".join(lines), reply_markup=kb([
        [btn("🔍 Проанализировать заново", f"aus:{chat_id}")],
        [btn("◀️ К каналу", f"chv:{chat_id}")],
    ]))