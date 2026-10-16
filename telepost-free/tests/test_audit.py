"""Чистка канала: классификация по алфавиту имени, отчёт, удаление, экраны, сбор подписчиков."""
from __future__ import annotations

from types import SimpleNamespace

from tests.test_handlers_flow import Base, FakeCb, datas  # первым: ставит заглушку aiogram

import unittest  # noqa: E402

from bot import audit  # noqa: E402
from bot.audit import Subscriber  # noqa: E402
from bot.handlers import channel_audit  # noqa: E402
from bot.telethon_reader import TelethonReader  # noqa: E402


def sub(i, name="", **kw):
    return Subscriber(id=i, name=name, **kw)


class ClassifyTests(unittest.TestCase):
    def test_scripts(self):
        c = audit.classify
        self.assertEqual(c(sub(1, "Иван Петров")), "ru")
        self.assertEqual(c(sub(2, "Олена Коваль")), "ru")
        self.assertEqual(c(sub(3, "Аляксей")), "ru")
        self.assertEqual(c(sub(4, "محمد علي")), "arabic")
        self.assertEqual(c(sub(5, "John Smith")), "latin")
        self.assertEqual(c(sub(6, "李雷")), "other")
        self.assertEqual(c(sub(7, "😀 123")), "unknown")
        self.assertEqual(c(sub(8, "")), "unknown")

    def test_mixed_names_count_as_ru(self):
        self.assertEqual(audit.classify(sub(1, "Anna Иванова")), "ru")
        self.assertEqual(audit.classify(sub(2, "Ahmed محمد")), "arabic")

    def test_flags_beat_name(self):
        self.assertEqual(audit.classify(sub(1, "Иван", deleted=True)), "deleted")
        self.assertEqual(audit.classify(sub(2, "Иван", bot=True)), "bot")
        self.assertEqual(audit.classify(sub(3, "Иван", fake=True)), "fake")

    def test_admins_are_skipped_and_ru_never_picked(self):
        subs = [sub(1, "Иван"), sub(2, "John"), sub(3, "Admin", is_admin=True), sub(4, "محمد")]
        g = audit.analyze(subs)
        self.assertEqual([s.id for s in g["latin"]], [2])
        self.assertEqual(sum(len(v) for v in g.values()), 3)
        everything = set(audit.CATEGORIES) | {"ru", "unknown"}
        self.assertEqual({s.id for s in audit.pick(g, everything)}, {2, 4})
        self.assertEqual({s.id for s in audit.pick(g, {"arabic"})}, {4})

    def test_report_mentions_partial_scan(self):
        g = audit.analyze([sub(1, "John")])
        self.assertIn("не всех", audit.report(g, 500, set(audit.CATEGORIES)))


class FakeBanReader:
    def __init__(self, fail_ids=(), flood_once=()):
        self.banned, self.fail_ids, self.flood_once = [], set(fail_ids), set(flood_once)

    async def ban(self, chat_id, s):
        if s.id in self.flood_once:
            self.flood_once.discard(s.id)
            raise SimpleNamespace_exc(7)
        if s.id in self.fail_ids:
            raise RuntimeError("нет прав")
        self.banned.append(s.id)


def SimpleNamespace_exc(seconds):
    e = Exception("flood")
    e.seconds = seconds
    return e


async def nosleep(_):
    return None


class CleanTests(unittest.IsolatedAsyncioTestCase):
    async def test_clean_counts_floods_and_failures(self):
        r = FakeBanReader(fail_ids={3}, flood_once={2})
        slept = []

        async def sleep(x):
            slept.append(x)

        res = await audit.clean(r, -100, [sub(1), sub(2), sub(3)], sleep=sleep, delay=0)
        self.assertEqual((res.done, res.failed), (2, 1))
        self.assertEqual(r.banned, [1, 2])
        self.assertIn(8, slept)  # переждали FloodWait 7 с + 1
        self.assertIn("нет прав", res.errors[0])

    async def test_stop(self):
        r = FakeBanReader()
        flag = {"n": 0}

        def stop():
            flag["n"] += 1
            return flag["n"] > 2

        res = await audit.clean(r, -100, [sub(i) for i in range(5)], stop=stop, sleep=nosleep, delay=0)
        self.assertTrue(res.stopped)
        self.assertEqual(res.done, 2)


class FakeIterClient:
    """Telethon-клиент: основной список обрывается, остальное находится только поиском."""

    def __init__(self):
        mk = lambda i, n, **kw: SimpleNamespace(id=i, first_name=n, last_name=None, username=None, **kw)  # noqa: E731
        self.main = [mk(1, "Иван"), mk(2, "John")]
        self.by_search = {"m": [mk(3, "محمد")], "a": [mk(2, "John")]}
        self.admin = mk(9, "Boss", participant=type("ChannelParticipantAdmin", (), {})())
        self.banned = []
        self.searches = []

    async def get_participants(self, peer, limit=0):
        return SimpleNamespace(total=4)

    async def _peer(self):
        return "peer"

    def iter_participants(self, peer, limit=None, search=""):
        async def gen():
            self.searches.append(search)
            data = self.main + [self.admin] if not search else self.by_search.get(search, [])
            for u in data:
                yield u
        return gen()

    async def get_input_entity(self, x):
        return x

    async def edit_permissions(self, peer, user, **kw):
        self.banned.append((user, kw))

    async def get_permissions(self, peer, who):
        return SimpleNamespace(is_creator=False, is_admin=True, ban_users=True)


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_adds_search_and_marks_admin(self):
        r = TelethonReader(FakeIterClient())
        r.me_id = 55
        subs, total = await r.list_subscribers(-100)
        self.assertEqual(total, 4)
        self.assertEqual({s.id for s in subs}, {1, 2, 3, 9})
        self.assertTrue(next(s for s in subs if s.id == 9).is_admin)
        self.assertGreater(len(r.client.searches), 1)  # добирали поиском
        self.assertTrue(await r.can_ban(-100))

    async def test_ban_blocks_view(self):
        r = TelethonReader(FakeIterClient())
        await r.ban(-100, sub(3, raw="rawuser"))
        self.assertEqual(r.client.banned, [("rawuser", {"view_messages": False})])


class HandlerReader:
    me_id = 777

    def __init__(self, subs, can=True):
        self.subs, self.can, self.banned = subs, can, []

    async def can_ban(self, chat_id):
        return self.can

    async def list_subscribers(self, chat_id, progress=None):
        return self.subs, len(self.subs)

    async def ban(self, chat_id, s):
        self.banned.append(s.id)


class ScreenTests(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        channel_audit._scans.clear()
        channel_audit._jobs.clear()
        self.subs = [sub(1, "Иван"), sub(2, "John"), sub(3, "محمد"), sub(4, "X", bot=True), sub(5, "Anna Иванова")]

    async def test_full_flow(self):
        reader = HandlerReader(self.subs)
        cb = FakeCb("aus:-100")
        await channel_audit.cb_scan(cb, self.db, reader)
        text, markup = cb.message.last()[1], cb.message.last()[2]
        self.assertIn("проверено 5 из 5", text)
        self.assertIn("aud:-100", datas(markup))
        self.assertNotIn("aut:-100:ru", datas(markup))  # ру удалять нельзя: кнопки нет
        # выключаем латиницу
        cb = FakeCb("aut:-100:latin")
        await channel_audit.cb_toggle(cb, self.db)
        self.assertIn("Удалить выбранных (2)", cb.message.last()[2].inline_keyboard[-3][0].text)
        cb = FakeCb("aud:-100")
        await channel_audit.cb_confirm(cb, self.db)
        self.assertIn("Удалить из канала 2", cb.message.last()[1])
        self.assertNotIn("John", cb.message.last()[1])
        cb = FakeCb("auok:-100")
        await channel_audit.cb_run(cb, self.db, reader)
        self.assertEqual(sorted(reader.banned), [3, 4])
        self.assertIn("Удалено: 2", cb.message.last()[1])
        self.assertEqual(channel_audit._jobs, {})

    async def test_analysis_works_without_ban_right_but_delete_needs_it(self):
        reader = HandlerReader(self.subs, can=False)
        cb = FakeCb("aus:-100")
        await channel_audit.cb_scan(cb, self.db, reader)
        self.assertIn("aud:-100", datas(cb.message.last()[2]))  # анализ сделан
        cb = FakeCb("auok:-100")
        await channel_audit.cb_run(cb, self.db, reader)
        self.assertIn("auf:-100", datas(cb.message.last()[2]))
        self.assertEqual(reader.banned, [])

    async def test_export_file(self):
        reader = HandlerReader([sub(1, "Иван"), sub(2, "John", username="john_x", lang="en"), sub(3, "محمد")])
        await channel_audit.cb_scan(FakeCb("aus:-100"), self.db, reader)
        docs = []
        cb = FakeCb("aul:-100")

        async def answer_document(doc, caption=None, **kw):
            docs.append((doc, caption))

        cb.message.answer_document = answer_document
        await channel_audit.cb_list_file(cb, self.db)
        body = docs[0][0].data.decode()
        self.assertIn("John | @john_x | язык en | https://t.me/john_x", body)
        self.assertIn("tg://user?id=3", body)
        self.assertNotIn("Иван", body)
        self.assertEqual(reader.banned, [])

    async def test_foreign_channel_and_no_reader(self):
        cb = FakeCb("aus:-999")
        await channel_audit.cb_scan(cb, self.db, HandlerReader(self.subs))
        self.assertEqual(cb.answers[-1], ("Канал не найден", True))
        cb = FakeCb("aus:-100")
        await channel_audit.cb_scan(cb, self.db, None)
        self.assertIn("аккаунт-читатель", cb.message.last()[1])

    async def test_expired_scan(self):
        cb = FakeCb("auok:-100")
        reader = HandlerReader(self.subs)
        await channel_audit.cb_run(cb, self.db, reader)
        self.assertEqual(reader.banned, [])
        self.assertIn("устарел", cb.message.last()[1])

    async def test_channel_card_has_button(self):
        cb = FakeCb("chv:-100")
        from bot.handlers import channels
        await channels.cb_channel_card(cb, self.db)
        self.assertIn("au:-100", datas(cb.message.last()[2]))


class LangTests(unittest.TestCase):
    def test_language_beats_name(self):
        c = audit.classify
        self.assertEqual(c(sub(1, "John", lang="uk")), "ru")
        self.assertEqual(c(sub(2, "Иван", lang="en")), "latin")
        self.assertEqual(c(sub(3, "Ivan", lang="ar-SA")), "arabic")
        self.assertEqual(c(sub(4, "Иван", lang="de")), "other")
        self.assertEqual(c(sub(5, "Иван", lang="")), "ru")