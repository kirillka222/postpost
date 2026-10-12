"""Форматирование и премиум-эмодзи: сущности, перенос после рерайта, публикация ботом и аккаунтом."""
from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace

from tests import aiogram_stub

aiogram_stub.install_if_missing()

from bot import entities as E  # noqa: E402
from bot import feed_engine, publisher  # noqa: E402
from bot.config import Config  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.entities import Entity  # noqa: E402
from bot.feeds import FetchedPost, MediaItem, compose_post  # noqa: E402
from bot.telethon_reader import TelethonReader  # noqa: E402
from tests.test_core import FakeBot  # noqa: E402
from tests.test_feeds import FakeReader, FakeTelethon, MediaBot, tmsg  # noqa: E402

USER = 1


def custom(offset, length, eid):
    return Entity("custom_emoji", offset, length, custom_emoji_id=eid)


# ---------- классы, имитирующие Telethon (определяем по имени класса) ----------
def _tl(name, **kw):
    cls = type(name, (), {})
    obj = cls()
    obj.__dict__.update(kw)
    return obj


class EntitiesTests(unittest.TestCase):
    def test_u16len(self):
        self.assertEqual((E.u16len("abc"), E.u16len("😀"), E.u16len("а😀б")), (3, 2, 4))

    def test_from_telethon(self):
        ents = [
            _tl("MessageEntityBold", offset=0, length=3),
            _tl("MessageEntityCustomEmoji", offset=4, length=2, document_id=5368324170671202286),
            _tl("MessageEntityTextUrl", offset=7, length=4, url="https://x.ru"),
            _tl("MessageEntityPre", offset=12, length=5, language="python"),
            _tl("MessageEntityBlockquote", offset=18, length=3, collapsed=True),
            _tl("MessageEntityUrl", offset=30, length=8),      # авто-сущность: не переносим
            _tl("MessageEntityHashtag", offset=40, length=4),  # авто-сущность: не переносим
        ]
        got = E.from_telethon(ents)
        self.assertEqual([e.type for e in got],
                         ["bold", "custom_emoji", "text_link", "pre", "expandable_blockquote"])
        self.assertEqual(got[1].custom_emoji_id, "5368324170671202286")
        self.assertEqual((got[2].url, got[3].language), ("https://x.ru", "python"))
        self.assertEqual(E.from_telethon(None), [])

    def test_to_telethon_roundtrip(self):
        made = []

        def factory(name):
            def make(**kw):
                made.append((name, kw))
                return (name, kw)
            return make

        fake_types = SimpleNamespace(**{n: factory(n) for n in [
            "MessageEntityBold", "MessageEntityItalic", "MessageEntityUnderline", "MessageEntityStrike",
            "MessageEntitySpoiler", "MessageEntityCode", "MessageEntityPre", "MessageEntityTextUrl",
            "MessageEntityBlockquote", "MessageEntityCustomEmoji"]})
        src = [Entity("bold", 0, 2), Entity("text_link", 3, 2, url="https://a.b"),
               Entity("pre", 6, 2, language="go"), custom(9, 2, "123"),
               Entity("expandable_blockquote", 12, 4), Entity("strikethrough", 17, 1)]
        out = E.to_telethon(src, fake_types)
        self.assertEqual([o[0] for o in out], ["MessageEntityBold", "MessageEntityTextUrl", "MessageEntityPre",
                                               "MessageEntityCustomEmoji", "MessageEntityBlockquote",
                                               "MessageEntityStrike"])
        self.assertEqual(out[3][1], {"document_id": 123, "offset": 9, "length": 2})  # id снова числом
        self.assertEqual(out[4][1], {"collapsed": True, "offset": 12, "length": 4})
        self.assertEqual(out[1][1]["url"], "https://a.b")

    def test_strip_shifts_entities_in_utf16(self):
        text = "  😀 Привет  "
        ents = [custom(2, 2, "1"), Entity("bold", 5, 6)]
        body, got = E.strip_text(text, ents)
        self.assertEqual(body, "😀 Привет")
        self.assertEqual(got, [custom(0, 2, "1"), Entity("bold", 3, 6)])

    def test_truncate_clips_and_drops_broken_custom_emoji(self):
        text = "Привет 😀 мир"
        ents = [Entity("bold", 0, 6), custom(7, 2, "1")]
        body, got = E.truncate(text, ents, 8)  # обрезали посреди эмодзи (суррогатная пара)
        self.assertEqual(body, "Привет 😀")
        self.assertEqual(got, ents)
        body, got = E.truncate(text, ents, 7)
        self.assertEqual((body, got), ("Привет ", [Entity("bold", 0, 6)]))
        self.assertEqual(E.truncate("коротко", ents, 100), ("коротко", ents))

    def test_remap_custom_emoji_by_order(self):
        original = "Привет 😀 и 🔥 снова 😀"
        o16 = lambda s: E.u16len(original[: original.index(s)])  # noqa: E731
        ents = [custom(o16("😀"), 2, "A"), custom(o16("🔥"), 2, "F"),
                custom(E.u16len(original) - 2, 2, "B")]
        new = "Новый 😀 текст 🔥 и ещё 😀"
        got = E.remap_custom_emoji(original, ents, new)
        self.assertEqual([(e.custom_emoji_id, new.encode("utf-16-le")[e.offset * 2:(e.offset + e.length) * 2]
                           .decode("utf-16-le")) for e in got], [("A", "😀"), ("F", "🔥"), ("B", "😀")])

    def test_remap_when_model_dropped_or_added_emoji(self):
        original = "Тест 😀 и 😀"
        ents = [custom(5, 2, "A"), custom(10, 2, "B")]
        # модель оставила одно эмодзи — получает первое премиум-эмодзи
        self.assertEqual([e.custom_emoji_id for e in E.remap_custom_emoji(original, ents, "Другой 😀")], ["A"])
        # модель добавила лишние — лишние остаются обычными
        got = E.remap_custom_emoji(original, ents, "😀 😀 😀")
        self.assertEqual([e.custom_emoji_id for e in got], ["A", "B"])
        # эмодзи пропало совсем — премиум-эмодзи нет
        self.assertEqual(E.remap_custom_emoji(original, ents, "Без эмодзи"), [])
        self.assertEqual(E.remap_custom_emoji("без", [], "что-то"), [])

    def test_helpers(self):
        ents = [Entity("bold", 0, 1), custom(1, 2, "1")]
        self.assertTrue(E.has_custom_emoji(ents))
        self.assertEqual(E.without_custom_emoji(ents), [Entity("bold", 0, 1)])

    def test_compose_post_keeps_entities_and_credit(self):
        ents = [custom(4, 2, "1")]
        text, got = compose_post("\n Тест😀 конец", [custom(6, 2, "1")], "chan", True)
        self.assertEqual(text, "Тест😀 конец\n\nИсточник: @chan")
        self.assertEqual(got, [custom(4, 2, "1")])
        text, got = compose_post("Тест😀", ents, "chan", False)
        self.assertEqual((text, got), ("Тест😀", ents))
        # длинный текст обрезается, ссылка на источник остаётся, эмодзи за границей пропадает
        long_text = "я" * 100 + "😀"
        text, got = compose_post(long_text, [custom(100, 2, "1")], "chan", True, limit=60)
        self.assertTrue(text.endswith("Источник: @chan") and len(text) == 60)
        self.assertEqual(got, [])


# ---------- публикация ботом ----------
class PublishEntitiesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        publisher.recent_sends._items.clear()

    async def test_text_gets_entities(self):
        bot = MediaBot()
        await publisher.publish_fetched(bot, -1, [], "😀 Привет", [custom(0, 2, "77"), Entity("bold", 3, 6)])
        name, kw = bot.calls[0]
        self.assertEqual(name, "send")
        self.assertEqual([(e.type, e.offset, e.length, e.custom_emoji_id) for e in kw["entities"]],
                         [("custom_emoji", 0, 2, "77"), ("bold", 3, 6, None)])

    async def test_no_entities_means_no_kwarg(self):
        bot = MediaBot()
        await publisher.publish_fetched(bot, -1, [], "Текст")
        self.assertEqual(bot.calls[0][1], {"chat_id": -1, "text": "Текст"})

    async def test_caption_entities_single_and_album(self):
        item = MediaItem("photo", b"x", "a.jpg")
        bot = MediaBot()
        await publisher.publish_fetched(bot, -1, [item], "😀 Подпись", [custom(0, 2, "5")])
        self.assertEqual(bot.calls[0][1]["caption_entities"][0].custom_emoji_id, "5")
        bot = MediaBot()
        await publisher.publish_fetched(bot, -1, [item, item], "😀 Подпись", [custom(0, 2, "5")])
        first, second = bot.calls[0][1]["media"]
        self.assertEqual(first.caption_entities[0].custom_emoji_id, "5")
        self.assertFalse(hasattr(second, "caption_entities"))

    async def test_long_text_entities_go_with_separate_message(self):
        bot = MediaBot()
        text = "😀" + "я" * 1500
        await publisher.publish_fetched(bot, -1, [MediaItem("photo", b"x", "a.jpg")], text, [custom(0, 2, "5")])
        self.assertEqual([n for n, _ in bot.calls], ["photo", "send"])
        self.assertNotIn("caption_entities", bot.calls[0][1])
        self.assertEqual(bot.calls[1][1]["entities"][0].custom_emoji_id, "5")

    async def test_rejected_custom_emoji_retried_without_them(self):
        class Strict(MediaBot):
            async def send_message(self, **kw):
                if any(e.type == "custom_emoji" for e in kw.get("entities", [])):
                    raise RuntimeError("Bad Request: custom emoji not allowed")
                return await super().send_message(**kw)

        bot = Strict()
        ids = await publisher.publish_fetched(bot, -1, [], "😀 Текст", [custom(0, 2, "5"), Entity("bold", 3, 5)])
        self.assertEqual(len(ids), 1)
        sent = bot.calls[-1][1]
        self.assertEqual([e.type for e in sent["entities"]], ["bold"])  # жирный остался

    async def test_other_errors_still_raise(self):
        with self.assertRaises(RuntimeError):
            await publisher.publish_fetched(FakeBot(fail_chats={-1}), -1, [], "Текст", [Entity("bold", 0, 2)])


# ---------- читатель: сущности и публикация от имени аккаунта ----------
NOW = tmsg.__globals__["NOW"]


class ReaderEntitiesTests(unittest.IsolatedAsyncioTestCase):
    async def test_entities_come_from_the_text_message(self):
        ents = [_tl("MessageEntityCustomEmoji", offset=0, length=2, document_id=42)]
        m1 = tmsg(1, "", grouped=9, photo=True)
        m2 = tmsg(2, "😀 Подпись", grouped=9, photo=True)
        m2.entities = ents
        r = TelethonReader(FakeTelethon([m1, m2]), settle_seconds=20, now=lambda: NOW)
        (post,) = await r.fetch_new("chan", 0)
        self.assertEqual(post.entities, [custom(0, 2, "42")])

    async def test_message_without_entities_attribute(self):
        r = TelethonReader(FakeTelethon([tmsg(1, "Текст")]), now=lambda: NOW)
        (post,) = await r.fetch_new("chan", 0)
        self.assertEqual(post.entities, [])

    async def test_premium_detected_on_start(self):
        class Client(FakeTelethon):
            def __init__(self, premium, authorized=True):
                super().__init__([])
                self._p, self._a = premium, authorized

            async def connect(self):
                pass

            async def is_user_authorized(self):
                return self._a

            async def get_me(self):
                return SimpleNamespace(premium=self._p)

        for premium in (True, False):
            r = TelethonReader(Client(premium))
            self.assertTrue(await r.start())
            self.assertIs(r.premium, premium)
        self.assertFalse(await TelethonReader(Client(True, authorized=False)).start())


class SendClient:
    """Клиент Telethon, запоминающий, что и как отправляют."""

    def __init__(self, known=True):
        self.calls = []
        self.known = known
        self.dialogs_loaded = False
        self._n = 500

    async def get_input_entity(self, chat_id):
        if not self.known and not self.dialogs_loaded:
            raise ValueError("Could not find the input entity")
        return ("peer", chat_id)

    async def get_dialogs(self):
        self.dialogs_loaded = True

    def _msg(self):
        self._n += 1
        return SimpleNamespace(id=self._n)

    async def send_message(self, peer, text, **kw):
        self.calls.append(("message", peer, text, kw))
        return self._msg()

    async def send_file(self, peer, file, **kw):
        self.calls.append(("file", peer, file, kw))
        if isinstance(file, list):
            return [self._msg() for _ in file]
        return self._msg()


class AccountSendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Telethon в песочнице не установлен: подставляем минимальные типы сущностей
        made = types.ModuleType("telethon.tl.types")
        for name in ["MessageEntityBold", "MessageEntityItalic", "MessageEntityUnderline", "MessageEntityStrike",
                     "MessageEntitySpoiler", "MessageEntityCode", "MessageEntityPre", "MessageEntityTextUrl",
                     "MessageEntityBlockquote", "MessageEntityCustomEmoji", "DocumentAttributeVideo"]:
            setattr(made, name, type(name, (), {"__init__": lambda self, **kw: self.__dict__.update(kw)}))
        self._saved = {k: sys.modules.get(k) for k in ("telethon", "telethon.tl", "telethon.tl.types")}
        tl = types.ModuleType("telethon.tl")
        tl.types = made
        top = types.ModuleType("telethon")
        top.tl = tl
        sys.modules.update({"telethon": top, "telethon.tl": tl, "telethon.tl.types": made})

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def parse_of(self, kw, text):
        return kw["parse_mode"](text)

    async def test_text_with_custom_emoji(self):
        client = SendClient()
        ids = await TelethonReader(client).send(-100, "😀 Привет", [custom(0, 2, "123")], [])
        kind, peer, text, kw = client.calls[0]
        self.assertEqual((kind, peer, text), ("message", ("peer", -100), "😀 Привет"))
        parsed_text, ents = self.parse_of(kw, "😀 Привет")
        self.assertEqual(parsed_text, "😀 Привет")
        self.assertEqual((ents[0].document_id, ents[0].offset, ents[0].length), (123, 0, 2))
        self.assertEqual(self.parse_of(kw, "другой")[1], [])  # на чужой текст сущности не навешиваются
        self.assertEqual(ids, [501])

    async def test_single_media_and_album(self):
        client = SendClient()
        m = MediaItem("photo", b"x", "a.jpg")
        await TelethonReader(client).send(-100, "😀 Подпись", [custom(0, 2, "1")], [m])
        kind, _, file, kw = client.calls[0]
        self.assertEqual((kind, kw["caption"], file.name), ("file", "😀 Подпись", "a.jpg"))

        client = SendClient()
        ids = await TelethonReader(client).send(-100, "😀 Подпись", [custom(0, 2, "1")], [m, m, m])
        _, _, files, kw = client.calls[0]
        self.assertEqual((len(files), kw["caption"]), (3, ["😀 Подпись", "", ""]))
        self.assertEqual(len(ids), 3)

    async def test_long_text_goes_separately_and_documents_not_mixed(self):
        client = SendClient()
        photo, doc = MediaItem("photo", b"x", "a.jpg"), MediaItem("document", b"y", "b.pdf")
        await TelethonReader(client).send(-100, "я" * 1500, [], [photo])
        self.assertEqual([c[0] for c in client.calls], ["file", "message"])
        self.assertEqual(client.calls[0][3]["caption"], "")
        client = SendClient()
        await TelethonReader(client).send(-100, "Подпись", [], [photo, doc])
        self.assertEqual([c[0] for c in client.calls], ["file", "file"])  # фото и документ — раздельно
        self.assertEqual((client.calls[0][3]["caption"], client.calls[1][3]["caption"]), ("Подпись", ""))
        self.assertTrue(client.calls[1][3]["force_document"])

    async def test_unknown_channel_loads_dialogs_once(self):
        client = SendClient(known=False)
        await TelethonReader(client).send(-100, "Текст", [], [])
        self.assertTrue(client.dialogs_loaded)


# ---------- движок ----------
class AccountReader(FakeReader):
    def __init__(self, posts, premium=True, fail=False):
        super().__init__(posts)
        self.premium = premium
        self.sent = []
        self.fail_send = fail

    async def send(self, chat_id, text, entities, media):
        self.sent.append((chat_id, text, entities, media))
        if self.fail_send:
            raise RuntimeError("CHAT_ADMIN_REQUIRED")
        return [900 + len(self.sent)]


class EngineEmojiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        publisher.recent_sends._items.clear()
        from bot import mirror_engine
        mirror_engine._last_notice.clear()
        self.cfg = Config(bot_token="x")
        self.db = Database(":memory:")
        await self.db.connect()
        await self.db.ensure_user(USER, "Europe/Moscow")
        self.answer = "Новый 😀 текст"

        async def rewriter(text, style):
            return self.answer

        self.rewriter = rewriter

    async def asyncTearDown(self):
        await self.db.close()

    async def run_once(self, reader, cfg=None):
        bot = MediaBot()
        await feed_engine.run_feeds_once(bot, self.db, cfg or self.cfg, reader, self.rewriter, pause=0)
        return bot

    def post(self):
        return FetchedPost([1], "Привет 😀", [], [Entity("bold", 0, 6), custom(7, 2, "A")])

    async def test_bot_keeps_formatting_and_emoji_without_ai(self):
        await self.db.add_feed(USER, "chan", "К", 0, [-100], rewrite=False, credit=False)
        bot = await self.run_once(FakeReader([self.post()]))
        ents = bot.calls[0][1]["entities"]
        self.assertEqual([(e.type, e.custom_emoji_id) for e in ents], [("bold", None), ("custom_emoji", "A")])

    async def test_emoji_survives_ai_rewrite_formatting_dropped(self):
        await self.db.add_feed(USER, "chan", "К", 0, [-100], rewrite=True, credit=True)
        bot = await self.run_once(FakeReader([self.post()]))
        kw = bot.calls[0][1]
        self.assertEqual(kw["text"], "Новый 😀 текст\n\nИсточник: @chan")
        self.assertEqual([(e.type, e.offset, e.custom_emoji_id) for e in kw["entities"]], [("custom_emoji", 6, "A")])

    async def test_premium_account_publishes_posts_with_custom_emoji(self):
        await self.db.add_feed(USER, "chan", "К", 0, [-100], rewrite=False, credit=False)
        reader = AccountReader([self.post()])
        bot = await self.run_once(reader)
        self.assertEqual(bot.calls, [])  # бот не публиковал
        chat, text, ents, _ = reader.sent[0]
        self.assertEqual((chat, text), (-100, "Привет 😀"))
        self.assertTrue(any(e.type == "custom_emoji" for e in ents))
        self.assertIn((-100, 901), publisher.recent_sends)  # защита от зацикливания зеркал

    async def test_posts_without_custom_emoji_still_go_via_bot(self):
        await self.db.add_feed(USER, "chan", "К", 0, [-100], rewrite=False, credit=False)
        reader = AccountReader([FetchedPost([1], "Просто текст", [], [Entity("bold", 0, 6)])])
        bot = await self.run_once(reader)
        self.assertEqual((reader.sent, bot.calls[0][0]), ([], "send"))

    async def test_non_premium_account_falls_back_to_bot(self):
        await self.db.add_feed(USER, "chan", "К", 0, [-100], rewrite=False, credit=False)
        reader = AccountReader([self.post()], premium=False)
        bot = await self.run_once(reader)
        self.assertEqual(reader.sent, [])
        self.assertEqual(bot.calls[0][0], "send")

    async def test_publish_via_bot_setting(self):
        await self.db.add_feed(USER, "chan", "К", 0, [-100], rewrite=False, credit=False)
        reader = AccountReader([self.post()])
        bot = await self.run_once(reader, Config(bot_token="x", feed_publish_via="bot"))
        self.assertEqual((reader.sent, bot.calls[0][0]), ([], "send"))

    async def test_account_failure_falls_back_to_bot_and_hints_once(self):
        await self.db.add_feed(USER, "chan", "К", 0, [-100], rewrite=False, credit=False)
        reader = AccountReader([self.post(), FetchedPost([2], "Ещё 😀", [], [custom(5, 2, "B")])], fail=True)
        bot = await self.run_once(reader)
        posts = [kw for n, kw in bot.calls if kw["chat_id"] == -100]
        self.assertEqual(len(posts), 2)  # оба поста всё равно вышли
        hints = [kw for n, kw in bot.calls if kw["chat_id"] == USER]
        self.assertEqual(len(hints), 1)
        self.assertIn("админом", hints[0]["text"])


if __name__ == "__main__":
    unittest.main()


class VideoPublishTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_video_gets_duration_size_thumb_streaming(self):
        item = MediaItem("video", b"v", "a.mp4", duration=12, width=720, height=1280, thumb=b"t")
        bot = MediaBot()
        await publisher.publish_fetched(bot, -1, [item], "Текст")
        kw = bot.calls[0][1]
        self.assertEqual((kw["duration"], kw["width"], kw["height"], kw["supports_streaming"]),
                         (12, 720, 1280, True))
        self.assertIn("thumbnail", kw)

    async def test_bot_album_video_props(self):
        vid = MediaItem("video", b"v", "a.mp4", duration=3, width=10, height=20)
        bot = MediaBot()
        await publisher.publish_fetched(bot, -1, [vid, MediaItem("photo", b"x", "a.jpg")], "")
        first, second = bot.calls[0][1]["media"]
        self.assertEqual((first.duration, first.supports_streaming), (3, True))
        self.assertFalse(hasattr(second, "duration"))

    async def test_photo_gets_no_video_params(self):
        bot = MediaBot()
        await publisher.publish_fetched(bot, -1, [MediaItem("photo", b"x", "a.jpg")], "")
        self.assertNotIn("supports_streaming", bot.calls[0][1])


class AccountVideoSendTests(AccountSendTests):
    async def test_source_is_sent_by_reference(self):
        src = object()
        client = SendClient()
        await TelethonReader(client).send(-100, "", [], [MediaItem("video", b"v", "a.mp4", source=src)])
        _, _, file, kw = client.calls[0]
        self.assertIs(file, src)
        self.assertNotIn("attributes", kw)

    async def test_reupload_gets_video_attributes_and_streaming(self):
        client = SendClient()
        item = MediaItem("video", b"v", "a.mp4", duration=12, width=720, height=1280, thumb=b"t")
        await TelethonReader(client).send(-100, "", [], [item])
        _, _, _, kw = client.calls[0]
        attr = kw["attributes"][0]
        self.assertEqual((attr.duration, attr.w, attr.h, attr.supports_streaming), (12, 720, 1280, True))
        self.assertTrue(kw["supports_streaming"] and kw["thumb"].read() == b"t")


class StagingBot(MediaBot):
    async def copy_message(self, **kw):
        self.calls.append(("copy", kw))
        return SimpleNamespace(message_id=7000 + len(self.calls))

    async def copy_messages(self, **kw):
        self.calls.append(("copy_many", kw))
        return [SimpleNamespace(message_id=8000 + i) for i in range(len(kw["message_ids"]))]

    async def delete_messages(self, **kw):
        self.calls.append(("delete", kw))
        return True


class StagingReader(AccountReader):
    def __init__(self, posts, **kw):
        super().__init__(posts, **kw)
        self.deleted = []

    async def delete(self, chat_id, ids):
        self.deleted.append((chat_id, ids))


class StagingTests(EngineEmojiTests):
    test_bot_keeps_formatting_and_emoji_without_ai = test_emoji_survives_ai_rewrite_formatting_dropped = None

    STAGE = -1009

    async def run_staged(self, reader, bot=None):
        bot = bot or StagingBot()
        cfg = Config(bot_token="x", feed_staging_chat=self.STAGE)
        await feed_engine.run_feeds_once(bot, self.db, cfg, reader, self.rewriter, pause=0)
        return bot

    async def test_posted_once_to_staging_and_copied_to_every_target(self):
        await self.db.add_feed(USER, "chan", "К", 0, [-100, -200], rewrite=False, credit=False)
        reader = StagingReader([self.post()])
        bot = await self.run_staged(reader)
        self.assertEqual([s[0] for s in reader.sent], [self.STAGE])  # залито один раз, не в каналы пользователей
        copies = [kw for n, kw in bot.calls if n == "copy"]
        self.assertEqual(sorted((c["chat_id"], c["from_chat_id"], c["message_id"]) for c in copies),
                         [(-200, self.STAGE, 901), (-100, self.STAGE, 901)])
        self.assertEqual(reader.deleted, [(self.STAGE, [901])])  # витрина очищена
        self.assertFalse([n for n, _ in bot.calls if n == "send"])  # бот сам ничего не публиковал

    async def test_album_is_copied_with_copy_messages(self):
        await self.db.add_feed(USER, "chan", "К", 0, [-100], rewrite=False, credit=False)

        class AlbumReader(StagingReader):
            async def send(self, chat_id, text, entities, media):
                self.sent.append((chat_id, text, entities, media))
                return [11, 12, 13]

        reader = AlbumReader([self.post()])
        bot = await self.run_staged(reader)
        [(n, kw)] = [c for c in bot.calls if c[0] == "copy_many"]
        self.assertEqual((kw["chat_id"], kw["from_chat_id"], kw["message_ids"]), (-100, self.STAGE, [11, 12, 13]))

    async def test_staging_failure_falls_back_to_direct_then_bot(self):
        await self.db.add_feed(USER, "chan", "К", 0, [-100], rewrite=False, credit=False)

        class Reader(StagingReader):
            async def send(self, chat_id, text, entities, media):
                self.sent.append((chat_id, text, entities, media))
                if chat_id == StagingTests.STAGE:
                    raise RuntimeError("витрина недоступна")
                return [55]

        reader = Reader([self.post()])
        bot = await self.run_staged(reader)
        self.assertEqual([s[0] for s in reader.sent], [self.STAGE, -100])  # потом прямая публикация аккаунтом
        self.assertEqual([n for n, _ in bot.calls if n != "copy"], [])

    async def test_copy_failure_falls_back_to_bot(self):
        await self.db.add_feed(USER, "chan", "К", 0, [-100], rewrite=False, credit=False)

        class Bad(StagingBot):
            async def copy_message(self, **kw):
                raise RuntimeError("chat not found")

        reader = StagingReader([self.post()], fail=False)
        reader.fail_send = False

        class NoDirect(StagingReader):
            async def send(self, chat_id, text, entities, media):
                if chat_id != StagingTests.STAGE:
                    raise RuntimeError("not admin")
                return await super().send(chat_id, text, entities, media)

        bot = await self.run_staged(NoDirect([self.post()]), Bad())
        self.assertEqual([n for n, kw in bot.calls if kw.get("chat_id") == -100], ["send"])  # пост всё равно вышел

    async def test_without_staging_setting_nothing_changes(self):
        await self.db.add_feed(USER, "chan", "К", 0, [-100], rewrite=False, credit=False)
        reader = StagingReader([self.post()])
        bot = await self.run_once(reader)
        self.assertEqual(reader.sent[0][0], -100)
        self.assertEqual(bot.calls, [])

    async def test_posts_without_custom_emoji_skip_staging(self):
        await self.db.add_feed(USER, "chan", "К", 0, [-100], rewrite=False, credit=False)
        reader = StagingReader([FetchedPost([1], "Просто текст", [], [Entity("bold", 0, 6)])])
        bot = await self.run_staged(reader)
        self.assertEqual((reader.sent, bot.calls[0][0]), ([], "send"))


class StagingKeepTests(StagingTests):
    async def test_keep_flag_leaves_posts_in_staging(self):
        await self.db.add_feed(USER, "chan", "К", 0, [-100], rewrite=False, credit=False)
        reader = StagingReader([self.post()])
        bot = StagingBot()
        cfg = Config(bot_token="x", feed_staging_chat=self.STAGE, feed_staging_keep=True)
        await feed_engine.run_feeds_once(bot, self.db, cfg, reader, self.rewriter, pause=0)
        self.assertEqual(reader.deleted, [])
        self.assertTrue([n for n, _ in bot.calls if n == "copy"])