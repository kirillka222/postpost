"""Тесты ядра (без Telegram и без aiogram): запуск `python -m unittest -v`."""
from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

from bot import llm, mirror_engine, publisher
from bot.config import Config, load_config
from bot.db import Database, today_str
from bot.plans import FREE, PRO
from bot.scheduler import run_due_posts
from bot.timeparse import TimeParseError, format_local, parse_when

# 2026-10-11 00:22 по Москве = 2026-10-10 21:22 UTC
NOW = datetime(2026, 10, 10, 21, 22, tzinfo=timezone.utc)
MSK = "Europe/Moscow"


def utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


class TimeParseTests(unittest.TestCase):
    def test_time_today_future(self):
        self.assertEqual(parse_when("18:30", NOW, MSK), utc(2026, 10, 11, 15, 30))

    def test_time_passed_goes_to_tomorrow(self):
        self.assertEqual(parse_when("00:10", NOW, MSK), utc(2026, 10, 11, 21, 10))

    def test_dot_separator(self):
        self.assertEqual(parse_when("18.30", NOW, MSK), utc(2026, 10, 11, 15, 30))

    def test_dayword(self):
        self.assertEqual(parse_when("завтра 09:00", NOW, MSK), utc(2026, 10, 12, 6, 0))
        self.assertEqual(parse_when("Послезавтра 10:15", NOW, MSK), utc(2026, 10, 13, 7, 15))
        self.assertEqual(parse_when("сегодня 23:59", NOW, MSK), utc(2026, 10, 11, 20, 59))

    def test_relative(self):
        self.assertEqual(parse_when("через 2 часа", NOW, MSK), utc(2026, 10, 10, 23, 22))
        self.assertEqual(parse_when("через 30 минут", NOW, MSK), utc(2026, 10, 10, 21, 52))
        self.assertEqual(parse_when("через час", NOW, MSK), utc(2026, 10, 10, 22, 22))
        self.assertEqual(parse_when("через 3 дня", NOW, MSK), utc(2026, 10, 13, 21, 22))

    def test_date_forms(self):
        want = utc(2026, 10, 12, 15, 30)
        self.assertEqual(parse_when("12.10 18:30", NOW, MSK), want)
        self.assertEqual(parse_when("12.10.2026 18:30", NOW, MSK), want)
        self.assertEqual(parse_when("12.10.26 18:30", NOW, MSK), want)
        self.assertEqual(parse_when("2026-10-12 18:30", NOW, MSK), want)

    def test_date_without_year_rolls_to_next_year(self):
        self.assertEqual(parse_when("01.10 10:00", NOW, MSK), utc(2027, 10, 1, 7, 0))

    def test_other_timezone(self):
        # в Екатеринбурге сейчас 02:22 11 октября, 10:00 — это 05:00 UTC
        self.assertEqual(parse_when("10:00", NOW, "Asia/Yekaterinburg"), utc(2026, 10, 11, 5, 0))

    def test_errors(self):
        for bad in ["", "вчера", "привет", "сегодня 00:10", "31.02 10:00", "через 2 месяца",
                    "25:00 завтра", "12.10.2020 10:00"]:
            with self.subTest(bad=bad):
                with self.assertRaises(TimeParseError):
                    parse_when(bad, NOW, MSK)

    def test_unknown_timezone(self):
        with self.assertRaises(TimeParseError):
            parse_when("18:30", NOW, "Mars/Olympus")

    def test_normalize_tz(self):
        from bot.timeparse import normalize_tz

        self.assertEqual(normalize_tz("Europe/Moscow"), "Europe/Moscow")
        self.assertEqual(normalize_tz("+3"), "Etc/GMT-3")
        self.assertEqual(normalize_tz("UTC-5"), "Etc/GMT+5")
        self.assertEqual(normalize_tz("utc+0"), "UTC")
        for bad in ["Mars/Olympus", "+20", "абракадабра"]:
            with self.assertRaises(TimeParseError):
                normalize_tz(bad)
        # Etc/GMT-3 действительно UTC+3: 10:00 по нему = 07:00 UTC
        self.assertEqual(parse_when("10:00", NOW, normalize_tz("+3")), utc(2026, 10, 11, 7, 0))

    def test_format_local(self):
        epoch = int(utc(2026, 10, 12, 15, 30).timestamp())
        self.assertEqual(format_local(epoch, MSK), "12.10.2026 18:30 (Europe/Moscow)")


class LLMTextTests(unittest.TestCase):
    def test_strip_think(self):
        self.assertEqual(llm.clean_output("<think>размышляю...</think>\n\nГотовый текст"), "Готовый текст")

    def test_unclosed_think_dropped(self):
        self.assertEqual(llm.clean_output("<think>обрыв без конца"), "")

    def test_strip_post_tags_fence_quotes(self):
        self.assertEqual(llm.clean_output("<post>\nТекст\n</post>"), "Текст")
        self.assertEqual(llm.clean_output("```\nТекст\n```"), "Текст")
        self.assertEqual(llm.clean_output("«Текст поста»"), "Текст поста")

    def test_inner_quotes_kept(self):
        self.assertEqual(llm.clean_output("«Раз» и «два»"), "«Раз» и «два»")

    def test_extract_urls(self):
        text = "Смотри https://example.com/a?b=1, и t.me/channel. Ещё https://example.com/a?b=1!"
        self.assertEqual(llm.extract_urls(text), ["https://example.com/a?b=1", "t.me/channel"])

    def test_restore_links(self):
        out = llm.restore_links("Читать: https://site.ru/x", "Подробности по ссылке")
        self.assertTrue(out.endswith("https://site.ru/x"))
        same = llm.restore_links("https://a.ru", "Вот https://a.ru тут")
        self.assertEqual(same, "Вот https://a.ru тут")


class _FakeOllama(BaseHTTPRequestHandler):
    mode = "ok"
    last_payload = None

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        type(self).last_payload = json.loads(self.rfile.read(length))
        if self.mode == "500":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"boom")
            return
        content = {
            "ok": "<think>думаю</think>Новый вариант поста",
            "empty": "<think>только мысли</think>",
            "nolink": "Короткая версия без ссылки",
        }[self.mode]
        body = json.dumps({"message": {"role": "assistant", "content": content}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # тишина в тестах
        pass


class LLMRewriteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), _FakeOllama)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        _FakeOllama.mode = "ok"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    async def test_rewrite_ok_and_payload(self):
        out = await llm.rewrite("Исходный пост", url=self.url, model="qwen3:4b", style="short")
        self.assertEqual(out, "Новый вариант поста")
        p = _FakeOllama.last_payload
        self.assertEqual(p["model"], "qwen3:4b")
        self.assertIs(p["think"], False)
        self.assertIs(p["stream"], False)
        self.assertIn("<post>\nИсходный пост\n</post>", p["messages"][1]["content"])
        self.assertIn("короче", p["messages"][1]["content"])

    async def test_links_restored(self):
        _FakeOllama.mode = "nolink"
        out = await llm.rewrite("Читать https://site.ru/p", url=self.url, model="m")
        self.assertIn("https://site.ru/p", out)

    async def test_http_error(self):
        _FakeOllama.mode = "500"
        with self.assertRaises(llm.LLMError):
            await llm.rewrite("текст", url=self.url, model="m")

    async def test_empty_answer(self):
        _FakeOllama.mode = "empty"
        with self.assertRaises(llm.LLMError):
            await llm.rewrite("текст", url=self.url, model="m")

    async def test_empty_input(self):
        with self.assertRaises(llm.LLMError):
            await llm.rewrite("   ", url=self.url, model="m")

    async def test_unreachable(self):
        with self.assertRaises(llm.LLMError):
            await llm.rewrite("текст", url="http://127.0.0.1:1", model="m", timeout=2)


class DBTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(":memory:")
        await self.db.connect()
        await self.db.ensure_user(1, MSK)

    async def asyncTearDown(self):
        await self.db.close()

    async def test_plan_free_then_pro_and_stacking(self):
        self.assertIs(await self.db.get_plan(1, now=1000), FREE)
        until = await self.db.extend_pro(1, 30, now=1000)
        self.assertEqual(until, 1000 + 30 * 86400)
        self.assertIs(await self.db.get_plan(1, now=2000), PRO)
        until2 = await self.db.extend_pro(1, 30, now=2000)  # продление прибавляется к остатку
        self.assertEqual(until2, 1000 + 60 * 86400)
        self.assertIs(await self.db.get_plan(1, now=until2 + 1), FREE)

    async def test_rewrite_quota(self):
        for i in range(1, 6):
            ok, used = await self.db.try_consume_rewrite(1, "2026-10-11", 5)
            self.assertEqual((ok, used), (True, i))
        self.assertEqual(await self.db.try_consume_rewrite(1, "2026-10-11", 5), (False, 5))
        self.assertEqual(await self.db.try_consume_rewrite(1, "2026-10-12", 5), (True, 1))  # новый день
        await self.db.refund_rewrite(1, "2026-10-12")
        self.assertEqual(await self.db.rewrites_used(1, "2026-10-12"), 0)
        self.assertEqual(await self.db.try_consume_rewrite(999, "2026-10-12", 5), (False, 0))

    async def test_limit_notice_once_per_day(self):
        self.assertTrue(await self.db.claim_limit_notice(1, "d1"))
        self.assertFalse(await self.db.claim_limit_notice(1, "d1"))
        self.assertTrue(await self.db.claim_limit_notice(1, "d2"))

    async def test_channels(self):
        self.assertTrue(await self.db.add_channel(1, -100, "Канал", "chan"))
        self.assertFalse(await self.db.add_channel(1, -100, "Новое имя", "chan"))
        self.assertEqual((await self.db.get_channel(1, -100))["title"], "Новое имя")
        self.assertEqual(await self.db.count_channels(1), 1)
        self.assertIsNone(await self.db.get_channel(2, -100))
        self.assertTrue(await self.db.remove_channel(1, -100))
        self.assertFalse(await self.db.remove_channel(1, -100))

    async def test_posts_lifecycle(self):
        a = await self.db.add_post(1, 5, 10, "text", None, [-100, -200], run_at=100)
        b = await self.db.add_post(1, 5, 11, "media", "новый", [-100], run_at=50)
        c = await self.db.add_post(1, 5, 12, "text", None, [-100], run_at=999)
        self.assertEqual(await self.db.count_pending_posts(1), 3)
        due = await self.db.claim_due_posts(now=200)
        self.assertEqual([p["id"] for p in due], [b, a])  # по времени
        self.assertEqual(due[1]["chat_ids"], [-100, -200])
        self.assertEqual(await self.db.claim_due_posts(now=200), [])  # повторно не берём
        self.assertEqual(await self.db.recover_stuck_posts(), 2)
        self.assertEqual(len(await self.db.claim_due_posts(now=200)), 2)
        await self.db.finish_post(a, "done")
        self.assertEqual((await self.db.get_post(a))["status"], "done")
        self.assertTrue(await self.db.cancel_post(1, c))
        self.assertFalse(await self.db.cancel_post(2, c))
        self.assertEqual(await self.db.list_pending_posts(1), [])

    async def test_mirrors(self):
        mid = await self.db.add_mirror(1, -1, [-2, -3, -2, -1], rewrite=True, style="short")
        (m,) = await self.db.list_mirrors(1)
        self.assertEqual((m["id"], m["targets"], m["rewrite"], m["style"]), (mid, [-3, -2], True, "short"))
        self.assertEqual(len(await self.db.mirrors_for_source(-1)), 1)
        self.assertEqual(await self.db.mirrors_for_source(-2), [])
        self.assertTrue(await self.db.set_mirror_active(1, mid, False))
        self.assertEqual(await self.db.mirrors_for_source(-1), [])
        self.assertFalse(await self.db.set_mirror_rewrite(2, mid, False))  # чужое
        self.assertTrue(await self.db.delete_mirror(1, mid))
        self.assertEqual(await self.db.count_mirrors(1), 0)
        with self.assertRaises(ValueError):
            await self.db.add_mirror(1, -1, [-1])

    async def test_remove_channel_cleans_mirrors(self):
        await self.db.add_mirror(1, -1, [-2, -3])
        await self.db.add_mirror(1, -4, [-2])
        await self.db.remove_channel(1, -2)  # получатель
        mirrors = await self.db.list_mirrors(1)
        self.assertEqual([(m["source_chat_id"], m["targets"]) for m in mirrors], [(-1, [-3])])
        await self.db.remove_channel(1, -1)  # источник
        self.assertEqual(await self.db.list_mirrors(1), [])

    def test_today_str(self):
        self.assertEqual(today_str(datetime(2026, 10, 11, 23, 59)), "2026-10-11")


class FakeBot:
    def __init__(self, fail_chats=(), flood_once=False):
        self.calls: list[tuple[str, dict]] = []
        self.fail_chats = set(fail_chats)
        self.flood_once = flood_once
        self._next = 1000

    def _answer(self, name, kw):
        self.calls.append((name, kw))
        if self.flood_once:
            self.flood_once = False

            class Flood(Exception):
                retry_after = 0

            raise Flood("flood")
        if kw.get("chat_id") in self.fail_chats:
            raise RuntimeError("Forbidden: bot was kicked")
        self._next += 1
        return SimpleNamespace(message_id=self._next)

    async def copy_message(self, **kw):
        return self._answer("copy", kw)

    async def send_message(self, **kw):
        return self._answer("send", kw)

    def names(self):
        return [c[0] for c in self.calls]


class PublisherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        publisher.recent_sends._items.clear()

    async def test_plain_copy_keeps_original(self):
        bot = FakeBot()
        ids = await publisher.publish_post(bot, -100, 5, 10, "text")
        self.assertEqual(bot.calls[0], ("copy", {"chat_id": -100, "from_chat_id": 5, "message_id": 10}))
        self.assertIn((-100, ids[0]), publisher.recent_sends)

    async def test_text_override_sends_message(self):
        bot = FakeBot()
        await publisher.publish_post(bot, -100, 5, 10, "text", "Новый текст")
        self.assertEqual(bot.calls, [("send", {"chat_id": -100, "text": "Новый текст"})])

    async def test_media_short_caption(self):
        bot = FakeBot()
        await publisher.publish_post(bot, -100, 5, 10, "media", "Подпись")
        self.assertEqual(bot.names(), ["copy"])
        self.assertEqual(bot.calls[0][1]["caption"], "Подпись")

    async def test_media_long_caption_splits(self):
        bot = FakeBot()
        ids = await publisher.publish_post(bot, -100, 5, 10, "media", "я" * 2000)
        self.assertEqual(bot.names(), ["copy", "send"])
        self.assertEqual(bot.calls[0][1]["caption"], "")
        self.assertEqual(len(ids), 2)

    async def test_flood_retry(self):
        bot = FakeBot(flood_once=True)
        ids = await publisher.publish_post(bot, -100, 5, 10, "text")
        self.assertEqual(len(bot.calls), 2)
        self.assertEqual(len(ids), 1)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        publisher.recent_sends._items.clear()
        self.db = Database(":memory:")
        await self.db.connect()
        await self.db.ensure_user(1, MSK)
        await self.db.add_channel(1, -100, "Первый")
        await self.db.add_channel(1, -200, "Второй")

    async def asyncTearDown(self):
        await self.db.close()

    async def test_publishes_due_only(self):
        due = await self.db.add_post(1, 5, 10, "text", None, [-100, -200], run_at=100)
        later = await self.db.add_post(1, 5, 11, "text", None, [-100], run_at=10_000)
        bot = FakeBot()
        self.assertEqual(await run_due_posts(bot, self.db, now=500), 1)
        targets = [kw["chat_id"] for n, kw in bot.calls if n == "copy"]
        self.assertEqual(targets, [-100, -200])
        self.assertEqual((await self.db.get_post(due))["status"], "done")
        self.assertEqual((await self.db.get_post(later))["status"], "pending")
        dm = [kw for n, kw in bot.calls if n == "send"]
        self.assertEqual(dm[0]["chat_id"], 1)
        self.assertIn("Первый", dm[0]["text"])

    async def test_partial_failure_is_done_with_error(self):
        pid = await self.db.add_post(1, 5, 10, "text", None, [-100, -200], run_at=100)
        bot = FakeBot(fail_chats={-200})
        await run_due_posts(bot, self.db, now=500)
        post = await self.db.get_post(pid)
        self.assertEqual(post["status"], "done")
        self.assertIn("Второй", post["error"])

    async def test_all_failed(self):
        pid = await self.db.add_post(1, 5, 10, "text", None, [-100], run_at=100)
        await run_due_posts(FakeBot(fail_chats={-100}), self.db, now=500)
        self.assertEqual((await self.db.get_post(pid))["status"], "failed")


class MirrorEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        publisher.recent_sends._items.clear()
        mirror_engine._last_notice.clear()
        self.cfg = Config(bot_token="x")
        self.db = Database(":memory:")
        await self.db.connect()
        await self.db.ensure_user(1, MSK)
        self.rewrites: list[tuple[str, str]] = []

        async def rewriter(text, style):
            self.rewrites.append((text, style))
            return "ПЕРЕПИСАНО"

        self.rewriter = rewriter

    async def asyncTearDown(self):
        await self.db.close()

    async def _post(self, bot, text="Оригинал", kind="text", message_id=7):
        return await mirror_engine.handle_channel_post(
            bot, self.db, self.cfg, chat_id=-1, message_id=message_id,
            text=text, kind=kind, rewriter=self.rewriter,
        )

    async def test_plain_mirror_copies_to_all_targets(self):
        await self.db.add_mirror(1, -1, [-2, -3])
        bot = FakeBot()
        self.assertEqual(await self._post(bot), 2)
        self.assertEqual([kw["chat_id"] for n, kw in bot.calls], [-3, -2])
        self.assertEqual(self.rewrites, [])

    async def test_rewrite_once_for_many_targets_and_quota(self):
        await self.db.add_mirror(1, -1, [-2, -3], rewrite=True, style="lively")
        bot = FakeBot()
        self.assertEqual(await self._post(bot), 2)
        self.assertEqual(self.rewrites, [("Оригинал", "lively")])
        self.assertEqual([n for n, _ in bot.calls], ["send", "send"])
        self.assertEqual(await self.db.rewrites_used(1, today_str()), 1)

    async def test_media_rewrites_caption(self):
        await self.db.add_mirror(1, -1, [-2], rewrite=True)
        bot = FakeBot()
        await self._post(bot, text="Подпись", kind="media")
        self.assertEqual(bot.calls[0][0], "copy")
        self.assertEqual(bot.calls[0][1]["caption"], "ПЕРЕПИСАНО")

    async def test_quota_exhausted_falls_back_and_notifies_once(self):
        await self.db.add_mirror(1, -1, [-2], rewrite=True)
        for _ in range(FREE.rewrites_per_day):
            await self.db.try_consume_rewrite(1, today_str(), FREE.rewrites_per_day)
        bot = FakeBot()
        await self._post(bot, message_id=1)
        await self._post(bot, message_id=2)
        self.assertEqual(self.rewrites, [])
        copies = [c for c in bot.calls if c[0] == "copy" and "caption" not in c[1]]
        self.assertEqual(len(copies), 2)  # оригиналы ушли как есть
        dms = [c for c in bot.calls if c[0] == "send" and c[1]["chat_id"] == 1]
        self.assertEqual(len(dms), 1)  # предупреждение — один раз в сутки

    async def test_pro_has_bigger_quota(self):
        await self.db.extend_pro(1, 30)
        await self.db.add_mirror(1, -1, [-2], rewrite=True)
        for _ in range(FREE.rewrites_per_day):
            await self.db.try_consume_rewrite(1, today_str(), PRO.rewrites_per_day)
        await self._post(FakeBot())
        self.assertEqual(len(self.rewrites), 1)

    async def test_rewriter_failure_copies_original_and_refunds(self):
        await self.db.add_mirror(1, -1, [-2], rewrite=True)

        async def broken(text, style):
            raise llm.LLMError("нет модели")

        self.rewriter = broken
        bot = FakeBot()
        self.assertEqual(await self._post(bot), 1)
        self.assertEqual(bot.calls[0][0], "copy")
        self.assertNotIn("caption", bot.calls[0][1])
        self.assertEqual(await self.db.rewrites_used(1, today_str()), 0)

    async def test_ignores_own_posts(self):
        await self.db.add_mirror(1, -1, [-2])
        publisher.recent_sends.add(-1, 7)
        bot = FakeBot()
        self.assertEqual(await self._post(bot, message_id=7), 0)
        self.assertEqual(bot.calls, [])

    async def test_one_failed_target_does_not_block_others(self):
        await self.db.add_mirror(1, -1, [-2, -3])
        bot = FakeBot(fail_chats={-2})
        self.assertEqual(await self._post(bot), 1)
        dm = [c for c in bot.calls if c[0] == "send" and c[1]["chat_id"] == 1]
        self.assertEqual(len(dm), 1)

    async def test_inactive_and_foreign_source_ignored(self):
        mid = await self.db.add_mirror(1, -1, [-2])
        await self.db.set_mirror_active(1, mid, False)
        bot = FakeBot()
        self.assertEqual(await self._post(bot), 0)
        self.assertEqual(bot.calls, [])


class ConfigTests(unittest.TestCase):
    def test_requires_token(self):
        with self.assertRaises(RuntimeError):
            load_config({})

    def test_defaults_and_overrides(self):
        cfg = load_config({"BOT_TOKEN": "t", "LLM_MODEL": "qwen3:8b", "PRO_PRICE_STARS": "99"})
        self.assertEqual((cfg.bot_token, cfg.llm_model, cfg.pro_price_stars), ("t", "qwen3:8b", 99))
        self.assertEqual(cfg.llm_url, "http://localhost:11434")


if __name__ == "__main__":
    unittest.main()
