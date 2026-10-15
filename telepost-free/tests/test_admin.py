"""Админка (/admin), учёт активности и счётчики публикаций."""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

from tests.test_handlers_flow import Base, FakeCb, FakeMsg, datas  # первым: ставит заглушку aiogram

from bot import access, feed_engine, middleware  # noqa: E402
from bot.config import Config, load_config  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.handlers import admin  # noqa: E402

ADMIN = 1
USER_A = 555


class AdminBase(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cfg = Config(bot_token="x", admin_ids=(ADMIN,))
        await self.db.touch_user(USER_A, "Alice_X", "Алиса Иванова", "Europe/Moscow")
        await self.db.add_channel(USER_A, -500, "Канал Алисы", "alice_chan")
        await self.db.add_feed(USER_A, "src", "Источник", 0, [-500])


class AdminAccessTests(AdminBase):
    async def test_config_admins(self):
        self.assertTrue(Config(bot_token="x", admin_ids=(5,)).is_admin(5))
        self.assertFalse(Config(bot_token="x", admin_ids=(5,)).is_admin(6))
        # без ADMIN_IDS админами считаются FEEDS_ADMINS, без обоих — никто
        self.assertTrue(Config(bot_token="x", feeds_admins=(7,)).is_admin(7))
        self.assertFalse(Config(bot_token="x").is_admin(1))
        self.assertEqual(load_config({"BOT_TOKEN": "x", "ADMIN_IDS": "1, 2"}).admin_ids, (1, 2))

    async def test_filters(self):
        self.assertTrue(admin.is_admin(FakeMsg(user_id=ADMIN), self.cfg))
        self.assertFalse(admin.is_admin(FakeMsg(user_id=2), self.cfg))
        self.assertTrue(admin.not_admin(FakeMsg(user_id=2), self.cfg))

    async def test_denied_callback(self):
        cb = FakeCb("ad:home", user_id=2)
        await admin.cb_denied(cb)
        self.assertEqual(cb.answers, [("Нет доступа", True)])


class AdminPanelTests(AdminBase):
    async def test_dashboard_counts(self):
        await self.db.bump_published(USER_A, manual=3, feed=2)
        await self.db.extend_pro(USER_A, 30)
        msg = FakeMsg(user_id=ADMIN)
        await admin.cmd_admin(msg, self.db, self.cfg, self.state)
        text = msg.last()[1]
        self.assertIn("Пользователей всего:", text)
        self.assertIn("С Pro: 1", text)
        self.assertIn("Опубликовано вручную/по плану: 3", text)
        self.assertIn("нейропостингом: 2", text)
        self.assertIn("Активных источников: 1", text)
        self.assertEqual(datas(msg.last()[2]), ["ad:list:0", "ad:find", "cp:home", "ad:home"])

    async def test_new_and_active_today(self):
        now = int(time.time())
        start = admin.day_start(now, "Europe/Moscow")
        before = await self.db.admin_stats(now, start)
        await self.db.touch_user(900, "old", "Старый", "UTC", now=now - 3 * 86400)
        await self.db.touch_user(901, "wk", "Давний", "UTC", now=now - 20 * 86400)
        after = await self.db.admin_stats(now, start)
        self.assertEqual(after["users"], before["users"] + 2)
        # зарегистрированные несколько дней назад не считаются «новыми» и «заходившими сегодня»
        self.assertEqual((after["new_today"], after["active_today"]), (before["new_today"], before["active_today"]))
        self.assertEqual(after["active_7d"], before["active_7d"] + 1)  # 3 дня назад — да, 20 — нет

    async def test_profile_by_username_and_id(self):
        await self.db.extend_pro(USER_A, 30)
        await self.db.bump_published(USER_A, manual=4, feed=1)
        for query in ("@alice_x", "Alice_X", str(USER_A)):
            msg = FakeMsg(user_id=ADMIN)
            await admin._send_profile(msg, self.db, self.cfg, query)
            text = msg.last()[1]
            self.assertIn("@Alice_X", text, query)
            self.assertIn(f"ID: {USER_A}", text)
            self.assertIn("Статус: ⭐ Pro до", text)
            self.assertIn("Канал Алисы (@alice_chan) · -500", text)
            self.assertIn("Источник", text)
            self.assertIn("Опубликовано постов: 4", text)
            self.assertIn("нейропостингом: 1", text)
            self.assertIn("Последняя активность:", text)

    async def test_free_status_and_not_found(self):
        msg = FakeMsg(user_id=ADMIN)
        await admin._send_profile(msg, self.db, self.cfg, str(USER_A))
        self.assertIn("Статус: Free", msg.last()[1])
        nf = FakeMsg(user_id=ADMIN)
        self.assertFalse(await admin._send_profile(nf, self.db, self.cfg, "@nobody"))
        self.assertIn("не найден", nf.last()[1])

    async def test_pending_and_done_scheduled_posts(self):
        pid = await self.db.add_post(USER_A, 1, 2, "text", None, [-500], int(time.time()) + 999)
        await self.db.add_post(USER_A, 1, 3, "text", None, [-500], 10)
        (due,) = await self.db.claim_due_posts(100)
        await self.db.finish_post(due["id"], "done")
        ov = await self.db.user_overview(USER_A, "2000-01-01")
        self.assertEqual((ov["pending"], ov["scheduled_done"]), (1, 1))
        self.assertTrue(pid)

    async def test_list_and_pagination(self):
        for i in range(10):
            await self.db.touch_user(1000 + i, f"u{i}", f"U{i}", "UTC")
        cb = FakeCb("ad:list:0", FakeMsg(), user_id=ADMIN)
        await admin.cb_list(cb, self.db, self.cfg)
        d = datas(cb.message.last()[2])
        self.assertEqual(len([x for x in d if x.startswith("adu:")]), admin.PAGE)
        self.assertIn("ad:list:1", d)
        cb2 = FakeCb("ad:list:1", FakeMsg(), user_id=ADMIN)
        await admin.cb_list(cb2, self.db, self.cfg)
        self.assertIn("ad:list:0", datas(cb2.message.last()[2]))

    async def test_grant_and_revoke_pro(self):
        cb = FakeCb(f"adp:{USER_A}:30", FakeMsg(), user_id=ADMIN)
        await admin.cb_pro(cb, self.db, self.cfg)
        self.assertGreater((await self.db.get_user(USER_A))["pro_until"], time.time())
        self.assertIn(f"adp:{USER_A}:0", datas(cb.message.last()[2]))
        await admin.cb_pro(FakeCb(f"adp:{USER_A}:0", FakeMsg(), user_id=ADMIN), self.db, self.cfg)
        self.assertEqual((await self.db.get_user(USER_A))["pro_until"], 0)

    async def test_find_flow_keeps_state_until_found(self):
        await admin.cb_find(FakeCb("ad:find", user_id=ADMIN), self.state)
        self.assertIs(self.state.state, admin.AdminFind.query)
        await admin.got_query(FakeMsg(text="@nobody", user_id=ADMIN), self.state, self.db, self.cfg)
        self.assertIs(self.state.state, admin.AdminFind.query)
        await admin.got_query(FakeMsg(text="@alice_x", user_id=ADMIN), self.state, self.db, self.cfg)
        self.assertIsNone(self.state.state)


class ActivityTests(AdminBase):
    async def test_middleware_records_user_and_activity(self):
        seen = []

        async def handler(event, data):
            seen.append(event)
            return "ok"

        user = SimpleNamespace(id=777, username="Bob", full_name="Боб", is_bot=False)
        res = await middleware.activity_middleware(handler, SimpleNamespace(from_user=user),
                                                   {"db": self.db, "cfg": self.cfg})
        self.assertEqual(res, "ok")
        row = await self.db.get_user(777)
        self.assertEqual((row["username"], row["full_name"]), ("Bob", "Боб"))
        self.assertGreater(row["last_seen"], time.time() - 5)
        # переименование и повторный заход обновляют данные, а не дублируют пользователя
        user.username = "@Bobby"
        await middleware.activity_middleware(handler, SimpleNamespace(from_user=user), {"db": self.db, "cfg": self.cfg})
        self.assertEqual((await self.db.get_user(777))["username"], "Bobby")

    async def test_middleware_survives_db_error_and_ignores_bots(self):
        async def handler(event, data):
            return "ok"

        class Broken:
            async def touch_user(self, *a, **k):
                raise RuntimeError("db down")

        user = SimpleNamespace(id=1, username="x", full_name="x", is_bot=False)
        self.assertEqual(await middleware.activity_middleware(
            handler, SimpleNamespace(from_user=user), {"db": Broken(), "cfg": self.cfg}), "ok")
        bot = SimpleNamespace(id=2, username="b", full_name="b", is_bot=True)
        await middleware.activity_middleware(handler, SimpleNamespace(from_user=bot), {"db": self.db, "cfg": self.cfg})
        self.assertIsNone(await self.db.get_user(2))

    async def test_old_users_table_gets_new_columns(self):
        import os, sqlite3, tempfile
        path = os.path.join(tempfile.mkdtemp(), "old.sqlite3")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, tz TEXT NOT NULL, pro_until INTEGER NOT NULL "
                  "DEFAULT 0, rewrite_day TEXT NOT NULL DEFAULT '', rewrite_count INTEGER NOT NULL DEFAULT 0, "
                  "limit_notified_day TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL)")
        c.execute("INSERT INTO users(user_id,tz,created_at) VALUES (9,'UTC',1)")
        c.commit()
        c.close()
        db = Database(path)
        await db.connect()
        try:
            u = await db.get_user(9)
            self.assertEqual((u["username"], u["last_seen"], u["posts_manual"], u["posts_feed"]), ("", 0, 0, 0))
        finally:
            await db.close()


class Cmd:
    def __init__(self, args):
        self.args = args


class AccessAndBanTests(AdminBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cfg = Config(bot_token="x", admin_ids=(ADMIN,))

    async def test_feeds_access_rules(self):
        db, cfg = self.db, self.cfg
        self.assertFalse(await access.feeds_allowed(db, cfg, USER_A))  # ограничение включено, доступа нет
        self.assertTrue(await access.feeds_allowed(db, cfg, ADMIN))
        await db.set_feeds_access(USER_A, True)
        self.assertTrue(await access.feeds_allowed(db, cfg, USER_A))
        await db.set_feeds_access(USER_A, False)
        self.assertFalse(await access.feeds_allowed(db, cfg, USER_A))
        # без ADMIN_IDS и FEEDS_ADMINS ограничений нет
        self.assertTrue(await access.feeds_allowed(db, Config(bot_token="x"), USER_A))
        self.assertTrue(await access.feeds_allowed(db, Config(bot_token="x", feeds_admins=(USER_A,)), USER_A))
        # id из FEEDS_ADMINS остаётся допущенным и при включённом ограничении через ADMIN_IDS
        both = Config(bot_token="x", admin_ids=(ADMIN,), feeds_admins=(USER_A,))
        self.assertTrue(await access.feeds_allowed(db, both, USER_A))

    async def test_grant_revoke_commands_by_username_and_id_with_notice(self):
        bot = FlowBotLike()
        m = FakeMsg(user_id=ADMIN)
        await admin.cmd_grant(m, Cmd("@alice_x"), bot, self.db, self.cfg)
        self.assertTrue((await self.db.get_user(USER_A))["feeds_access"])
        self.assertEqual(bot.sent[-1][0], USER_A)
        self.assertIn("открыт доступ", bot.sent[-1][1])
        await admin.cmd_revoke(m, Cmd(str(USER_A)), bot, self.db, self.cfg)
        self.assertFalse((await self.db.get_user(USER_A))["feeds_access"])
        # по числовому id можно выдать доступ тому, кто ещё не писал боту
        await admin.cmd_grant(m, Cmd("424242"), bot, self.db, self.cfg)
        self.assertTrue((await self.db.get_user(424242))["feeds_access"])
        # неизвестный username и пустая команда
        before = len(m.out)
        await admin.cmd_grant(m, Cmd("@ghost"), bot, self.db, self.cfg)
        self.assertIn("не писал боту", m.last()[1])
        await admin.cmd_grant(m, Cmd(None), bot, self.db, self.cfg)
        self.assertIn("Использование", m.last()[1])
        self.assertEqual(len(m.out), before + 2)

    async def test_ban_unban_and_lists(self):
        m = FakeMsg(user_id=ADMIN)
        await admin.cmd_ban(m, Cmd("@alice_x спам в каналах"), self.db, self.cfg)
        u = await self.db.get_user(USER_A)
        self.assertEqual((u["banned"], u["ban_reason"]), (1, "спам в каналах"))
        await admin.cmd_banned(m, self.db)
        self.assertIn("Alice_X", m.last()[1])
        self.assertIn("спам в каналах", m.last()[1])
        await admin.cmd_unban(m, Cmd(str(USER_A)), self.db, self.cfg)
        u = await self.db.get_user(USER_A)
        self.assertEqual((u["banned"], u["ban_reason"]), (0, ""))
        await admin.cmd_banned(m, self.db)
        self.assertEqual(m.last()[1], "Забаненных нет.")

    async def test_admin_cannot_be_banned(self):
        await self.db.touch_user(ADMIN, "boss", "Boss", "UTC")
        m = FakeMsg(user_id=ADMIN)
        await admin.cmd_ban(m, Cmd(str(ADMIN)), self.db, self.cfg)
        self.assertIn("Нельзя забанить администратора", m.last()[1])
        self.assertEqual((await self.db.get_user(ADMIN))["banned"], 0)

    async def test_profile_buttons_toggle_access_and_ban(self):
        bot = FlowBotLike()
        cb = FakeCb(f"adf:{USER_A}:1", FakeMsg(), user_id=ADMIN)
        await admin.cb_access(cb, bot, self.db, self.cfg)
        self.assertIn("Нейропостинг: доступ открыт", cb.message.last()[1])
        self.assertIn(f"adf:{USER_A}:0", datas(cb.message.last()[2]))
        cb = FakeCb(f"adb:{USER_A}:1", FakeMsg(), user_id=ADMIN)
        await admin.cb_ban(cb, self.db, self.cfg)
        self.assertIn("🚫 ЗАБАНЕН", cb.message.last()[1])
        self.assertIn(f"adb:{USER_A}:0", datas(cb.message.last()[2]))
        await admin.cb_ban(FakeCb(f"adb:{USER_A}:0", FakeMsg(), user_id=ADMIN), self.db, self.cfg)
        self.assertEqual((await self.db.get_user(USER_A))["banned"], 0)

    async def test_middleware_blocks_banned_but_not_admin(self):
        await self.db.set_banned(USER_A, True, "нарушение")
        called = []

        async def handler(event, data):
            called.append(event)
            return "ok"

        msg = FakeMsg(user_id=USER_A)
        msg.from_user = SimpleNamespace(id=USER_A, username="Alice_X", full_name="A", is_bot=False)
        self.assertIsNone(await middleware.activity_middleware(handler, msg, {"db": self.db, "cfg": self.cfg}))
        self.assertEqual(called, [])
        self.assertIn("Доступ к боту закрыт", msg.last()[1])
        self.assertIn("нарушение", msg.last()[1])
        cb = FakeCb("x", FakeMsg(), user_id=USER_A)
        cb.from_user = SimpleNamespace(id=USER_A, username="Alice_X", full_name="A", is_bot=False)
        await middleware.activity_middleware(handler, cb, {"db": self.db, "cfg": self.cfg})
        self.assertEqual(cb.answers[-1][1], True)
        self.assertEqual(called, [])
        # разбанили — пускает; админ проходит даже с флагом
        await self.db.set_banned(USER_A, False)
        msg2 = FakeMsg(user_id=USER_A)
        msg2.from_user = SimpleNamespace(id=USER_A, username="Alice_X", full_name="A", is_bot=False)
        self.assertEqual(await middleware.activity_middleware(handler, msg2, {"db": self.db, "cfg": self.cfg}), "ok")
        await self.db.touch_user(ADMIN, "boss", "B", "UTC")
        await self.db.set_banned(ADMIN, True)
        adm = FakeMsg(user_id=ADMIN)
        adm.from_user = SimpleNamespace(id=ADMIN, username="boss", full_name="B", is_bot=False)
        self.assertEqual(await middleware.activity_middleware(handler, adm, {"db": self.db, "cfg": self.cfg}), "ok")

    async def test_banned_scheduled_posts_and_feeds_are_skipped(self):
        await self.db.add_post(USER_A, 1, 2, "text", None, [-500], 10)
        await self.db.set_banned(USER_A, True)
        self.assertEqual(await self.db.claim_due_posts(100), [])
        self.assertEqual(await self.db.list_active_feeds(), [])
        await self.db.set_banned(USER_A, False)
        self.assertEqual(len(await self.db.claim_due_posts(100)), 1)
        self.assertEqual(len(await self.db.list_active_feeds()), 1)

    async def test_engine_skips_owners_without_access(self):
        seen = []

        async def fake(bot, db, reader, feed, rewriter, cfg):
            seen.append(feed["owner_id"])
            return 0
        orig = feed_engine.process_feed
        feed_engine.process_feed = fake
        try:
            await feed_engine.run_feeds_once(None, self.db, self.cfg, object(), rewriter=object(), pause=0)
            self.assertEqual(seen, [])  # у Алисы нет доступа
            await self.db.set_feeds_access(USER_A, True)
            await feed_engine.run_feeds_once(None, self.db, self.cfg, object(), rewriter=object(), pause=0)
            self.assertEqual(seen, [USER_A])
        finally:
            feed_engine.process_feed = orig


class FlowBotLike:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


if __name__ == "__main__":
    unittest.main()