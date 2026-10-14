"""Водяной знак: рендер, публикация (бот и аккаунт), настройки, нейропостинг."""
from __future__ import annotations

import io
import unittest

from tests.test_manual import AccountReader, DlBot, photo_msg  # первым: ставит заглушку aiogram

from bot import feed_engine, manual, mirror_engine, publisher, watermark  # noqa: E402
from bot.config import Config  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.feeds import FetchedPost, MediaItem  # noqa: E402
from bot.handlers import posting  # noqa: E402
from bot.handlers import watermark as wm_handler  # noqa: E402
from tests.test_feeds import FakeReader, MediaBot  # noqa: E402
from tests.test_handlers_flow import FakeCb, FakeMsg, FakeState  # noqa: E402

SPEC = {"text": "@канал", "pos": "br", "size": 30, "opacity": 80, "feeds": True}
HAS_PIL = True
try:
    from PIL import Image
except ImportError:  # pragma: no cover
    HAS_PIL = False


def jpeg(w=400, h=300, color=(20, 40, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG")
    return buf.getvalue()


class TextTests(unittest.TestCase):
    def test_clean_text_drops_emoji_and_limits(self):
        self.assertEqual(watermark.clean_text("  @канал 🔥 "), "@канал")
        self.assertEqual(watermark.clean_text("a\nb\nc"), "a\nb")
        self.assertEqual(len(watermark.clean_text("x" * 200)), watermark.MAX_TEXT)
        self.assertEqual(watermark.clean_text("🔥🔥"), "")

    def test_normalize_and_step(self):
        n = watermark.normalize({"size": 500, "opacity": 1, "pos": "zz"})
        self.assertEqual((n["size"], n["opacity"], n["pos"]), (80, 10, "br"))
        self.assertEqual(watermark.step(80, 10, watermark.SIZE_RANGE), 80)
        self.assertEqual(watermark.step(10, -10, watermark.SIZE_RANGE), 10)
        self.assertFalse(watermark.ready({"text": "🔥"}))
        self.assertTrue(watermark.ready(SPEC))
        self.assertFalse(watermark.ready(None))


@unittest.skipUnless(HAS_PIL, "нет Pillow")
class RenderTests(unittest.IsolatedAsyncioTestCase):
    def test_overlay_positions(self):
        for pos, corner in (("tl", (0, 0)), ("br", (399, 299))):
            ov = watermark.overlay(400, 300, {**SPEC, "pos": pos})
            self.assertEqual(ov.size, (400, 300))
            xs = [x for x in range(0, 400, 4) for y in range(0, 300, 4) if ov.getpixel((x, y))[3]]
            self.assertTrue(xs, pos)
            if pos == "tl":
                self.assertLess(max(xs), 200)
            else:
                self.assertGreater(min(xs), 150)

    def test_image_changes_pixels_and_stays_jpeg(self):
        src = jpeg()
        out = watermark.image_bytes(src, SPEC)
        self.assertEqual(Image.open(io.BytesIO(out)).format, "JPEG")
        self.assertEqual(Image.open(io.BytesIO(out)).size, (400, 300))
        self.assertNotEqual(out, src)
        a, b = Image.open(io.BytesIO(src)).convert("RGB"), Image.open(io.BytesIO(out)).convert("RGB")
        self.assertLess(sum(abs(x - y) for x, y in zip(a.getpixel((5, 5)), b.getpixel((5, 5)))), 12)  # вдали от знака
        self.assertNotEqual(a.crop((200, 220, 400, 300)).tobytes(), b.crop((200, 220, 400, 300)).tobytes())

    def test_sample(self):
        self.assertEqual(Image.open(io.BytesIO(watermark.sample(SPEC))).size, (1280, 720))

    async def test_apply_items(self):
        items = [MediaItem("photo", jpeg(), "p.jpg", source="SRC"), MediaItem("document", b"doc", "d.txt")]
        out = await watermark.apply_items(items, SPEC)
        self.assertNotEqual(out[0].data, items[0].data)
        self.assertIsNone(out[0].source)  # иначе аккаунт отправил бы исходный файл по ссылке
        self.assertEqual(out[1].data, b"doc")
        with self.assertRaises(watermark.WatermarkError):
            await watermark.apply_items([MediaItem("video", b"", "v.mp4", big=True)], SPEC)
        with self.assertRaises(watermark.WatermarkError):
            await watermark.apply_items(items, {**SPEC, "text": ""})
        with self.assertRaises(watermark.WatermarkError):
            await watermark.apply_items([MediaItem("photo", b"not an image", "p.jpg")], SPEC)

    @unittest.skipUnless(watermark.find_ffmpeg(), "нет ffmpeg")
    async def test_video(self):
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/t.mp4"
            subprocess.run([watermark.find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i",
                            "testsrc=size=320x180:rate=10:duration=1", "-pix_fmt", "yuv420p", path], check=True)
            with open(path, "rb") as fh:
                data = fh.read()
        out = await watermark.video_bytes(data, SPEC, "t.mp4")
        self.assertGreater(len(out), 500)
        self.assertEqual(out[4:8], b"ftyp")


@unittest.skipUnless(HAS_PIL, "нет Pillow")
class PublishTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        publisher.recent_sends._items.clear()
        self.payload = manual.build_payload([photo_msg(1, "подпись")], force=True)

    async def test_build_payload_force(self):
        self.assertIsNone(manual.build_payload([photo_msg(1, "без эмодзи")]))
        self.assertEqual(self.payload["media"][0]["file_id"], "big1")
        self.assertTrue(watermark.has_visual(self.payload))

    async def test_bot_publishes_watermarked_photo(self):
        class Bot(MediaBot, DlBot):
            async def download(self, file_id):
                return io.BytesIO(jpeg())

        bot, notes = Bot(), []
        await publisher.publish_post(bot, -100, 5, 1, "media", None, None, self.payload, None, notes,
                                     {"silent": True}, SPEC)
        name, kw = bot.calls[0]
        self.assertEqual(name, "photo")
        self.assertEqual(kw["caption"], "подпись")
        self.assertTrue(kw["disable_notification"])
        self.assertNotEqual(kw["photo"].data, jpeg())

    async def test_account_publishes_watermarked_photo(self):
        class Bot(DlBot):
            async def download(self, file_id):
                return io.BytesIO(jpeg())

        reader = AccountReader()
        await publisher.publish_post(Bot(), -100, 5, 1, "media", None, None, self.payload, reader, [], None, SPEC)
        _, text, _, media = reader.sent[0]
        self.assertEqual(text, "подпись")
        self.assertNotEqual(media[0].data, jpeg())

    async def test_failure_never_publishes_without_mark(self):
        bot = DlBot()  # download отдаёт не картинку
        with self.assertRaises(watermark.WatermarkError):
            await publisher.publish_post(bot, -100, 5, 1, "media", None, None, self.payload, None, [], None, SPEC)
        self.assertEqual(bot.names(), [])
        reader = AccountReader()
        with self.assertRaises(watermark.WatermarkError):  # аккаунт тоже не откатывается к копии без знака
            await publisher.publish_post(bot, -100, 5, 1, "media", None, None, self.payload, reader, [], None, SPEC)
        self.assertEqual((bot.names(), reader.sent), ([], []))
        with self.assertRaises(watermark.WatermarkError):
            await publisher.publish_post(bot, -100, 5, 1, "media", None, None, self.payload, None, [], None,
                                         {**SPEC, "text": ""})

    async def test_unsupported_attachment_notes_and_copies(self):
        bot, notes = DlBot(), []
        await publisher.publish_post(bot, -100, 5, 1, "media", None, None, None, None, notes, None, SPEC)
        self.assertEqual(bot.names(), ["copy"])
        self.assertIn("водяной знак не наложен", notes[0])

    async def test_without_watermark_nothing_changes(self):
        bot = DlBot()
        await publisher.publish_post(bot, -100, 5, 1, "media", None, None, self.payload, None, [])
        self.assertEqual(bot.names(), ["copy"])


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(":memory:")
        await self.db.connect()

    async def asyncTearDown(self):
        await self.db.close()

    async def test_db_roundtrip(self):
        self.assertEqual(await self.db.get_watermark(1), watermark.normalize(None))
        await self.db.set_watermark(1, text="@x", pos="tl", size=50, feeds=True)
        got = await self.db.get_watermark(1)
        self.assertEqual((got["text"], got["pos"], got["size"], got["feeds"]), ("@x", "tl", 50, True))
        with self.assertRaises(ValueError):
            await self.db.set_watermark(1, bogus=1)

    def test_view(self):
        text, kb = wm_handler.view(None)
        self.assertEqual(len(kb.inline_keyboard), 1)  # без текста — только «Задать текст»
        text, kb = wm_handler.view(SPEC)
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("wm:pos:tc", datas)
        self.assertIn("wm:feeds", datas)
        self.assertIn("@канал", text)

    async def test_handlers(self):
        msg, state = FakeMsg(user_id=1, text="  @мой 🔥 "), FakeState()
        await wm_handler.got_text(msg, state, self.db)
        self.assertEqual((await self.db.get_watermark(1))["text"], "@мой")
        cb = FakeCb("wm:pos:tl", user_id=1)
        await wm_handler.cb_change(cb, self.db)
        await wm_handler.cb_change(FakeCb("wm:size:+", user_id=1), self.db)
        await wm_handler.cb_change(FakeCb("wm:op:-", user_id=1), self.db)
        await wm_handler.cb_change(FakeCb("wm:feeds", user_id=1), self.db)
        got = await self.db.get_watermark(1)
        self.assertEqual((got["pos"], got["size"], got["opacity"], got["feeds"]), ("tl", 40, 50, True))
        await wm_handler.cb_change(FakeCb("wm:clear", user_id=1), self.db)
        got = await self.db.get_watermark(1)
        self.assertEqual((got["text"], got["feeds"]), ("", False))
        deny = FakeCb("wm:size:+", user_id=1)
        await wm_handler.cb_change(deny, self.db)
        self.assertTrue(deny.answers[0][1])  # без текста менять нечего

    async def test_empty_text_rejected(self):
        msg, state = FakeMsg(user_id=1, text="🔥"), FakeState()
        await wm_handler.got_text(msg, state, self.db)
        self.assertIn("❌", msg.out[-1][1])
        self.assertEqual((await self.db.get_watermark(1))["text"], "")


class DraftTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(":memory:")
        await self.db.connect()
        await self.db.ensure_user(1, "UTC")
        await self.db.add_channel(1, -100, "Канал", "")

    async def asyncTearDown(self):
        await self.db.close()

    def test_button_only_for_media(self):
        def has(kb):
            return any(b.callback_data == "dop:watermark" for row in kb.inline_keyboard for b in row)

        self.assertFalse(has(posting.draft_kb([], [])))
        self.assertTrue(has(posting.draft_kb([], [], media=True)))

    async def test_toggle_needs_text_and_media(self):
        state = FakeState()
        photo = manual.build_payload([photo_msg(1)], force=True)
        await state.set_data({"selected": [-100], "options": {}, "wm": photo, "kind": "media"})
        await state.set_state(posting.Draft.menu)
        cb = FakeCb("dop:watermark", user_id=1)
        await posting.toggle_option(cb, state, self.db)
        self.assertTrue(cb.answers[0][1])  # текст не задан
        await self.db.set_watermark(1, text="@x")
        await posting.toggle_option(FakeCb("dop:watermark", user_id=1), state, self.db)
        self.assertTrue((await state.get_data())["options"]["watermark"])
        self.assertIs(posting.post_payload(await state.get_data()), photo)
        await posting.toggle_option(FakeCb("dop:watermark", user_id=1), state, self.db)
        self.assertIsNone(posting.post_payload(await state.get_data()))


@unittest.skipUnless(HAS_PIL, "нет Pillow")
class FeedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        publisher.recent_sends._items.clear()
        mirror_engine._last_notice.clear()
        self.cfg = Config(bot_token="x")
        self.db = Database(":memory:")
        await self.db.connect()
        await self.db.ensure_user(1, "UTC")
        await self.db.add_feed(1, "chan", "Канал", 0, [-100], rewrite=False, credit=False)

    async def asyncTearDown(self):
        await self.db.close()

    async def run_once(self, post):
        bot = MediaBot()
        await feed_engine.run_feeds_once(bot, self.db, self.cfg, FakeReader([post]), None, pause=0)
        return bot

    async def test_applied_when_enabled(self):
        src = jpeg()
        await self.db.set_watermark(1, text="@x", feeds=True)
        bot = await self.run_once(FetchedPost([1], "t", [MediaItem("photo", src, "p.jpg")]))
        self.assertEqual(bot.calls[0][0], "photo")
        self.assertNotEqual(bot.calls[0][1]["photo"].data, src)

    async def test_untouched_when_disabled(self):
        src = jpeg()
        await self.db.set_watermark(1, text="@x", feeds=False)
        bot = await self.run_once(FetchedPost([1], "t", [MediaItem("photo", src, "p.jpg")]))
        self.assertEqual(bot.calls[0][1]["photo"].data, src)

    async def test_failure_skips_post(self):
        await self.db.set_watermark(1, text="@x", feeds=True)
        bot = await self.run_once(FetchedPost([1], "t", [MediaItem("photo", b"broken", "p.jpg")]))
        self.assertEqual([c for c in bot.calls if c[1].get("chat_id") == -100], [])  # в канал ничего не ушло
        self.assertTrue(any(c[1].get("chat_id") == 1 for c in bot.calls))  # владелец получил предупреждение
        self.assertEqual((await self.db.list_feeds(1))[0]["last_msg_id"], 1)  # пост не зацикливается


if __name__ == "__main__":
    unittest.main()