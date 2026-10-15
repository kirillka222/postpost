"""Админское копирование: фото и текст как есть, из нескольких фото/видео последнее отбрасывается."""
from __future__ import annotations

import unittest

from tests import aiogram_stub

aiogram_stub.install_if_missing()

from bot import feed_engine, mirror_engine, publisher  # noqa: E402
from bot.config import Config  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.entities import Entity  # noqa: E402
from bot import watermark  # noqa: E402
from bot.feeds import FetchedPost, MediaItem, SourceInfo, drop_last_visual  # noqa: E402
from bot.telethon_reader import TelethonReader  # noqa: E402
from bot.handlers import copyfeeds  # noqa: E402
from tests.test_feeds import FakeReader, MediaBot  # noqa: E402
from tests.test_handlers_flow import FakeCb, FakeMsg, FakeState  # noqa: E402

ADMIN = 1


async def _noop():
    return None


class HistoryReader(FakeReader):
    """Канал с историей: fetch_oldest отдаёт следующие посты по порядку, как настоящий читатель."""

    def __init__(self, posts):
        super().__init__(posts)
        self.oldest_calls = []
        self.top = max(p.last_id for p in posts)
        self.latest = self.top

    async def fetch_oldest(self, username, after_id, limit=5):
        self.oldest_calls.append((after_id, limit))
        # ids без постов (служебные сообщения канала) тоже занимают место в пачке, как у настоящего читателя
        window = range(after_id + 1, after_id + 1 + limit)
        got = [p for p in sorted(self.posts, key=lambda p: p.last_id) if p.last_id in window]
        return got, (after_id + limit if window[-1] <= self.top else self.top)


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


class CleanTests(unittest.IsolatedAsyncioTestCase):
    def test_plan(self):
        self.assertEqual(watermark.clean_plan(1000, 500, {"pos": "br", "w": 30, "h": 10, "mode": "crop"}),
                         ("crop", (0, 0, 1000, 450)))  # нижний угол: срезаем нижнюю полосу
        self.assertEqual(watermark.clean_plan(1000, 500, {"pos": "tl", "w": 30, "h": 10, "mode": "crop"}),
                         ("crop", (0, 50, 1000, 450)))
        self.assertEqual(watermark.clean_plan(1000, 500, {"pos": "mr", "w": 20, "h": 10, "mode": "crop"}),
                         ("crop", (0, 0, 800, 500)))
        self.assertEqual(watermark.clean_plan(1000, 500, {"pos": "c", "mode": "crop"})[0], "blur")  # центр не обрезать
        kind, (x, y, w, h) = watermark.clean_plan(1000, 500, {"pos": "br", "w": 30, "h": 10, "mode": "blur"})
        self.assertEqual(kind, "blur")
        self.assertTrue(x >= 1 and y >= 1 and x + w <= 999 and y + h <= 499)  # рамка внутри кадра

    def test_image_blur_and_crop(self):
        import io

        from PIL import Image

        img = Image.new("RGB", (400, 300), (30, 30, 30))
        for x in range(300, 396):  # «знак»: светлая плашка в правом нижнем углу
            for y in range(270, 296):
                img.putpixel((x, y), (255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        src = buf.getvalue()
        blurred = Image.open(io.BytesIO(watermark.image_bytes(src, None, {"pos": "br", "w": 30, "h": 12,
                                                                          "mode": "blur"}))).convert("L")
        self.assertEqual(blurred.size, (400, 300))
        self.assertLess(blurred.getpixel((350, 283)), 200)  # плашка больше не ярко-белая
        cropped = Image.open(io.BytesIO(watermark.image_bytes(src, None, {"pos": "br", "w": 30, "h": 12,
                                                                          "mode": "crop"})))
        self.assertEqual(cropped.size, (400, 300 - 36))  # нижняя полоса 12% отрезана

    @unittest.skipUnless(watermark.find_ffmpeg(), "нет ffmpeg")
    async def test_video_blur_and_crop_with_own_mark(self):
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/t.mp4"
            subprocess.run([watermark.find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i",
                            "testsrc=size=320x180:rate=10:duration=1", "-pix_fmt", "yuv420p", path], check=True)
            with open(path, "rb") as fh:
                data = fh.read()
            spec = {"text": "@мой", "pos": "tl", "size": 20, "opacity": 80}
            for mode in ("blur", "crop"):
                out = await watermark.video_bytes(data, spec, "t.mp4", {"pos": "br", "w": 30, "h": 20, "mode": mode})
                self.assertEqual(out[4:8], b"ftyp")
                with open(f"{tmp}/o.mp4", "wb") as fh:
                    fh.write(out)
                probe = subprocess.run([watermark.find_ffmpeg(), "-i", f"{tmp}/o.mp4"], capture_output=True, text=True)
                self.assertIn("320x144" if mode == "crop" else "320x180", probe.stderr)
            only = await watermark.video_bytes(data, None, "t.mp4", {"pos": "br", "mode": "blur"})  # без своего знака
            self.assertEqual(only[4:8], b"ftyp")

    async def test_apply_items_remove_only(self):
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (400, 300), (10, 10, 10)).save(buf, "JPEG")
        out = await watermark.apply_items([MediaItem("photo", buf.getvalue(), "p.jpg")], None,
                                          remove={"pos": "br", "mode": "crop", "h": 10})
        self.assertEqual(Image.open(io.BytesIO(out[0].data)).size, (400, 270))
        same = await watermark.apply_items([MediaItem("photo", b"x", "p.jpg")], None)  # ничего не просили
        self.assertEqual(same[0].data, b"x")


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    def test_spoiler_is_dropped_from_media_and_text(self):
        class Media:
            spoiler = True
            id = 5

        plain = TelethonReader._plain(Media())
        self.assertFalse(plain.spoiler)
        self.assertEqual(plain.id, 5)
        src = Media()
        TelethonReader._plain(src)
        self.assertTrue(src.spoiler)  # исходный объект не меняем

    async def test_big_album_at_batch_boundary_is_not_mistaken_for_end(self):
        from datetime import datetime, timedelta, timezone
        from types import SimpleNamespace as NS

        old = datetime.now(timezone.utc) - timedelta(days=1)
        album = [NS(id=i, date=old, grouped_id=77, message="", photo=True, action=None) for i in range(1, 9)]
        after = [NS(id=9, date=old, grouped_id=None, message="дальше", photo=None, action=None)]

        class Client:
            async def get_messages(self, ent, offset_id=0, reverse=False, limit=1):
                self.limit = limit
                return [m for m in album + after if m.id > offset_id][:limit]

        reader = TelethonReader.__new__(TelethonReader)
        reader.client, reader.settle_seconds, reader._cache = Client(), 0, {"chan": object()}
        reader._now = lambda: datetime.now(timezone.utc)
        seen = []

        async def convert(msgs):
            seen.append([m.id for m in msgs])
            return [FetchedPost([m.id for m in msgs], "альбом")] if msgs else []

        reader._convert = convert
        posts, scanned = await reader.fetch_oldest("chan", 0, 5)
        self.assertEqual(seen[0], list(range(1, 9)))  # альбом из 8 файлов взят целиком, не пусто
        self.assertEqual((len(posts), scanned), (1, 8))


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
        self.old_delay = feed_engine.COPY_DELAY
        feed_engine.COPY_DELAY = 0
        feed_engine._copy_fails.clear()
        feed_engine._copy_active.clear()

        async def rewriter(text, style):
            self.rewrites.append(text)
            return "ПЕРЕПИСАНО"

        self.rewriter = rewriter

    async def asyncTearDown(self):
        feed_engine.COPY_DELAY = self.old_delay
        await self.db.close()

    async def test_backfill_from_first_to_last_in_batches_with_delay(self):
        await self.db.add_feed(ADMIN, "src", "Источник", 0, [-100], rewrite=False, credit=False, kind="copy")
        reader = HistoryReader([FetchedPost([i], f"пост {i}") for i in range(1, 8)])
        sleeps = []
        real_sleep = feed_engine.asyncio.sleep

        async def fake_sleep(delay, *a):
            sleeps.append(delay)
            await real_sleep(0)

        feed_engine.COPY_DELAY = 5
        feed_engine.asyncio.sleep = fake_sleep
        try:
            bot = MediaBot()
            n1 = await feed_engine.run_feeds_once(bot, self.db, self.cfg, reader, self.rewriter, kind="copy")
            self.assertEqual(n1, feed_engine.COPY_BATCH)  # «ещё есть работа»
            n2 = await feed_engine.run_feeds_once(bot, self.db, self.cfg, reader, self.rewriter, kind="copy")
            n3 = await feed_engine.run_feeds_once(bot, self.db, self.cfg, reader, self.rewriter, kind="copy")
        finally:
            feed_engine.asyncio.sleep = real_sleep
        self.assertEqual(n2, 2)
        self.assertEqual(n3, 0)
        self.assertEqual([kw["text"] for c, kw in bot.calls if c == "send" and kw["chat_id"] == -100],
                         [f"пост {i}" for i in range(1, 8)])  # строго по порядку
        self.assertEqual(sleeps.count(5), 7)  # пауза 5 с после каждого поста
        self.assertEqual([c[0] for c in reader.oldest_calls], [0, 5, 7])  # после 7 — пусто
        self.assertTrue(any("завершено" in kw.get("text", "") for c, kw in bot.calls if kw["chat_id"] == ADMIN))

    async def test_leading_service_messages_do_not_stall_backfill(self):
        # первые 7 id — служебные сообщения канала (постов нет), настоящие посты начинаются с id 8
        await self.db.add_feed(ADMIN, "src", "Источник", 0, [-100], rewrite=False, credit=False, kind="copy")
        reader = HistoryReader([FetchedPost([8], "первый настоящий"), FetchedPost([9], "второй")])
        bot = MediaBot()
        for _ in range(3):
            await feed_engine.run_feeds_once(bot, self.db, self.cfg, reader, self.rewriter, kind="copy")
        self.assertEqual([kw["text"] for c, kw in bot.calls if kw["chat_id"] == -100],
                         ["первый настоящий", "второй"])

    async def test_failed_post_blocks_queue_then_is_skipped(self):
        await self.db.add_feed(ADMIN, "src", "Источник", 0, [-100], rewrite=False, credit=False, kind="copy")
        reader = HistoryReader([FetchedPost([1], "первый"), FetchedPost([2], "второй")])

        class Broken(MediaBot):
            async def send_message(self, **kw):
                if kw["chat_id"] == -100 and kw["text"] == "первый":
                    raise RuntimeError("Forbidden")
                return await super().send_message(**kw)

        bot = Broken()
        for _ in range(2):
            await feed_engine.run_feeds_once(bot, self.db, self.cfg, reader, self.rewriter, kind="copy")
            self.assertEqual((await self.db.list_feeds(ADMIN, "copy"))[0]["last_msg_id"], 0)  # курсор стоит
        self.assertEqual([c for c in bot.calls if c[1].get("chat_id") == -100 and c[1].get("text") == "второй"], [])
        await feed_engine.run_feeds_once(bot, self.db, self.cfg, reader, self.rewriter, kind="copy")  # 3-я попытка
        self.assertEqual((await self.db.list_feeds(ADMIN, "copy"))[0]["last_msg_id"], 2)  # первый пропущен, второй вышел
        self.assertTrue(any(kw.get("text") == "второй" for c, kw in bot.calls if kw["chat_id"] == -100))

    async def test_clean_setting_applied_to_copy_feed_only(self):
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (400, 300), (10, 10, 10)).save(buf, "JPEG")
        fid = await self.db.add_feed(ADMIN, "src", "Источник", 0, [-100], rewrite=False, credit=False, kind="copy")
        await self.db.set_feed_clean(ADMIN, fid, {"pos": "br", "mode": "crop", "h": 10, "w": 30})
        self.assertEqual((await self.db.get_feed(ADMIN, fid, "copy"))["clean"]["mode"], "crop")
        bot = await self.run_once(FetchedPost([1], "t", [MediaItem("photo", buf.getvalue(), "p.jpg")]))
        self.assertEqual(Image.open(io.BytesIO(bot.calls[0][1]["photo"].data)).size, (400, 270))

    async def test_schedule_and_pause_do_not_apply(self):
        await self.db.add_feed(ADMIN, "src", "Источник", 0, [-100], rewrite=False, credit=False, kind="copy")
        await self.db.set_feed_schedule(ADMIN, mode="interval", every=360, paused_until=feed_engine.schedule.FOREVER)
        bot = MediaBot()
        await feed_engine.run_feeds_once(bot, self.db, self.cfg, HistoryReader([FetchedPost([1], "а"),
                                                                                 FetchedPost([2], "б")]),
                                         self.rewriter, kind="copy")
        self.assertEqual(len([c for c in bot.calls if c[1].get("chat_id") == -100]), 2)

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
        self.reader = FakeReader(latest=42)  # история копируется с нуля, а не с последнего поста

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
                         ("chan", 0, False, False, [-200, -100]))
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

    async def test_clean_screen(self):
        fid = await self.db.add_feed(ADMIN, "src", "Источник", 0, [-100], kind="copy")
        cb = FakeCb(f"cp:cl:{fid}:open", user_id=ADMIN)
        await copyfeeds.cb_clean(cb, self.db)
        self.assertIn("не убираю", cb.message.out[-1][1])
        for action in ("on", "pos:tl", "w:+", "h:-", "mode"):
            await copyfeeds.cb_clean(FakeCb(f"cp:cl:{fid}:{action}", user_id=ADMIN), self.db)
        c = (await self.db.get_feed(ADMIN, fid, "copy"))["clean"]
        self.assertEqual((c["pos"], c["w"], c["h"], c["mode"]), ("tl", 35, 7, "crop"))
        await copyfeeds.cb_clean(FakeCb(f"cp:cl:{fid}:on", user_id=ADMIN), self.db)
        self.assertIsNone((await self.db.get_feed(ADMIN, fid, "copy"))["clean"])  # переключатель выключает
        cb = FakeCb(f"cp:cl:{fid}:demo", user_id=ADMIN)
        cb.message.answer_photo = lambda *a, **k: _noop()
        await copyfeeds.cb_clean(cb, self.db)

    def test_views(self):
        text, markup = copyfeeds.main_view([], {})
        self.assertIn("последнее отбрасывается", text)
        self.assertEqual(SourceInfo(1, "a", "b").chat_id, 1)


if __name__ == "__main__":
    unittest.main()