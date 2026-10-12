"""Чистка чужих ссылок, своя подпись с гиперссылками, настройки в /feeds."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from tests.test_handlers_flow import Base, FakeCb, FakeMsg, FakeState, datas  # первым: заглушка aiogram

from bot import feed_engine, publisher  # noqa: E402
from bot.config import Config  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.entities import Entity, from_bot, strip_links, u16len  # noqa: E402
from bot.feeds import FetchedPost, compose_post  # noqa: E402
from bot.handlers import feeds as feeds_h  # noqa: E402
from tests.test_feeds import FakeReader, MediaBot  # noqa: E402

USER = 1


def link(offset, length, url="https://x.io"):
    return Entity("text_link", offset, length, url=url)


class StripLinksTests(unittest.TestCase):
    def test_plain_text_untouched(self):
        self.assertEqual(strip_links("Просто текст #хештег", [Entity("bold", 0, 6)]),
                         ("Просто текст #хештег", [Entity("bold", 0, 6)]))

    def test_urls_and_mentions_removed_from_long_line(self):
        text, ents = strip_links("Мы запустили новый сервис для всех желающих https://site.ru/a и @pilot_chan рады", [])
        self.assertNotIn("http", text)
        self.assertNotIn("@pilot_chan", text)
        self.assertIn("Мы запустили новый сервис", text)

    def test_ad_line_removed_whole(self):
        text, _ = strip_links("Важная новость про курс валют на сегодня\n\nПодписаться: @newschannel", [])
        self.assertEqual(text, "Важная новость про курс валют на сегодня")

    def test_source_line_with_tme_link_removed(self):
        text, _ = strip_links("Текст поста тут\nИсточник: t.me/somechan/123", [])
        self.assertEqual(text, "Текст поста тут")

    def test_lead_in_allows_longer_ad_line(self):
        text, _ = strip_links("Новость\n\nПодписывайся на наш канал 👉 @abcde", [])
        self.assertEqual(text, "Новость")

    def test_short_headline_with_link_is_kept_as_text(self):
        text, _ = strip_links("Курс вырос на пять процентов за сутки, пишут аналитики https://rbc.ru/1", [])
        self.assertTrue(text.startswith("Курс вырос на пять процентов"))
        self.assertNotIn("rbc.ru", text)

    def test_hidden_hyperlink_loses_link_but_keeps_text_in_long_sentence(self):
        text, ents = strip_links("Согласно новому докладу аналитиков рынок растёт", [link(9, 5)])
        self.assertEqual(text, "Согласно новому докладу аналитиков рынок растёт")
        self.assertEqual(ents, [])

    def test_hidden_link_only_line_removed(self):
        text, ents = strip_links("Новость дня сегодня и вчера\nЧитать далее", [link(28, 13)])
        self.assertEqual(text, "Новость дня сегодня и вчера")
        self.assertEqual(ents, [])

    def test_email_is_not_a_mention(self):
        text, _ = strip_links("Пишите на ivan@example.com по любым вопросам нашей компании", [])
        self.assertIn("ivan@example.com", text)

    def test_formatting_offsets_shift_after_removal(self):
        # «Реклама: @chan» удаляется целиком, жирный фрагмент второй строки должен сдвинуться
        text, ents = strip_links("Реклама: @chan\nЖирный текст остаётся", [Entity("bold", 15, 6)])
        self.assertEqual(text, "Жирный текст остаётся")
        self.assertEqual(ents, [Entity("bold", 0, 6)])

    def test_offsets_with_emoji_utf16(self):
        src = "😀 Привет всем https://a.io тут много слов для длины"
        text, _ = strip_links(src, [Entity("bold", u16len("😀 "), 6)])
        self.assertNotIn("https", text)
        _, ents = strip_links(src, [Entity("bold", u16len("😀 "), 6)])
        self.assertEqual(ents, [Entity("bold", u16len("😀 "), 6)])

    def test_custom_emoji_survives(self):
        text, ents = strip_links("😀 Привет, это длинное сообщение без ссылок вообще", [Entity("custom_emoji", 0, 2, custom_emoji_id="9")])
        self.assertEqual(ents, [Entity("custom_emoji", 0, 2, custom_emoji_id="9")])

    def test_only_links_leaves_empty_text(self):
        self.assertEqual(strip_links("https://a.io\n@promo_chan", [])[0], "")

    def test_no_triple_newlines_left(self):
        text, _ = strip_links("Первый абзац новости\n\nПодписаться: @abcde\n\nВторой абзац новости", [])
        self.assertNotIn("\n\n\n", text)


class FooterComposeTests(unittest.TestCase):
    def test_footer_after_body_and_credit_with_shifted_entities(self):
        foot_ents = [Entity("text_link", 0, 4, url="https://my.site")]
        text, ents = compose_post("Тело", [Entity("bold", 0, 4)], "chan", True, footer="Сайт", footer_entities=foot_ents)
        self.assertEqual(text, "Тело\n\nИсточник: @chan\n\nСайт")
        self.assertEqual(ents[0], Entity("bold", 0, 4))
        self.assertEqual(ents[1].offset, u16len("Тело\n\nИсточник: @chan\n\n"))
        self.assertEqual(ents[1].url, "https://my.site")

    def test_footer_only_without_credit(self):
        text, ents = compose_post("Тело", [], "chan", False, footer="Подпись 😀",
                                  footer_entities=[Entity("custom_emoji", 8, 2, custom_emoji_id="1")])
        self.assertEqual(text, "Тело\n\nПодпись 😀")
        self.assertEqual(ents[0].offset, u16len("Тело\n\nПодпись "))

    def test_long_body_truncated_but_footer_kept(self):
        text, ents = compose_post("я" * 500, [], "chan", True, limit=100, footer="Моя подпись",
                                  footer_entities=[Entity("bold", 0, 3)])
        self.assertEqual(len(text), 100)
        self.assertTrue(text.endswith("Источник: @chan\n\nМоя подпись"))
        self.assertEqual(ents[0].offset, u16len(text) - len("Моя подпись"))

    def test_no_footer_changes_nothing(self):
        self.assertEqual(compose_post("Тело", [], "chan", True), ("Тело\n\nИсточник: @chan", []))

    def test_footer_without_body(self):
        text, _ = compose_post("", [], "chan", False, footer="Подпись")
        self.assertEqual(text, "Подпись")


class FromBotTests(unittest.TestCase):
    def test_conversion_keeps_links_and_emoji_drops_auto_entities(self):
        src = [SimpleNamespace(type="text_link", offset=0, length=4, url="https://a.io", language=None, custom_emoji_id=None),
               SimpleNamespace(type="url", offset=5, length=3, url=None, language=None, custom_emoji_id=None),
               SimpleNamespace(type="custom_emoji", offset=9, length=2, url=None, language=None, custom_emoji_id="7")]
        self.assertEqual(from_bot(src), [Entity("text_link", 0, 4, url="https://a.io"),
                                         Entity("custom_emoji", 9, 2, custom_emoji_id="7")])
        self.assertEqual(from_bot(None), [])


class PrefsDbTests(unittest.IsolatedAsyncioTestCase):
    async def test_defaults_and_roundtrip(self):
        db = Database(":memory:")
        await db.connect()
        self.assertEqual(await db.get_feed_prefs(1), {"strip_links": True, "footer": "", "footer_entities": []})
        self.assertFalse(await db.toggle_strip_links(1))
        self.assertTrue(await db.toggle_strip_links(1))
        await db.set_footer(1, "Сайт", [{"type": "text_link", "offset": 0, "length": 4, "url": "u"}])
        prefs = await db.get_feed_prefs(1)
        self.assertEqual((prefs["footer"], prefs["footer_entities"][0]["url"], prefs["strip_links"]), ("Сайт", "u", True))
        self.assertEqual((await db.get_feed_prefs(2))["footer"], "")  # настройки у каждого свои
        await db.close()


class EnginePrefsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        publisher.recent_sends._items.clear()
        from bot import mirror_engine
        mirror_engine._last_notice.clear()
        self.db = Database(":memory:")
        await self.db.connect()
        await self.db.ensure_user(USER, "Europe/Moscow")
        self.seen = []

        async def rewriter(text, style):
            self.seen.append(text)
            return "Переписано нейросетью. Читайте на https://leak.example и @leak_chan"

        self.rewriter = rewriter

    async def asyncTearDown(self):
        await self.db.close()

    async def run_once(self, post, rewrite=True, credit=False):
        await self.db.add_feed(USER, "chan", "К", 0, [-100], rewrite=rewrite, credit=credit)
        bot = MediaBot()
        await feed_engine.run_feeds_once(bot, self.db, Config(bot_token="x"), FakeReader([post]), self.rewriter, pause=0)
        return bot

    async def test_links_stripped_before_and_after_ai_and_footer_added(self):
        await self.db.set_footer(USER, "Наш сайт", [{"type": "text_link", "offset": 4, "length": 4, "url": "https://my.site"}])
        post = FetchedPost([1], "Важная новость сегодня подробно\nПодписаться: @src_chan", [], [])
        bot = await self.run_once(post)
        self.assertNotIn("@src_chan", self.seen[0])  # в ИИ ушёл уже очищенный текст
        kw = bot.calls[0][1]
        self.assertNotIn("leak", kw["text"])  # ссылки, дописанные моделью, тоже убраны
        self.assertTrue(kw["text"].endswith("\n\nНаш сайт"))
        link_e = [e for e in kw["entities"] if e.type == "text_link"][0]
        self.assertEqual(link_e.url, "https://my.site")
        self.assertEqual(link_e.offset, u16len(kw["text"]) - len("Наш сайт") + 4)

    async def test_strip_off_keeps_source_links(self):
        await self.db.toggle_strip_links(USER)
        post = FetchedPost([1], "Новость https://src.example/a про многое сегодня и завтра", [], [])
        bot = await self.run_once(post, rewrite=False)
        self.assertIn("https://src.example/a", bot.calls[0][1]["text"])

    async def test_post_with_only_links_is_skipped_but_cursor_moves(self):
        post = FetchedPost([5], "https://a.io\n@promo_chan", [], [], )
        bot = await self.run_once(post, rewrite=False)
        self.assertEqual(bot.calls, [])
        self.assertEqual((await self.db.list_feeds(USER))[0]["last_msg_id"], 5)

    async def test_footer_added_without_ai(self):
        await self.db.set_footer(USER, "Подпись", [])
        bot = await self.run_once(FetchedPost([1], "Просто пост без ссылок", [], []), rewrite=False)
        self.assertEqual(bot.calls[0][1]["text"], "Просто пост без ссылок\n\nПодпись")


class FooterHandlerTests(Base):
    async def test_view_has_prefs_buttons(self):
        text, kb = feeds_h.feeds_view([], {}, await self.db.get_feed_prefs(USER))
        self.assertIn("fp:strip", datas(kb))
        self.assertIn("fp:footer", datas(kb))
        self.assertNotIn("fp:clear", datas(kb))
        self.assertIn("убираются", text)

    async def test_toggle_strip_button(self):
        cb = FakeCb("fp:strip")
        await feeds_h.toggle_strip(cb, self.db)
        self.assertFalse((await self.db.get_feed_prefs(USER))["strip_links"])
        self.assertIn("остаются", cb.message.last()[1])

    async def test_footer_flow_saves_text_entities_and_previews(self):
        state = FakeState()
        await feeds_h.footer_start(FakeCb("fp:footer"), state)
        self.assertIs(state.state, feeds_h.FeedFooter.waiting)
        msg = FakeMsg(text="Мой сайт 😀", user_id=USER)
        msg.entities = [SimpleNamespace(type="text_link", offset=4, length=4, url="https://my.site", language=None, custom_emoji_id=None),
                        SimpleNamespace(type="custom_emoji", offset=9, length=2, url=None, language=None, custom_emoji_id="55")]
        msg.caption = msg.caption_entities = None
        await feeds_h.footer_received(msg, state, self.db)
        prefs = await self.db.get_feed_prefs(USER)
        self.assertEqual(prefs["footer"], "Мой сайт 😀")
        self.assertEqual([(e["type"], e["url"], e["custom_emoji_id"]) for e in prefs["footer_entities"]],
                         [("text_link", "https://my.site", None), ("custom_emoji", None, "55")])
        self.assertIsNone(state.state)

    async def test_too_long_or_empty_footer_rejected(self):
        state = FakeState()
        for text in ("я" * (feeds_h.FOOTER_MAX + 1), ""):
            msg = FakeMsg(text=text, user_id=USER)
            msg.entities = msg.caption = msg.caption_entities = None
            await feeds_h.footer_received(msg, state, self.db)
        self.assertEqual((await self.db.get_feed_prefs(USER))["footer"], "")

    async def test_clear_footer(self):
        await self.db.set_footer(USER, "Подпись", [])
        await feeds_h.clear_footer(FakeCb("fp:clear"), self.db)
        self.assertEqual((await self.db.get_feed_prefs(USER))["footer"], "")


if __name__ == "__main__":
    unittest.main()


class HangTests(EnginePrefsTests):
    async def test_hanging_source_times_out_and_other_feeds_still_processed(self):
        import asyncio

        class Hang(FakeReader):
            async def fetch_new(self, username, after_id):
                if username == "slow":
                    await asyncio.sleep(10)
                return await super().fetch_new(username, after_id)

        await self.db.add_feed(USER, "slow", "С", 0, [-100], rewrite=False, credit=False)
        await self.db.add_feed(USER, "fast", "Б", 0, [-100], rewrite=False, credit=False)
        bot = MediaBot()
        old = feed_engine.FETCH_TIMEOUT
        feed_engine.FETCH_TIMEOUT = 0.05
        try:
            with self.assertLogs("bot.feed_engine", "WARNING") as logs:
                n = await feed_engine.run_feeds_once(
                    bot, self.db, Config(bot_token="x"), Hang([FetchedPost([1], "Пост", [], [])]), self.rewriter, pause=0)
        finally:
            feed_engine.FETCH_TIMEOUT = old
        self.assertEqual(n, 1)
        self.assertTrue(any("TimeoutError" in m for m in logs.output))

    async def test_cycle_logs_active_source_count(self):
        with self.assertLogs("bot.feed_engine", "INFO") as logs:
            await feed_engine.run_feeds_once(MediaBot(), self.db, Config(bot_token="x"), FakeReader([]), self.rewriter, pause=0)
        self.assertTrue(any("активных: 0" in m for m in logs.output))


class RewriteFailureNoticeTests(EnginePrefsTests):
    async def test_failed_rewrite_tells_owner_once_and_publishes_original(self):
        async def broken(text, style):
            raise RuntimeError("Модель не уложилась в лимит длины ответа")

        self.rewriter = broken
        await self.db.add_feed(USER, "chan", "К", 0, [-100], rewrite=True, credit=False)
        posts = [FetchedPost([1], "Первый пост без ссылок", [], []), FetchedPost([2], "Второй пост без ссылок", [], [])]
        bot = MediaBot()
        await feed_engine.run_feeds_once(bot, self.db, Config(bot_token="x"), FakeReader(posts), broken, pause=0)
        sent = [kw for n, kw in bot.calls if kw["chat_id"] == -100]
        self.assertEqual([kw["text"] for kw in sent], ["Первый пост без ссылок", "Второй пост без ссылок"])
        dms = [kw for n, kw in bot.calls if kw["chat_id"] == USER]
        self.assertEqual(len(dms), 1)  # не спамим по каждому посту
        self.assertIn("лимит длины", dms[0]["text"])
        from bot.db import today_str
        self.assertEqual(await self.db.rewrites_used(USER, today_str()), 0)  # квота возвращена