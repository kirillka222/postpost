"""Сквозные сценарии хэндлеров с поддельными Message/CallbackQuery/Bot.

Если aiogram не установлен — подставляется заглушка (tests/aiogram_stub.py), поэтому
тесты проверяют нашу логику, но не совместимость с настоящим aiogram.
"""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import aiogram_stub

aiogram_stub.install_if_missing()

from aiogram.enums import ContentType  # noqa: E402

from bot import llm, publisher  # noqa: E402
from bot.config import Config  # noqa: E402
from bot.db import Database, today_str  # noqa: E402
from bot.handlers import billing, channels, common, mirror, posting  # noqa: E402
from bot.plans import FREE, PRO  # noqa: E402
from tests.test_core import FakeBot  # noqa: E402

USER = 1


class FakeState:
    def __init__(self):
        self.state = None
        self.data: dict = {}

    async def set_state(self, s):
        self.state = s

    async def set_data(self, d):
        self.data = dict(d)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.state, self.data = None, {}


class FakeMsg:
    def __init__(self, user_id=USER, text=None, caption=None, message_id=1, chat_id=None,
                 media_group_id=None, forward_origin=None, content_type=ContentType.TEXT,
                 successful_payment=None):
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=chat_id if chat_id is not None else user_id, type="private")
        self.text, self.caption, self.message_id = text, caption, message_id
        self.media_group_id, self.forward_origin = media_group_id, forward_origin
        self.content_type, self.successful_payment = content_type, successful_payment
        self.out: list[tuple] = []

    async def answer(self, text, reply_markup=None, **kw):
        self.out.append(("answer", text, reply_markup))

    async def reply(self, text, reply_markup=None, **kw):
        self.out.append(("reply", text, reply_markup))

    async def edit_text(self, text, reply_markup=None, **kw):
        self.out.append(("edit_text", text, reply_markup))

    async def edit_reply_markup(self, reply_markup=None, **kw):
        self.out.append(("edit_markup", None, reply_markup))

    def last(self):
        return self.out[-1]


class FakeCb:
    def __init__(self, data, message=None, user_id=USER):
        self.data, self.message = data, message or FakeMsg(user_id)
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[tuple] = []

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))


class FlowBot(FakeBot):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.members: dict = {}
        self.invoices: list[dict] = []

    async def me(self):
        return SimpleNamespace(id=999)

    async def get_chat_member(self, chat_id, user_id):
        return self.members[(chat_id, user_id)]

    async def send_invoice(self, **kw):
        self.invoices.append(kw)


def datas(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        publisher.recent_sends._items.clear()
        self.cfg = Config(bot_token="x", llm_url="http://127.0.0.1:1", pro_price_stars=150)
        self.db = Database(":memory:")
        await self.db.connect()
        await self.db.ensure_user(USER, "Europe/Moscow")
        await self.db.add_channel(USER, -100, "Первый", "first")
        await self.db.add_channel(USER, -200, "Второй", "")
        self.state = FakeState()
        self.bot = FlowBot()

    async def asyncTearDown(self):
        await self.db.close()

    async def draft(self, text="Привет", **kw):
        msg = FakeMsg(text=text, message_id=50, **kw)
        await posting.new_draft(msg, self.state, self.db, self.cfg)
        return msg


class PostingTests(Base):
    async def test_publish_now_copies_original(self):
        msg = await self.draft()
        self.assertIs(self.state.state, posting.Draft.menu)
        self.assertEqual(msg.last()[0], "reply")
        self.assertEqual(self.state.data["kind"], "text")
        self.assertEqual(self.state.data["selected"], [])

        menu = FakeMsg()
        await posting.toggle_channel(FakeCb("dch:-100", menu), self.state, self.db)
        await posting.toggle_channel(FakeCb("dch:-200", menu), self.state, self.db)
        await posting.toggle_channel(FakeCb("dch:-200", menu), self.state, self.db)  # снять
        self.assertEqual(self.state.data["selected"], [-100])
        self.assertIn("dch:-100", datas(menu.last()[2]))

        await posting.go(FakeCb("dgo:now", menu), self.bot, self.state, self.db, self.cfg)
        self.assertEqual(self.bot.calls[0], ("copy", {"chat_id": -100, "from_chat_id": USER, "message_id": 50}))
        self.assertIsNone(self.state.state)
        self.assertIn("Первый", menu.last()[1])

    async def test_foreign_channel_rejected(self):
        await self.draft()
        cb = FakeCb("dch:-999")
        await posting.toggle_channel(cb, self.state, self.db)
        self.assertTrue(cb.answers[-1][1])  # show_alert
        self.assertEqual(self.state.data["selected"], [])

    async def test_nothing_selected(self):
        await self.draft()
        cb = FakeCb("dgo:now")
        await posting.go(cb, self.bot, self.state, self.db, self.cfg)
        self.assertTrue(cb.answers[-1][1])
        self.assertEqual(self.bot.calls, [])

    async def test_single_channel_is_preselected(self):
        await self.db.remove_channel(USER, -200)
        await self.draft()
        self.assertEqual(self.state.data["selected"], [-100])

    async def test_no_channels(self):
        await self.db.remove_channel(USER, -100)
        await self.db.remove_channel(USER, -200)
        msg = await self.draft()
        self.assertIn("/channels", msg.last()[1])
        self.assertIsNone(self.state.state)

    async def test_media_has_no_ai_buttons_without_caption(self):
        msg = await self.draft(text=None, content_type=ContentType.PHOTO)
        self.assertEqual(self.state.data["kind"], "media")
        self.assertFalse(any(d.startswith("dai:") for d in datas(msg.last()[2])))

    async def test_album_warned_once(self):
        m1 = await self.draft(media_group_id="g1")
        m2 = await self.draft(media_group_id="g1")
        self.assertIn("Альбомы", m1.last()[1] if m1.out else "")
        self.assertEqual(m2.out, [])
        self.assertIsNone(self.state.state)

    async def test_ai_rewrite_flow(self):
        await self.draft("Исходный текст")
        menu = FakeMsg()
        await posting.toggle_channel(FakeCb("dch:-100", menu), self.state, self.db)
        mock = AsyncMock(return_value="Новый текст")
        with patch.object(llm, "rewrite", mock):
            await posting.ai_rewrite(FakeCb("dai:short", menu), self.state, self.db, self.cfg)
        self.assertEqual(mock.call_args.args[0], "Исходный текст")
        self.assertEqual(mock.call_args.kwargs["style"], "short")
        self.assertEqual(self.state.data["override"], "Новый текст")
        self.assertIn("Новый текст", menu.last()[1])
        self.assertIn("dai:orig", datas(menu.last()[2]))
        self.assertEqual(await self.db.rewrites_used(USER, today_str()), 1)

        await posting.go(FakeCb("dgo:now", menu), self.bot, self.state, self.db, self.cfg)
        self.assertEqual(self.bot.calls[0], ("send", {"chat_id": -100, "text": "Новый текст"}))

    async def test_ai_original_restored(self):
        await self.draft("Текст")
        menu = FakeMsg()
        with patch.object(llm, "rewrite", AsyncMock(return_value="Другой")):
            await posting.ai_rewrite(FakeCb("dai:normal", menu), self.state, self.db, self.cfg)
        await posting.ai_rewrite(FakeCb("dai:orig", menu), self.state, self.db, self.cfg)
        self.assertIsNone(self.state.data["override"])

    async def test_ai_quota_exhausted(self):
        await self.draft("Текст")
        for _ in range(FREE.rewrites_per_day):
            await self.db.try_consume_rewrite(USER, today_str(), 99)
        cb = FakeCb("dai:normal")
        mock = AsyncMock(return_value="x")
        with patch.object(llm, "rewrite", mock):
            await posting.ai_rewrite(cb, self.state, self.db, self.cfg)
        mock.assert_not_called()
        self.assertTrue(cb.answers[-1][1])
        self.assertIn("/pro", cb.answers[-1][0])

    async def test_ai_pro_quota_is_larger(self):
        await self.db.extend_pro(USER, 30)
        await self.draft("Текст")
        for _ in range(FREE.rewrites_per_day):
            await self.db.try_consume_rewrite(USER, today_str(), 99)
        with patch.object(llm, "rewrite", AsyncMock(return_value="ok")):
            await posting.ai_rewrite(FakeCb("dai:normal"), self.state, self.db, self.cfg)
        self.assertEqual(self.state.data["override"], "ok")

    async def test_ai_failure_refunds(self):
        await self.draft("Текст")
        menu = FakeMsg()
        with patch.object(llm, "rewrite", AsyncMock(side_effect=llm.LLMError("нет модели"))):
            await posting.ai_rewrite(FakeCb("dai:normal", menu), self.state, self.db, self.cfg)
        self.assertEqual(await self.db.rewrites_used(USER, today_str()), 0)
        self.assertIn("недоступна", menu.last()[1])
        self.assertIsNone(self.state.data["override"])

    async def test_schedule_flow(self):
        await self.draft("Отложенный")
        menu = FakeMsg()
        await posting.toggle_channel(FakeCb("dch:-100", menu), self.state, self.db)
        await posting.go(FakeCb("dgo:later", menu), self.bot, self.state, self.db, self.cfg)
        self.assertIs(self.state.state, posting.Draft.waiting_time)

        bad = FakeMsg(text="когда-нибудь")
        await posting.got_time(bad, self.state, self.db, self.cfg)
        self.assertIn("Не понял", bad.last()[1])
        self.assertIs(self.state.state, posting.Draft.waiting_time)

        ok = FakeMsg(text="через 2 часа")
        await posting.got_time(ok, self.state, self.db, self.cfg)
        self.assertIn("запланирован", ok.last()[1])
        self.assertIsNone(self.state.state)
        (post,) = await self.db.list_pending_posts(USER)
        self.assertAlmostEqual(post["run_at"], time.time() + 7200, delta=10)
        self.assertEqual((post["src_chat"], post["src_msg"], post["chat_ids"]), (USER, 50, [-100]))

    async def test_schedule_respects_queue_limit(self):
        await self.draft()
        for i in range(FREE.max_pending_posts):
            await self.db.add_post(USER, 1, i, "text", None, [-100], int(time.time()) + 999)
        await self.state.set_data({**self.state.data, "selected": [-100]})
        cb = FakeCb("dgo:later")
        await posting.go(cb, self.bot, self.state, self.db, self.cfg)
        self.assertTrue(cb.answers[-1][1])
        self.assertIs(self.state.state, posting.Draft.menu)

    async def test_cancel_draft(self):
        await self.draft()
        menu = FakeMsg()
        await posting.go(FakeCb("dgo:cancel", menu), self.bot, self.state, self.db, self.cfg)
        self.assertIsNone(self.state.state)

    async def test_queue_and_cancel(self):
        pid = await self.db.add_post(USER, 1, 5, "text", None, [-100, -200], int(time.time()) + 600)
        msg = FakeMsg()
        await posting.cmd_queue(msg, self.db, self.cfg)
        self.assertIn(f"#{pid}", msg.last()[1])
        self.assertIn("Первый, Второй", msg.last()[1])
        cb = FakeCb(f"pc:{pid}", FakeMsg())
        await posting.cb_cancel_post(cb, self.db, self.cfg)
        self.assertEqual(cb.message.last()[1], "Очередь пуста.")
        self.assertEqual(cb.answers[-1][0], "Отменено")

    def test_command_filter(self):
        self.assertFalse(posting._not_command(FakeMsg(text="/start")))
        self.assertTrue(posting._not_command(FakeMsg(text="обычный текст")))
        self.assertTrue(posting._not_command(FakeMsg(text=None)))


class ChannelsTests(Base):
    def forwarded(self, chat_id=-300, ctype="channel"):
        chat = SimpleNamespace(id=chat_id, type=ctype, title="Новый канал", username="newchan")
        return FakeMsg(forward_origin=SimpleNamespace(type="channel", chat=chat))

    def grant(self, chat_id=-300, bot_admin=True, user_status="creator"):
        self.bot.members[(chat_id, 999)] = SimpleNamespace(
            status="administrator" if bot_admin else "member", can_post_messages=bot_admin
        )
        self.bot.members[(chat_id, USER)] = SimpleNamespace(status=user_status, can_post_messages=None)

    async def test_add_channel_by_forward(self):
        self.grant()
        await self.db.remove_channel(USER, -200)
        msg = self.forwarded()
        await self.state.set_state(channels.AddChannel.waiting)
        await channels.got_channel(msg, self.bot, self.state, self.db, self.cfg)
        self.assertIn("подключён", msg.last()[1])
        self.assertIsNotNone(await self.db.get_channel(USER, -300))
        self.assertIsNone(self.state.state)

    async def test_rejects_non_admin_bot_and_user(self):
        self.grant(bot_admin=False)
        msg = self.forwarded()
        await channels.got_channel(msg, self.bot, self.state, self.db, self.cfg)
        self.assertIn("администратором", msg.last()[1])
        self.grant(user_status="member")
        msg = self.forwarded()
        await channels.got_channel(msg, self.bot, self.state, self.db, self.cfg)
        self.assertIn("только его администратор", msg.last()[1])
        self.assertIsNone(await self.db.get_channel(USER, -300))

    async def test_rejects_group(self):
        self.grant()
        msg = self.forwarded(ctype="supergroup")
        await channels.got_channel(msg, self.bot, self.state, self.db, self.cfg)
        self.assertIn("Это не канал", msg.last()[1])

    async def test_free_channel_limit_and_readd(self):
        await self.db.add_channel(USER, -150, "Третий")
        self.grant()
        msg = self.forwarded()
        await channels.got_channel(msg, self.bot, self.state, self.db, self.cfg)
        self.assertIn("/pro", msg.last()[1])
        self.assertEqual(await self.db.count_channels(USER), FREE.max_channels)
        # повторное подключение уже существующего канала лимит не трогает
        self.grant(chat_id=-100)
        msg = self.forwarded(chat_id=-100)
        await channels.got_channel(msg, self.bot, self.state, self.db, self.cfg)
        self.assertIn("подключён", msg.last()[1])

    async def test_unrecognized_input(self):
        msg = FakeMsg(text="какая-то фраза с пробелами")
        await channels.got_channel(msg, self.bot, self.state, self.db, self.cfg)
        self.assertIn("Не получилось определить", msg.last()[1])

    async def test_list_and_delete(self):
        msg = FakeMsg()
        await channels.cmd_channels(msg, self.db, self.cfg)
        self.assertIn("Первый (@first)", msg.last()[1])
        self.assertEqual(datas(msg.last()[2]), ["chdel:-100", "chdel:-200", "chadd"])
        cb = FakeCb("chdel:-100", FakeMsg())
        await channels.cb_delete(cb, self.db)
        self.assertEqual(await self.db.count_channels(USER), 1)


class MirrorTests(Base):
    async def create_rule(self):
        await mirror.new_start(FakeCb("mn:start"), self.state, self.db)
        await mirror.pick_source(FakeCb("mn:src:-100"), self.state, self.db)
        return FakeCb

    async def test_create_rule_flow(self):
        cb = FakeCb("mn:start")
        await mirror.new_start(cb, self.state, self.db)
        self.assertIs(self.state.state, mirror.MirrorNew.source)

        cb = FakeCb("mn:src:-100", FakeMsg())
        await mirror.pick_source(cb, self.state, self.db)
        self.assertIs(self.state.state, mirror.MirrorNew.targets)
        # источник не предлагается в получателях
        self.assertNotIn("mn:tg:-100", datas(cb.message.last()[2]))

        for data in ["mn:tg:-200", "mn:ai", "mn:st:short"]:
            await mirror.targets_step(FakeCb(data, FakeMsg()), self.state, self.db)
        self.assertEqual(self.state.data["targets"], [-200])
        self.assertTrue(self.state.data["ai"])
        self.assertEqual(self.state.data["style"], "short")

        done = FakeCb("mn:done", FakeMsg())
        await mirror.targets_step(done, self.state, self.db)
        (rule,) = await self.db.list_mirrors(USER)
        self.assertEqual((rule["source_chat_id"], rule["targets"], rule["rewrite"], rule["style"]),
                         (-100, [-200], True, "short"))
        self.assertIn("Первый → Второй", done.message.last()[1])
        self.assertIsNone(self.state.state)

    async def test_done_without_targets(self):
        await mirror.new_start(FakeCb("mn:start"), self.state, self.db)
        await mirror.pick_source(FakeCb("mn:src:-100", FakeMsg()), self.state, self.db)
        cb = FakeCb("mn:done", FakeMsg())
        await mirror.targets_step(cb, self.state, self.db)
        self.assertTrue(cb.answers[-1][1])
        self.assertEqual(await self.db.count_mirrors(USER), 0)

    async def test_needs_two_channels_and_respects_limit(self):
        await self.db.remove_channel(USER, -200)
        cb = FakeCb("mn:start")
        await mirror.new_start(cb, self.state, self.db)
        self.assertTrue(cb.answers[-1][1])
        await self.db.add_channel(USER, -200, "Второй")
        await self.db.add_mirror(USER, -100, [-200])  # лимит Free = 1
        cb = FakeCb("mn:start")
        await mirror.new_start(cb, self.state, self.db)
        self.assertIn("/pro", cb.answers[-1][0])

    async def test_manage_rule(self):
        mid = await self.db.add_mirror(USER, -100, [-200])
        for action in ["ai", "act"]:
            await mirror.manage_rule(FakeCb(f"mm:{action}:{mid}", FakeMsg()), self.db)
        (rule,) = await self.db.list_mirrors(USER)
        self.assertTrue(rule["rewrite"])
        self.assertFalse(rule["active"])
        cb = FakeCb(f"mm:del:{mid}", FakeMsg())
        await mirror.manage_rule(cb, self.db)
        self.assertEqual(await self.db.count_mirrors(USER), 0)
        cb = FakeCb(f"mm:ai:{mid}", FakeMsg())
        await mirror.manage_rule(cb, self.db)
        self.assertTrue(cb.answers[-1][1])  # правила уже нет

    async def test_channel_post_is_mirrored(self):
        await self.db.add_mirror(USER, -100, [-200])
        post = FakeMsg(text="Новость", chat_id=-100, message_id=5)
        await mirror.on_channel_post(post, self.bot, self.db, self.cfg)
        self.assertEqual(self.bot.calls, [("copy", {"chat_id": -200, "from_chat_id": -100, "message_id": 5})])

    async def test_service_message_ignored(self):
        await self.db.add_mirror(USER, -100, [-200])
        post = FakeMsg(chat_id=-100, content_type=ContentType.PINNED_MESSAGE)
        await mirror.on_channel_post(post, self.bot, self.db, self.cfg)
        self.assertEqual(self.bot.calls, [])

    async def test_media_caption_rewritten(self):
        await self.db.add_mirror(USER, -100, [-200], rewrite=True)
        post = FakeMsg(text=None, caption="Подпись", chat_id=-100, message_id=6,
                       content_type=ContentType.PHOTO)
        with patch.object(llm, "rewrite", AsyncMock(return_value="Новая подпись")):
            await mirror.on_channel_post(post, self.bot, self.db, self.cfg)
        self.assertEqual(self.bot.calls[0][1]["caption"], "Новая подпись")


class BillingAndCommonTests(Base):
    async def test_pro_screen_free_and_pro(self):
        msg = FakeMsg()
        await billing.cmd_pro(msg, self.db, self.cfg)
        self.assertIn("Ваш тариф: Free", msg.last()[1])
        self.assertEqual(datas(msg.last()[2]), ["buy:pro"])
        await self.db.extend_pro(USER, 30)
        msg = FakeMsg()
        await billing.cmd_pro(msg, self.db, self.cfg)
        self.assertIn("Ваш тариф: Pro", msg.last()[1])
        self.assertIn("активен до", msg.last()[1])

    async def test_invoice_in_stars(self):
        await billing.cb_buy(FakeCb("buy:pro"), self.bot, self.cfg)
        (inv,) = self.bot.invoices
        self.assertEqual(inv["currency"], "XTR")
        self.assertEqual(inv["provider_token"], "")
        self.assertEqual(inv["payload"], "pro:30")
        self.assertEqual(inv["prices"][0].amount, 150)

    async def test_pre_checkout_validation(self):
        good = SimpleNamespace(invoice_payload="pro:30", total_amount=150, answer=AsyncMock())
        await billing.pre_checkout(good, self.cfg)
        good.answer.assert_awaited_once_with(ok=True, error_message=None)
        wrong = SimpleNamespace(invoice_payload="pro:30", total_amount=1, answer=AsyncMock())
        await billing.pre_checkout(wrong, self.cfg)
        self.assertIs(wrong.answer.call_args.kwargs["ok"], False)

    async def test_successful_payment_activates_pro(self):
        pay = SimpleNamespace(invoice_payload="pro:30", telegram_payment_charge_id="ch1", total_amount=150)
        msg = FakeMsg(successful_payment=pay)
        await billing.paid(msg, self.db, self.cfg)
        self.assertIs(await self.db.get_plan(USER), PRO)
        self.assertIn("Спасибо", msg.last()[1])

    async def test_start_cancel_tz(self):
        msg = FakeMsg(user_id=77)
        await common.cmd_start(msg, self.db, self.cfg)
        self.assertEqual((await self.db.get_user(77))["tz"], "Europe/Moscow")
        self.assertIn("/channels", msg.last()[1])

        await self.state.set_state(posting.Draft.menu)
        await common.cmd_cancel(FakeMsg(), self.state)
        self.assertIsNone(self.state.state)

        msg = FakeMsg()
        await common.cmd_tz(msg, SimpleNamespace(args="+5"), self.db, self.cfg)
        self.assertEqual((await self.db.get_user(USER))["tz"], "Etc/GMT-5")
        msg = FakeMsg()
        await common.cmd_tz(msg, SimpleNamespace(args="Mars/Olympus"), self.db, self.cfg)
        self.assertIn("❌", msg.last()[1])
        self.assertEqual((await self.db.get_user(USER))["tz"], "Etc/GMT-5")
        msg = FakeMsg()
        await common.cmd_tz(msg, SimpleNamespace(args=None), self.db, self.cfg)
        self.assertIn("Etc/GMT-5", msg.last()[1])

    async def test_paysupport(self):
        cfg = Config(bot_token="x", support_contact="@owner")
        msg = FakeMsg()
        await billing.cmd_paysupport(msg, cfg)
        self.assertIn("@owner", msg.last()[1])

    def test_main_module_imports(self):
        import bot.main as main

        self.assertTrue(callable(main.run))
        self.assertTrue(any(c.command == "pro" for c in main.COMMANDS) if hasattr(main.COMMANDS[0], "command") else True)


if __name__ == "__main__":
    unittest.main()


class PostingUxTests(Base):
    async def start(self, selected=()):
        await self.draft("Пост")
        self.state.data["selected"] = list(selected)

    async def test_select_all_and_unselect(self):
        await self.start()
        menu = FakeMsg()
        await posting.toggle_all(FakeCb("dch:all", menu), self.state, self.db)
        self.assertEqual(sorted(self.state.data["selected"]), [-200, -100])
        self.assertTrue(any("Снять все" in b.text for r in menu.last()[2].inline_keyboard for b in r))
        self.assertIn("Выбрано каналов: 2 из 2", menu.last()[1])
        await posting.toggle_all(FakeCb("dch:all", menu), self.state, self.db)
        self.assertEqual(self.state.data["selected"], [])

    async def test_select_all_completes_partial_selection(self):
        await self.start(selected=[-100])
        await posting.toggle_all(FakeCb("dch:all", FakeMsg()), self.state, self.db)
        self.assertEqual(sorted(self.state.data["selected"]), [-200, -100])

    async def test_publish_now_to_all_channels(self):
        await self.start()
        menu = FakeMsg()
        await posting.toggle_all(FakeCb("dch:all", menu), self.state, self.db)
        await posting.go(FakeCb("dgo:now", menu), self.bot, self.state, self.db, self.cfg)
        self.assertEqual(sorted(kw["chat_id"] for n, kw in self.bot.calls if n == "copy"), [-200, -100])
        self.assertIn("Первый", menu.last()[1])
        self.assertIn("Второй", menu.last()[1])

    async def test_buttons_show_selected_count(self):
        await self.start(selected=[-100, -200])
        channels = await self.db.list_channels(USER)
        kb = posting.draft_kb(channels, [-100, -200], True, False)
        texts = [b.text for r in kb.inline_keyboard for b in r]
        self.assertIn("🚀 Сейчас (2)", texts)
        self.assertIn("⏰ Отложить (2)", texts)
        none = [b.text for r in posting.draft_kb(channels, [], True, False).inline_keyboard for b in r]
        self.assertIn("🚀 Сейчас", none)

    async def test_single_channel_has_no_select_all(self):
        kb = posting.draft_kb([{"chat_id": -1, "title": "Один"}], [], True, False)
        self.assertNotIn("dch:all", datas(kb))

    async def test_quick_time_buttons_shown_and_today_hidden_late_evening(self):
        from datetime import datetime, timezone
        morning = datetime(2026, 10, 11, 6, 0, tzinfo=timezone.utc)   # 09:00 Москва: 18:00 ещё впереди
        evening = datetime(2026, 10, 11, 17, 0, tzinfo=timezone.utc)  # 20:00 Москва: 18:00 уже прошло
        self.assertIn("dtm:t18", datas(posting.time_kb("Europe/Moscow", morning)))
        late = datas(posting.time_kb("Europe/Moscow", evening))
        self.assertNotIn("dtm:t18", late)
        self.assertTrue({"dtm:1h", "dtm:m9", "dtm:m18", "dtm:back"} <= set(late))

    async def test_schedule_via_quick_button_lists_channels(self):
        await self.start(selected=[-100, -200])
        await self.state.set_state(posting.Draft.waiting_time)
        cb = FakeCb("dtm:m9")
        await posting.quick_time(cb, self.state, self.db, self.cfg)
        (post,) = await self.db.list_pending_posts(USER)
        self.assertEqual(sorted(post["chat_ids"]), [-200, -100])
        text = cb.message.last()[1]
        self.assertIn("запланирован", text)
        self.assertIn("Первый", text)
        self.assertIn("Второй", text)
        self.assertIsNone(self.state.state)

    async def test_back_returns_to_draft_menu(self):
        await self.start(selected=[-100])
        await self.state.set_state(posting.Draft.waiting_time)
        cb = FakeCb("dtm:back")
        await posting.quick_time(cb, self.state, self.db, self.cfg)
        self.assertIs(self.state.state, posting.Draft.menu)
        self.assertEqual(self.state.data["selected"], [-100])
        self.assertEqual(await self.db.list_pending_posts(USER), [])

    async def test_unknown_quick_code_ignored(self):
        await self.start(selected=[-100])
        await self.state.set_state(posting.Draft.waiting_time)
        await posting.quick_time(FakeCb("dtm:zzz"), self.state, self.db, self.cfg)
        self.assertEqual(await self.db.list_pending_posts(USER), [])
        self.assertIs(self.state.state, posting.Draft.waiting_time)

    async def test_quick_buttons_schedule_the_labelled_time(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        for code, check in (("1h", lambda dt, now: abs((dt - now).total_seconds() - 3600) < 20),
                            ("m9", lambda dt, now: (dt.hour, dt.minute) == (9, 0) and dt.date() > now.date()),
                            ("m18", lambda dt, now: (dt.hour, dt.minute) == (18, 0) and dt.date() > now.date())):
            await self.db.cancel_all_posts(USER) if hasattr(self.db, "cancel_all_posts") else None
            await self.start(selected=[-100])
            await self.state.set_state(posting.Draft.waiting_time)
            before = {p["id"] for p in await self.db.list_pending_posts(USER)}
            await posting.quick_time(FakeCb("dtm:" + code), self.state, self.db, self.cfg)
            (new,) = [p for p in await self.db.list_pending_posts(USER) if p["id"] not in before]
            zone = ZoneInfo("Europe/Moscow")
            when = datetime.fromtimestamp(new["run_at"], tz=zone)
            self.assertTrue(check(when, datetime.now(zone)), (code, when))


class AlbumFloodTests(Base):
    """Пересланный альбом = несколько сообщений сразу; отвечать нужно один раз."""

    def grant(self):
        self.bot.members[(-300, 999)] = SimpleNamespace(status="administrator", can_post_messages=True)
        self.bot.members[(-300, USER)] = SimpleNamespace(status="creator")

    def album_item(self, gid="g1"):
        chat = SimpleNamespace(id=-300, type="channel", title="Новый", username="")
        return FakeMsg(forward_origin=SimpleNamespace(type="channel", chat=chat), media_group_id=gid)

    async def test_forwarded_album_answers_once_when_processed_concurrently(self):
        import asyncio

        self.grant()
        await self.db.remove_channel(USER, -200)
        await self.state.set_state(channels.AddChannel.waiting)
        msgs = [self.album_item() for _ in range(10)]
        await asyncio.gather(*(channels.got_channel(m, self.bot, self.state, self.db, self.cfg) for m in msgs))
        answered = [m for m in msgs if m.out]
        self.assertEqual(len(answered), 1)
        self.assertIn("подключён", answered[0].last()[1])

    async def test_next_album_is_handled_separately(self):
        self.grant()
        await self.db.remove_channel(USER, -200)
        await self.state.set_state(channels.AddChannel.waiting)
        first, second = self.album_item("a"), self.album_item("b")
        await channels.got_channel(first, self.bot, self.state, self.db, self.cfg)
        await channels.got_channel(second, self.bot, self.state, self.db, self.cfg)
        self.assertTrue(first.out and second.out)

    async def test_single_forward_is_not_affected(self):
        self.grant()
        await self.db.remove_channel(USER, -200)
        await self.state.set_state(channels.AddChannel.waiting)
        msg = self.album_item(gid=None)
        await channels.got_channel(msg, self.bot, self.state, self.db, self.cfg)
        self.assertIn("подключён", msg.last()[1])

    async def test_footer_album_without_text_does_not_spam(self):
        from bot.handlers import feeds as feeds_h

        msgs = []
        for _ in range(5):
            m = FakeMsg(text=None, media_group_id="foot")
            m.entities = m.caption = m.caption_entities = None
            msgs.append(m)
        for m in msgs:
            await feeds_h.footer_received(m, FakeState(), self.db)
        self.assertEqual(len([m for m in msgs if m.out]), 1)