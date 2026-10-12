"""Нейропостинг: читатель Telethon (с поддельным клиентом), движок опроса, БД, публикация, /feeds."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests import aiogram_stub

aiogram_stub.install_if_missing()

from bot import feed_engine, mirror_engine, publisher  # noqa: E402
from bot.config import Config  # noqa: E402
from bot.db import Database, today_str  # noqa: E402
from bot.feeds import (  # noqa: E402
    MAX_MEDIA_BYTES, FetchedPost, MediaItem, SourceError, SourceInfo, compose_text, normalize_username,
)
from bot.handlers import feeds as feeds_handler  # noqa: E402
from bot.plans import FREE  # noqa: E402
from bot.telethon_reader import TelethonReader  # noqa: E402
from tests.test_core import FakeBot  # noqa: E402
from tests.test_handlers_flow import FakeCb, FakeMsg, FakeState, datas  # noqa: E402

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
USER = 1


class MediaBot(FakeBot):
    async def send_photo(self, **kw):
        return self._answer("photo", kw)

    async def send_video(self, **kw):
        return self._answer("video", kw)

    async def send_document(self, **kw):
        return self._answer("document", kw)

    async def send_media_group(self, **kw):
        self.calls.append(("group", kw))
        return [SimpleNamespace(message_id=2000 + i) for i in range(len(kw["media"]))]


# ---------------- чистые функции ----------------
class HelpersTests(unittest.TestCase):
    def test_normalize_username(self):
        for raw in ["@durov", "durov", "t.me/durov", "https://t.me/durov", "https://t.me/s/durov",
                    " t.me/durov/123 ", "https://telegram.me/durov?start=1"]:
            self.assertEqual(normalize_username(raw), "durov", raw)

    def test_normalize_rejects(self):
        for raw in ["t.me/+AbCdEf", "https://t.me/joinchat/xyz", "ab", "привет", "", "t.me/c/123/4", "@a b"]:
            with self.subTest(raw=raw), self.assertRaises(SourceError):
                normalize_username(raw)

    def test_compose_text(self):
        self.assertEqual(compose_text("Текст", "chan", True), "Текст\n\nИсточник: @chan")
        self.assertEqual(compose_text("Текст", "chan", False), "Текст")
        self.assertEqual(compose_text("", "chan", True), "Источник: @chan")
        long = compose_text("я" * 5000, "chan", True, limit=100)
        self.assertEqual(len(long), 100)
        self.assertTrue(long.endswith("Источник: @chan"))  # ссылка не обрезается
        self.assertEqual(len(compose_text("я" * 5000, "chan", False, limit=100)), 100)


# ---------------- читатель Telethon ----------------
def tmsg(mid, text="", date=None, grouped=None, photo=False, video=False, doc=False,
         sticker=False, action=None, size=100, name=None, ext=None):
    f = (SimpleNamespace(size=size, name=name, ext=ext, duration=12, width=720, height=1280)
         if (photo or video or doc or sticker) else None)
    return SimpleNamespace(
        id=mid, message=text, date=date or NOW - timedelta(minutes=5), grouped_id=grouped,
        photo=object() if photo else None, video=object() if video else None, gif=None,
        document=object() if (doc or video or sticker) else None, sticker=object() if sticker else None,
        video_note=None, action=action, file=f, media=object() if (photo or video or doc) else None,
    )


INVITE = "AbCdEfGhIjKlMnOpQr"


class HelpersPrivateTests(unittest.TestCase):
    def test_normalize_invites(self):
        for raw in [f"https://t.me/+{INVITE}", f"t.me/+{INVITE}", f"t.me/joinchat/{INVITE}",
                    f"https://t.me/joinchat/{INVITE}?x=1", f"tg://join?invite={INVITE}", f" +{INVITE} "]:
            self.assertEqual(normalize_username(raw), "+" + INVITE, raw)

    def test_hash_case_is_preserved_but_usernames_lowercased(self):
        from bot.feeds import feed_key, is_private, source_label

        self.assertEqual(feed_key("+" + INVITE), "+" + INVITE)
        self.assertEqual(feed_key("MixedCase"), "mixedcase")
        self.assertTrue(is_private("+" + INVITE) and not is_private("durov"))
        self.assertEqual(source_label("durov"), "@durov")
        self.assertEqual(source_label("+" + INVITE, "Клуб"), "🔒 Клуб")
        self.assertEqual(source_label("+" + INVITE), "закрытый канал")

    def test_invalid_invites(self):
        for raw in ["t.me/+short", "t.me/joinchat/", "tg://join?invite=", "t.me/+" + "x" * 80]:
            with self.subTest(raw=raw), self.assertRaises(SourceError):
                normalize_username(raw)

    def test_compose_private_credit_uses_title_not_hash(self):
        out = compose_text("Текст", "+" + INVITE, True, title="Клуб")
        self.assertEqual(out, "Текст\n\nИсточник: «Клуб»")
        self.assertNotIn(INVITE, out)
        self.assertEqual(compose_text("Текст", "+" + INVITE, True), "Текст\n\nИсточник: закрытый канал")


class FakeTelethon:
    def __init__(self, messages, entity=None, fail_entity=False):
        self.messages = messages
        self.entity = entity or SimpleNamespace(id=777, title="Источник", broadcast=True)
        self.fail_entity = fail_entity
        self.downloads: list[int] = []
        self.get_kwargs = None

    async def get_entity(self, username):
        if self.fail_entity:
            raise ValueError("No user has username")
        return self.entity

    async def get_messages(self, ent, limit=None, min_id=0, **kw):
        self.get_kwargs = {"limit": limit, "min_id": min_id}
        res = sorted((m for m in self.messages if m.id > min_id), key=lambda m: -m.id)  # новые первыми, как у Telethon
        return res[:limit]

    async def download_media(self, m, file=None, thumb=None):
        if thumb is not None:
            return b"THUMB%d" % m.id
        self.downloads.append(m.id)
        return b"DATA%d" % m.id


class VideoPropsTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_keeps_duration_size_thumb_and_source(self):
        msg = tmsg(5, "v", video=True, name="clip.mp4")
        reader = TelethonReader(FakeTelethon([msg]), settle_seconds=20, now=lambda: NOW)
        reader._cache["chan"] = SimpleNamespace(id=1, title="C", broadcast=True)
        [post] = await reader.fetch_new("chan", 0)
        item = post.media[0]
        self.assertEqual((item.duration, item.width, item.height), (12, 720, 1280))
        self.assertEqual(item.thumb, b"THUMB5")
        self.assertIs(item.source, msg.media)

    async def test_photo_has_no_video_props(self):
        reader = TelethonReader(FakeTelethon([tmsg(6, "p", photo=True)]), settle_seconds=20, now=lambda: NOW)
        reader._cache["chan"] = SimpleNamespace(id=1, title="C", broadcast=True)
        [post] = await reader.fetch_new("chan", 0)
        self.assertEqual((post.media[0].duration, post.media[0].thumb), (0, None))

    async def test_thumb_failure_does_not_lose_video(self):
        class NoThumb(FakeTelethon):
            async def download_media(self, m, file=None, thumb=None):
                if thumb is not None:
                    raise RuntimeError("no thumb")
                return await super().download_media(m, file)

        reader = TelethonReader(NoThumb([tmsg(7, "v", video=True)]), settle_seconds=20, now=lambda: NOW)
        reader._cache["chan"] = SimpleNamespace(id=1, title="C", broadcast=True)
        [post] = await reader.fetch_new("chan", 0)
        self.assertIsNone(post.media[0].thumb)


class PrivateReaderTests(unittest.IsolatedAsyncioTestCase):
    """Вступление по инвайту проверено через подмену _invite_entity: сами запросы Telethon в песочнице не запускались."""

    def make(self, messages, entity=None):
        client = FakeTelethon(messages)
        reader = TelethonReader(client, settle_seconds=20, now=lambda: NOW)
        reader.invites = []
        ent = entity or SimpleNamespace(id=900, title="Закрытый клуб", broadcast=True)

        async def fake_invite(h):
            reader.invites.append(h)
            return ent

        reader._invite_entity = fake_invite
        client.get_entity = AsyncMock(side_effect=AssertionError("для закрытого канала get_entity не нужен"))
        return reader, client

    async def test_resolve_and_fetch_private_with_cache(self):
        r, c = self.make([tmsg(1, "старое"), tmsg(2, "новое")])
        key = "+" + INVITE
        info = await r.resolve(key)
        self.assertEqual((info.chat_id, info.title), (900, "Закрытый клуб"))
        posts = await r.fetch_new(key, after_id=1)
        self.assertEqual([p.text for p in posts], ["новое"])
        await r.latest_id(key)
        self.assertEqual(r.invites, [INVITE])  # вступали/проверяли один раз, дальше берём из кеша

    async def test_private_group_is_rejected(self):
        r, _ = self.make([], entity=SimpleNamespace(id=1, title="Чат", broadcast=False))
        with self.assertRaises(SourceError):
            await r.resolve("+" + INVITE)

    async def test_private_failure_becomes_friendly_error(self):
        client = FakeTelethon([])
        r = TelethonReader(client)

        async def boom(h):
            raise RuntimeError("INVITE_HASH_EXPIRED")

        r._invite_entity = boom
        with self.assertRaises(SourceError) as cm:
            await r.resolve("+" + INVITE)
        self.assertIn("недействительна", str(cm.exception))

    async def test_private_floodwait_passes_through(self):
        class Flood(Exception):
            seconds = 30

        r = TelethonReader(FakeTelethon([]))

        async def flood(h):
            raise Flood()

        r._invite_entity = flood
        with self.assertRaises(Flood):
            await r.resolve("+" + INVITE)


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    def reader(self, messages, **kw):
        client = FakeTelethon(messages, **kw)
        return TelethonReader(client, settle_seconds=20, now=lambda: NOW), client

    async def test_resolve_and_latest(self):
        r, _ = self.reader([tmsg(5, "a"), tmsg(9, "b")])
        info = await r.resolve("chan")
        self.assertEqual((info.chat_id, info.title, info.username), (777, "Источник", "chan"))
        self.assertEqual(await r.latest_id("chan"), 9)
        empty, _ = self.reader([])
        self.assertEqual(await empty.latest_id("chan"), 0)

    async def test_resolve_errors(self):
        r, _ = self.reader([], entity=SimpleNamespace(id=1, title="Чат", broadcast=False))
        with self.assertRaises(SourceError):
            await r.resolve("chat")
        r, _ = self.reader([], fail_entity=True)
        with self.assertRaises(SourceError):
            await r.resolve("nobody")

    async def test_fetch_new_order_text_and_min_id(self):
        r, c = self.reader([tmsg(1, "старое"), tmsg(2, "первое"), tmsg(3, "второе")])
        posts = await r.fetch_new("chan", after_id=1)
        self.assertEqual([p.text for p in posts], ["первое", "второе"])  # по возрастанию
        self.assertEqual(c.get_kwargs["min_id"], 1)
        self.assertEqual([p.message_ids for p in posts], [[2], [3]])

    async def test_fresh_messages_not_settled_are_deferred(self):
        fresh = tmsg(3, "только что", date=NOW - timedelta(seconds=5))
        r, _ = self.reader([tmsg(2, "готово"), fresh])
        self.assertEqual([p.text for p in await r.fetch_new("chan", 0)], ["готово"])

    async def test_album_grouped_into_one_post(self):
        msgs = [tmsg(10, "", grouped=5, photo=True), tmsg(11, "Подпись", grouped=5, photo=True),
                tmsg(12, "", grouped=5, video=True), tmsg(13, "Отдельный текст")]
        r, c = self.reader(msgs)
        album, single = await r.fetch_new("chan", 0)
        self.assertEqual(album.message_ids, [10, 11, 12])
        self.assertEqual(album.text, "Подпись")
        self.assertEqual([m.kind for m in album.media], ["photo", "photo", "video"])
        self.assertEqual(album.media[0].filename, "photo.jpg")
        self.assertEqual(album.media[0].data, b"DATA10")
        self.assertEqual((single.text, single.media), ("Отдельный текст", []))

    async def test_skips_service_stickers_oversize_and_empty(self):
        msgs = [tmsg(1, "вступил", action=object()), tmsg(2, sticker=True),
                tmsg(3, "", photo=True, size=MAX_MEDIA_BYTES + 1),
                tmsg(4, "Большой файл с текстом", doc=True, size=MAX_MEDIA_BYTES + 1),
                tmsg(5, "Файл", doc=True, name="report.pdf")]
        r, c = self.reader(msgs)
        posts = await r.fetch_new("chan", 0)
        self.assertEqual([p.message_ids for p in posts], [[4], [5]])
        self.assertEqual(posts[0].media, [])  # слишком большой файл не качаем, текст остаётся
        self.assertEqual(posts[1].media[0].filename, "report.pdf")
        self.assertEqual(c.downloads, [5])

    async def test_floodwait_passes_through(self):
        class Flood(Exception):
            seconds = 7

        client = FakeTelethon([])
        client.get_entity = AsyncMock(side_effect=Flood())
        r = TelethonReader(client)
        with self.assertRaises(Flood):
            await r.fetch_new("chan", 0)


# ---------------- БД ----------------
class FeedDBTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(":memory:")
        await self.db.connect()
        await self.db.ensure_user(USER, "Europe/Moscow")

    async def asyncTearDown(self):
        await self.db.close()

    async def test_crud(self):
        fid = await self.db.add_feed(USER, "Chan", "Канал", 10, [-1, -2, -1], rewrite=True, style="short")
        (f,) = await self.db.list_feeds(USER)
        self.assertEqual((f["id"], f["username"], f["last_msg_id"], f["targets"], f["style"]),
                         (fid, "chan", 10, [-2, -1], "short"))
        self.assertTrue(f["rewrite"] and f["credit"] and f["active"])
        with self.assertRaises(ValueError):
            await self.db.add_feed(USER, "CHAN", "Канал", 0, [-1])  # дубликат без учёта регистра
        with self.assertRaises(ValueError):
            await self.db.add_feed(USER, "other", "x", 0, [])
        self.assertEqual(await self.db.count_feeds(USER), 1)

        self.assertIs(await self.db.toggle_feed(USER, fid, "rewrite"), False)
        self.assertIs(await self.db.toggle_feed(USER, fid, "credit"), False)
        self.assertIs(await self.db.toggle_feed(USER, fid, "active"), False)
        self.assertEqual(await self.db.list_active_feeds(), [])
        self.assertIsNone(await self.db.toggle_feed(2, fid, "active"))  # чужой
        with self.assertRaises(ValueError):
            await self.db.toggle_feed(USER, fid, "owner_id")  # защита от подстановки имени колонки

        await self.db.update_feed_last(fid, 50)
        await self.db.update_feed_last(fid, 20)  # назад не двигаем
        self.assertEqual((await self.db.list_feeds(USER))[0]["last_msg_id"], 50)
        self.assertTrue(await self.db.set_feed_style(USER, fid, "lively"))
        self.assertFalse(await self.db.delete_feed(2, fid))
        self.assertTrue(await self.db.delete_feed(USER, fid))
        self.assertEqual(await self.db.list_feeds(USER), [])

    async def test_private_key_keeps_case_and_dedups(self):
        key = "+" + INVITE
        await self.db.add_feed(USER, key, "Клуб", 5, [-1])
        self.assertEqual((await self.db.list_feeds(USER))[0]["username"], key)
        with self.assertRaises(ValueError):
            await self.db.add_feed(USER, key, "Клуб", 0, [-1])
        await self.db.add_feed(USER, key.lower(), "Другой", 0, [-1])  # другой регистр хэша — другая ссылка

    async def test_remove_channel_cleans_feeds(self):
        await self.db.add_channel(USER, -1, "A")
        await self.db.add_channel(USER, -2, "B")
        await self.db.add_feed(USER, "one", "1", 0, [-1, -2])
        await self.db.add_feed(USER, "two", "2", 0, [-1])
        await self.db.remove_channel(USER, -1)
        feeds = {f["username"]: f["targets"] for f in await self.db.list_feeds(USER)}
        self.assertEqual(feeds, {"one": [-2]})  # у «two» не осталось получателей — правило удалено


# ---------------- публикация ----------------
class PublishFetchedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        publisher.recent_sends._items.clear()

    def item(self, kind, n=1):
        return MediaItem(kind, b"x" * n, f"f.{kind}")

    async def test_text_only(self):
        bot = MediaBot()
        ids = await publisher.publish_fetched(bot, -1, [], "Привет")
        self.assertEqual(bot.calls, [("send", {"chat_id": -1, "text": "Привет"})])
        self.assertIn((-1, ids[0]), publisher.recent_sends)
        bot = MediaBot()
        self.assertEqual(await publisher.publish_fetched(bot, -1, [], "   "), [])
        self.assertEqual(bot.calls, [])

    async def test_single_media_with_caption(self):
        bot = MediaBot()
        await publisher.publish_fetched(bot, -1, [self.item("photo")], "Подпись")
        name, kw = bot.calls[0]
        self.assertEqual((name, kw["caption"], kw["photo"].filename), ("photo", "Подпись", "f.photo"))

    async def test_long_text_goes_separately(self):
        bot = MediaBot()
        await publisher.publish_fetched(bot, -1, [self.item("video")], "я" * 2000)
        self.assertEqual([n for n, _ in bot.calls], ["video", "send"])
        self.assertNotIn("caption", bot.calls[0][1])

    async def test_album(self):
        bot = MediaBot()
        ids = await publisher.publish_fetched(bot, -1, [self.item("photo"), self.item("video")], "Подпись")
        name, kw = bot.calls[0]
        self.assertEqual(name, "group")
        self.assertEqual(kw["media"][0].caption, "Подпись")
        self.assertIsNone(kw["media"][1].caption)
        self.assertEqual(ids, [2000, 2001])

    async def test_documents_mixed_with_photo_sent_separately(self):
        bot = MediaBot()
        await publisher.publish_fetched(bot, -1, [self.item("photo"), self.item("document")], "Подпись")
        self.assertEqual([n for n, _ in bot.calls], ["photo", "document"])
        self.assertEqual(bot.calls[0][1]["caption"], "Подпись")
        self.assertNotIn("caption", bot.calls[1][1])


# ---------------- движок ----------------
class FakeReader:
    def __init__(self, posts=None, fail=None, latest=100):
        self.posts = posts or []
        self.fail = fail
        self.latest = latest
        self.calls: list[tuple[str, int]] = []

    async def resolve(self, username):
        if username == "nope":
            raise SourceError("Канал @nope не найден или недоступен.")
        if username.startswith("+"):
            return SourceInfo(6, username, "Закрытый клуб")
        return SourceInfo(5, username, f"Канал {username}")

    async def latest_id(self, username):
        return self.latest

    async def fetch_new(self, username, after_id):
        self.calls.append((username, after_id))
        if self.fail:
            raise self.fail
        return [p for p in self.posts if p.last_id > after_id]


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        publisher.recent_sends._items.clear()
        mirror_engine._last_notice.clear()
        self.cfg = Config(bot_token="x")
        self.db = Database(":memory:")
        await self.db.connect()
        await self.db.ensure_user(USER, "Europe/Moscow")
        self.rewrites: list[tuple[str, str]] = []

        async def rewriter(text, style):
            self.rewrites.append((text, style))
            return "ПЕРЕПИСАНО"

        self.rewriter = rewriter

    async def asyncTearDown(self):
        await self.db.close()

    async def run_once(self, reader, bot=None):
        bot = bot or MediaBot()
        n = await feed_engine.run_feeds_once(bot, self.db, self.cfg, reader, self.rewriter, pause=0)
        return n, bot

    async def test_new_post_rewritten_credited_and_cursor_moves(self):
        await self.db.add_feed(USER, "chan", "Канал", 10, [-100, -200], rewrite=True, style="short")
        reader = FakeReader([FetchedPost([5], "старое"), FetchedPost([11], "Новость")])
        n, bot = await self.run_once(reader)
        self.assertEqual(n, 2)
        self.assertEqual(self.rewrites, [("Новость", "short")])  # один рерайт на два канала
        self.assertEqual([(c, kw["chat_id"]) for c, kw in bot.calls],
                         [("send", -200), ("send", -100)] if bot.calls[0][1]["chat_id"] == -200 else
                         [("send", -100), ("send", -200)])
        self.assertEqual(bot.calls[0][1]["text"], "ПЕРЕПИСАНО\n\nИсточник: @chan")
        self.assertEqual((await self.db.list_feeds(USER))[0]["last_msg_id"], 11)
        # второй проход ничего не дублирует
        n2, bot2 = await self.run_once(reader)
        self.assertEqual((n2, bot2.calls), (0, []))
        self.assertEqual(reader.calls[-1], ("chan", 11))

    async def test_no_ai_no_credit_keeps_original(self):
        await self.db.add_feed(USER, "chan", "К", 0, [-100], rewrite=False, credit=False)
        _, bot = await self.run_once(FakeReader([FetchedPost([1], "Оригинал")]))
        self.assertEqual(self.rewrites, [])
        self.assertEqual(bot.calls[0][1]["text"], "Оригинал")

    async def test_private_source_credit_uses_title(self):
        await self.db.add_feed(USER, "+" + INVITE, "Закрытый клуб", 0, [-100], rewrite=False)
        reader = FakeReader([FetchedPost([1], "Секретная новость")])
        _, bot = await self.run_once(reader)
        self.assertEqual(bot.calls[0][1]["text"], "Секретная новость\n\nИсточник: «Закрытый клуб»")
        self.assertEqual(reader.calls[0][0], "+" + INVITE)

    async def test_private_read_error_message_has_no_hash(self):
        await self.db.add_feed(USER, "+" + INVITE, "Закрытый клуб", 0, [-100])
        _, bot = await self.run_once(FakeReader(fail=SourceError("ссылка отозвана")))
        (dm,) = [kw for _, kw in bot.calls if kw["chat_id"] == USER]
        self.assertIn("🔒 Закрытый клуб", dm["text"])
        self.assertNotIn(INVITE, dm["text"])

    async def test_media_post_caption_rewritten(self):
        await self.db.add_feed(USER, "chan", "К", 0, [-100])
        post = FetchedPost([3, 4], "Подпись", [MediaItem("photo", b"1", "a.jpg"), MediaItem("photo", b"2", "b.jpg")])
        _, bot = await self.run_once(FakeReader([post]))
        name, kw = bot.calls[0]
        self.assertEqual(name, "group")
        self.assertEqual(kw["media"][0].caption, "ПЕРЕПИСАНО\n\nИсточник: @chan")

    async def test_media_without_text_not_sent_to_ai(self):
        await self.db.add_feed(USER, "chan", "К", 0, [-100])
        _, bot = await self.run_once(FakeReader([FetchedPost([3], "", [MediaItem("photo", b"1", "a.jpg")])]))
        self.assertEqual(self.rewrites, [])
        self.assertEqual(bot.calls[0][1]["caption"], "Источник: @chan")

    async def test_quota_exhausted_posts_original_and_warns_once(self):
        await self.db.add_feed(USER, "chan", "К", 0, [-100])
        for _ in range(FREE.rewrites_per_day):
            await self.db.try_consume_rewrite(USER, today_str(), 99)
        reader = FakeReader([FetchedPost([1], "Раз"), FetchedPost([2], "Два")])
        _, bot = await self.run_once(reader)
        self.assertEqual(self.rewrites, [])
        texts = [kw["text"] for _, kw in bot.calls if kw["chat_id"] == -100]
        self.assertEqual(texts, ["Раз\n\nИсточник: @chan", "Два\n\nИсточник: @chan"])
        dms = [kw for _, kw in bot.calls if kw["chat_id"] == USER]
        self.assertEqual(len(dms), 1)

    async def test_rewriter_failure_posts_original_and_refunds(self):
        await self.db.add_feed(USER, "chan", "К", 0, [-100])

        async def broken(text, style):
            raise RuntimeError("модель недоступна")

        self.rewriter = broken
        _, bot = await self.run_once(FakeReader([FetchedPost([1], "Оригинал")]))
        posted = [kw for n, kw in bot.calls if kw["chat_id"] == -100]
        self.assertEqual(posted[0]["text"], "Оригинал\n\nИсточник: @chan")
        self.assertEqual(await self.db.rewrites_used(USER, today_str()), 0)
        dms = [kw for n, kw in bot.calls if kw["chat_id"] == USER]
        self.assertIn("модель недоступна", dms[0]["text"])  # владелец узнаёт причину

    async def test_read_error_notifies_owner_once_and_keeps_cursor(self):
        await self.db.add_feed(USER, "chan", "К", 7, [-100])
        reader = FakeReader(fail=SourceError("Канал @chan не найден или недоступен."))
        _, bot = await self.run_once(reader)
        await self.run_once(reader, bot)
        dms = [kw for _, kw in bot.calls if kw["chat_id"] == USER]
        self.assertEqual(len(dms), 1)
        self.assertEqual((await self.db.list_feeds(USER))[0]["last_msg_id"], 7)

    async def test_floodwait_sleeps_and_does_not_notify(self):
        class Flood(Exception):
            seconds = 0

        await self.db.add_feed(USER, "chan", "К", 7, [-100])
        _, bot = await self.run_once(FakeReader(fail=Flood()))
        self.assertEqual(bot.calls, [])

    async def test_failed_target_still_advances_cursor(self):
        await self.db.add_feed(USER, "chan", "К", 0, [-100, -200], rewrite=False)
        reader = FakeReader([FetchedPost([4], "Текст")])
        n, bot = await self.run_once(reader, MediaBot(fail_chats={-100}))
        self.assertEqual(n, 1)  # второй канал доставлен
        self.assertEqual((await self.db.list_feeds(USER))[0]["last_msg_id"], 4)
        self.assertEqual(len([1 for _, kw in bot.calls if kw["chat_id"] == USER]), 1)  # владельцу сообщили

    async def test_backlog_is_capped(self):
        await self.db.add_feed(USER, "chan", "К", 0, [-100], rewrite=False)
        posts = [FetchedPost([i], f"п{i}") for i in range(1, 31)]
        n, bot = await self.run_once(FakeReader(posts))
        self.assertEqual(n, feed_engine.MAX_BACKLOG)
        self.assertTrue(bot.calls[0][1]["text"].startswith("п21"))
        self.assertEqual((await self.db.list_feeds(USER))[0]["last_msg_id"], 30)

    async def test_paused_feed_ignored(self):
        fid = await self.db.add_feed(USER, "chan", "К", 0, [-100])
        await self.db.toggle_feed(USER, fid, "active")
        reader = FakeReader([FetchedPost([1], "Текст")])
        n, _ = await self.run_once(reader)
        self.assertEqual((n, reader.calls), (0, []))

    async def test_own_channel_posts_not_reechoed_by_mirrors(self):
        """Пост, опубликованный нейропостингом, не должен зациклиться через автопересылку."""
        await self.db.add_feed(USER, "chan", "К", 0, [-100], rewrite=False)
        await self.db.add_mirror(USER, -100, [-200])
        _, bot = await self.run_once(FakeReader([FetchedPost([1], "Текст")]))
        sent_id = bot.calls[0]
        self.assertEqual(sent_id[0], "send")
        msg_id = 1001
        n = await mirror_engine.handle_channel_post(bot, self.db, self.cfg, chat_id=-100, message_id=msg_id,
                                                    text="Текст", kind="text", rewriter=self.rewriter)
        self.assertEqual(n, 0)


# ---------------- хэндлер /feeds ----------------
class FeedsHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg = Config(bot_token="x")
        self.db = Database(":memory:")
        await self.db.connect()
        await self.db.ensure_user(USER, "Europe/Moscow")
        await self.db.add_channel(USER, -100, "Мой канал")
        await self.db.add_channel(USER, -200, "Второй")
        self.state = FakeState()
        self.reader = FakeReader(latest=321)

    async def asyncTearDown(self):
        await self.db.close()

    async def test_disabled_without_reader(self):
        msg = FakeMsg()
        await feeds_handler.cmd_feeds(msg, self.db, self.cfg, None)
        self.assertIn("не настроен", msg.last()[1])
        cb = FakeCb("fn:start")
        await feeds_handler.new_start(cb, self.state, self.db, None)
        self.assertTrue(cb.answers[-1][1])

    async def test_full_add_flow(self):
        await feeds_handler.new_start(FakeCb("fn:start"), self.state, self.db, self.reader)
        self.assertIs(self.state.state, feeds_handler.FeedNew.username)

        bad = FakeMsg(text="t.me/nope")
        await feeds_handler.got_username(bad, self.state, self.db, self.reader)
        self.assertIn("не найден", bad.last()[1])
        self.assertIs(self.state.state, feeds_handler.FeedNew.username)

        priv = FakeMsg(text="https://t.me/+secret")
        await feeds_handler.got_username(priv, self.state, self.db, self.reader)
        self.assertIn("выглядит неверно", priv.last()[1])

        ok = FakeMsg(text="@news_channel")
        await feeds_handler.got_username(ok, self.state, self.db, self.reader)
        self.assertIs(self.state.state, feeds_handler.FeedNew.targets)
        self.assertEqual(self.state.data["username"], "news_channel")
        self.assertIn("fn:tg:-100", datas(ok.last()[2]))

        for data in ["fn:tg:-100", "fn:tg:-200", "fn:tg:-200", "fn:st:lively", "fn:cr"]:
            await feeds_handler.targets_step(FakeCb(data, FakeMsg()), self.state, self.db, self.reader)
        done = FakeCb("fn:done", FakeMsg())
        await feeds_handler.targets_step(done, self.state, self.db, self.reader)
        (f,) = await self.db.list_feeds(USER)
        self.assertEqual((f["username"], f["targets"], f["style"], f["credit"], f["rewrite"]),
                         ("news_channel", [-100], "lively", False, True))
        self.assertEqual(f["last_msg_id"], 321)  # старые посты не публикуем
        self.assertIn("@news_channel → Мой канал", done.message.last()[1])
        self.assertIsNone(self.state.state)

    async def test_add_private_channel_by_invite(self):
        await feeds_handler.new_start(FakeCb("fn:start"), self.state, self.db, self.reader)
        msg = FakeMsg(text=f"https://t.me/+{INVITE}")
        await feeds_handler.got_username(msg, self.state, self.db, self.reader)
        self.assertIs(self.state.state, feeds_handler.FeedNew.targets)
        self.assertEqual(self.state.data["username"], "+" + INVITE)
        self.assertIn("только с разрешения владельца", msg.last()[1])
        self.assertNotIn(INVITE, msg.last()[1])  # хэш инвайта пользователю обратно не светим
        await feeds_handler.targets_step(FakeCb("fn:tg:-100", FakeMsg()), self.state, self.db, self.reader)
        done = FakeCb("fn:done", FakeMsg())
        await feeds_handler.targets_step(done, self.state, self.db, self.reader)
        (f,) = await self.db.list_feeds(USER)
        self.assertEqual((f["username"], f["title"], f["last_msg_id"]), ("+" + INVITE, "Закрытый клуб", 321))
        self.assertIn("🔒 Закрытый клуб → Мой канал", done.message.last()[1])
        self.assertNotIn(INVITE, done.message.last()[1])
        # повторная отправка той же ссылки — дубликат
        await feeds_handler.new_start(FakeCb("fn:start"), self.state, self.db, self.reader)
        dup = FakeMsg(text=f"t.me/joinchat/{INVITE}")
        await feeds_handler.got_username(dup, self.state, self.db, self.reader)
        self.assertIn("уже добавлен", dup.last()[1])

    async def test_duplicate_limit_and_no_channels(self):
        await self.db.add_feed(USER, "dupe", "Д", 0, [-100])
        msg = FakeMsg(text="@dupe")
        await feeds_handler.got_username(msg, self.state, self.db, self.reader)
        self.assertIn("уже добавлен", msg.last()[1])

        await self.db.add_feed(USER, "second", "2", 0, [-100])  # лимит Free = 2
        cb = FakeCb("fn:start")
        await feeds_handler.new_start(cb, self.state, self.db, self.reader)
        self.assertIn("/pro", cb.answers[-1][0])

        db2 = Database(":memory:")
        await db2.connect()
        await db2.ensure_user(USER, "Europe/Moscow")
        cb = FakeCb("fn:start")
        await feeds_handler.new_start(cb, self.state, db2, self.reader)
        self.assertIn("/channels", cb.answers[-1][0])
        await db2.close()

    async def test_done_without_targets(self):
        await self.state.set_state(feeds_handler.FeedNew.targets)
        await self.state.set_data({"username": "x" * 5, "title": "t", "targets": [], "ai": True,
                                   "style": "normal", "credit": True})
        cb = FakeCb("fn:done", FakeMsg())
        await feeds_handler.targets_step(cb, self.state, self.db, self.reader)
        self.assertTrue(cb.answers[-1][1])
        self.assertEqual(await self.db.count_feeds(USER), 0)

    async def test_manage(self):
        fid = await self.db.add_feed(USER, "chan", "К", 0, [-100])
        msg = FakeMsg()
        await feeds_handler.cmd_feeds(msg, self.db, self.cfg, self.reader)
        self.assertIn("@chan → Мой канал", msg.last()[1])
        self.assertIn(f"fd:del:{fid}", datas(msg.last()[2]))
        for action in ["ai", "cr", "act"]:
            await feeds_handler.manage_feed(FakeCb(f"fd:{action}:{fid}", FakeMsg()), self.db)
        (f,) = await self.db.list_feeds(USER)
        self.assertEqual((f["rewrite"], f["credit"], f["active"]), (False, False, False))
        await feeds_handler.manage_feed(FakeCb(f"fd:del:{fid}", FakeMsg()), self.db)
        self.assertEqual(await self.db.count_feeds(USER), 0)
        cb = FakeCb(f"fd:ai:{fid}", FakeMsg())
        await feeds_handler.manage_feed(cb, self.db)
        self.assertTrue(cb.answers[-1][1])


class ConfigFeedTests(unittest.TestCase):
    def test_config(self):
        from bot.config import load_config

        cfg = load_config({"BOT_TOKEN": "t", "TG_API_ID": "123", "TG_API_HASH": "abc", "FEED_POLL_SECONDS": "60"})
        self.assertEqual((cfg.tg_api_id, cfg.tg_api_hash, cfg.tg_session, cfg.feed_poll_seconds),
                         (123, "abc", "data/userbot", 60))
        self.assertEqual(load_config({"BOT_TOKEN": "t", "TG_API_ID": ""}).tg_api_id, 0)


if __name__ == "__main__":
    unittest.main()