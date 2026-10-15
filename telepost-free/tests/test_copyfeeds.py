"""Админское копирование: фото и текст как есть, из нескольких фото/видео последнее отбрасывается."""
from __future__ import annotations

import unittest

from tests import aiogram_stub

aiogram_stub.install_if_missing()

from bot import feed_engine, mirror_engine, publisher  # noqa: E402
from bot.config import Config  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.entities import Entity  # noqa: E402
from bot.feeds import FetchedPost, MediaItem, SourceInfo, drop_last_visual  # noqa: E402
from bot.handlers import copyfeeds  # noqa: E402
from tests.test_feeds import FakeReader, MediaBot  # noqa: E402
from tests.test_handlers_flow import FakeCb, FakeMsg, FakeState  # noqa: E402

ADMIN = 1


def photo(n):
    return MediaItem("photo", f"p{n}".encode(), f"{n}.jpg")


class HelperTests(unittest.TestCase):
    def test_drop_last_visual(self):
        self.assertEqual([m.filename for m in drop_last_visual([photo(1), photo(2), photo(3)])], ["1.jpg", "2.jpg"])
        self.assertEqual(len(drop_last_visual([photo(1)])), 1)  # одно медиа остаётся
        self.assertEqual(drop_last_visual([]), [])
        doc = MediaItem("document", b"d", "d.txt")
        out = drop_last_visual([photo(1), doc, MediaItem("video", b"v", "v.mp4")])
        self.assertEqual([m.kind for m in out], ["photo", "document"])  # документ не считается и не отбрасывается


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        publisher.recent_sends._items.clear()
        mirror_engine._last_notice.clear()
        self.cfg = Config(bot_token="x", admin_ids=(ADMIN,))
        self.db = Database(":memory:")
        await self.db.connect()
        await self.db.ensure_user(ADMIN, "UTC")
        await self.db.set_footer(ADMIN, "ПОДПИСЬ", [])
        self.rewrites = []

        async def rewriter(text, style):
            self.rewrites.append(text)
            return "ПЕРЕПИСАНО"

        self.rewriter = rewriter

    async def asyncTearDown(self):
        await self.db.close()

    async def run_once(self, post):
        bot = MediaBot()
        await feed_engine.run_feeds_once(bot, self.db, self.cfg, FakeReader([post]), self.rewriter, pause=0)
        return bot

    async def test_three_photos_become_two_with_original_text(self):
        await self.db.add_feed(ADMIN, "src", "Источник", 0, [-100], rewrite=False, credit=False, kind="copy")
        ents = [Entity("bold", 0, 5)]
        bot = await self.run_once(FetchedPost([1, 2, 3], "Жирный текст https://x.io", [photo(1), photo(2), photo(3)],
                                              ents))
        name, kw = bot.calls[0]
        self.assertEqual(name, "group")
        self.assertEqual([m.media.data for m in kw["media"]], [b"p1", b"p2"])  # третье отброшено
        self.assertEqual(kw["media"][0].caption, "Жирный текст https://x.io")  # без подписи, ссылки и ИИ
        self.assertEqual(self.rewrites, [])

    async def test_single_photo_kept(self):
        await self.db.add_feed(ADMIN, "src", "Источник", 0, [-100], rewrite=False, credit=False, kind="copy")
        bot = await self.run_once(FetchedPost([1], "Текст", [photo(1)]))
        self.assertEqual(bot.calls[0][0], "photo")
        self.assertEqual(bot.calls[0][1]["photo"].data, b"p1")

    async def test_regular_feed_untouched(self):
        await self.db.add_feed(ADMIN, "src", "Источник", 0, [-100], rewrite=False, credit=False)
        bot = await self.run_once(FetchedPost([1, 2], "Текст", [photo(1), photo(2)]))
        self.assertEqual(len(bot.calls[0][1]["media"]), 2)  # обычный нейропостинг ничего не отбрасывает

    async def test_copy_feeds_hidden_from_regular_list(self):
        await self.db.add_feed(ADMIN, "src", "Источник", 0, [-100], rewrite=False, kind="copy")
        await self.db.add_feed(ADMIN, "other", "Другой", 0, [-100])
        self.assertEqual([f["username"] for f in await self.db.list_feeds(ADMIN)], ["other"])
        self.assertEqual([f["username"] for f in await self.db.list_feeds(ADMIN, "copy")], ["src"])
        self.assertEqual(await self.db.count_feeds(ADMIN), 1)
        self.assertEqual(len(await self.db.list_active_feeds()), 2)  # движок видит оба вида


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(":memory:")
        await self.db.connect()
        await self.db.ensure_user(ADMIN, "UTC")
        await self.db.add_channel(ADMIN, -100, "Первый", "")
        await self.db.add_channel(ADMIN, -200, "Второй", "")
        self.reader = FakeReader(latest=42)

    async def asyncTearDown(self):
        await self.db.close()

    def test_only_admins(self):
        cfg = Config(bot_token="x", admin_ids=(ADMIN,))
        self.assertTrue(copyfeeds.is_admin(FakeMsg(user_id=ADMIN), cfg))
        self.assertFalse(copyfeeds.is_admin(FakeMsg(user_id=2), cfg))
        self.assertFalse(copyfeeds.is_admin(FakeMsg(user_id=ADMIN), Config(bot_token="x")))  # админы не заданы

    async def test_add_flow_and_manage(self):
        state = FakeState()
        await copyfeeds.new_start(FakeCb("cp:new", user_id=ADMIN), state, self.db, self.reader)
        self.assertIs(state.state, copyfeeds.CopyNew.username)
        await copyfeeds.got_username(FakeMsg(user_id=ADMIN, text="@chan"), state, self.db, self.reader)
        self.assertIs(state.state, copyfeeds.CopyNew.targets)
        done = FakeCb("cp:nd", user_id=ADMIN)
        await copyfeeds.new_targets(done, state, self.db, self.reader)
        self.assertTrue(done.answers[0][1])  # каналы не выбраны
        await copyfeeds.new_targets(FakeCb("cp:na", user_id=ADMIN), state, self.db, self.reader)
        await copyfeeds.new_targets(FakeCb("cp:nd", user_id=ADMIN), state, self.db, self.reader)
        feed = (await self.db.list_feeds(ADMIN, "copy"))[0]
        self.assertEqual((feed["username"], feed["last_msg_id"], feed["rewrite"], feed["credit"], feed["targets"]),
                         ("chan", 42, False, False, [-200, -100]))
        fid = feed["id"]
        await copyfeeds.cb_target_change(FakeCb(f"cp:tt:{fid}:-100:0", user_id=ADMIN), self.db)
        self.assertEqual((await self.db.get_feed(ADMIN, fid, "copy"))["targets"], [-200])
        await copyfeeds.cb_toggle(FakeCb(f"cp:act:{fid}", user_id=ADMIN), self.db)
        self.assertFalse((await self.db.get_feed(ADMIN, fid, "copy"))["active"])
        await copyfeeds.cb_delete(FakeCb(f"cp:delok:{fid}", user_id=ADMIN), self.db)
        self.assertEqual(await self.db.list_feeds(ADMIN, "copy"), [])

    async def test_duplicate_source_rejected(self):
        await self.db.add_feed(ADMIN, "chan", "Канал", 0, [-100])  # уже есть в обычном нейропостинге
        state = FakeState()
        msg = FakeMsg(user_id=ADMIN, text="@chan")
        await copyfeeds.got_username(msg, state, self.db, self.reader)
        self.assertIn("уже добавлен", msg.out[-1][1])

    def test_views(self):
        text, markup = copyfeeds.main_view([], {})
        self.assertIn("последнее отбрасывается", text)
        self.assertEqual(SourceInfo(1, "a", "b").chat_id, 1)


if __name__ == "__main__":
    unittest.main()