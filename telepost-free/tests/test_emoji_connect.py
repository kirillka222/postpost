"""Подключение Premium-аккаунта админом канала кнопкой 💎 (бот приглашает и назначает сам)."""
from __future__ import annotations

from types import SimpleNamespace

from tests.test_handlers_flow import Base, FakeCb, FlowBot, datas  # первым: ставит заглушку aiogram

from aiogram.exceptions import TelegramAPIError  # noqa: E402

from bot.feeds import SourceError  # noqa: E402
from bot.handlers import channels  # noqa: E402

ACCOUNT = 777


class RightsBot(FlowBot):
    """Бот с настраиваемыми правами; неизвестных участников «не находит», как настоящий Telegram."""

    def __init__(self, promote_rights=True, **kw):
        super().__init__(**kw)
        self.log: list[tuple] = []
        self.promote_rights = promote_rights

    async def get_chat_member(self, chat_id, user_id):
        if user_id == 999:
            return SimpleNamespace(status="administrator", can_post_messages=True,
                                   can_promote_members=self.promote_rights, can_invite_users=self.promote_rights)
        if (chat_id, user_id) in self.members:
            return self.members[(chat_id, user_id)]
        raise TelegramAPIError("user not found")

    async def create_chat_invite_link(self, chat_id, **kw):
        self.log.append(("link", chat_id, kw))
        return SimpleNamespace(invite_link="https://t.me/+AbCdEfGhIjKlMnOpQr")

    async def promote_chat_member(self, chat_id, user_id, **kw):
        self.log.append(("promote", chat_id, user_id, kw))

    async def revoke_chat_invite_link(self, chat_id, link):
        self.log.append(("revoke", chat_id, link))


class Reader:
    premium = True
    me_id = ACCOUNT

    def __init__(self, fail=None):
        self.joined = []
        self.fail = fail

    async def join_invite(self, link):
        if self.fail:
            raise self.fail
        self.joined.append(link)


class EmojiConnectTests(Base):
    async def setUp_bot(self, **kw):
        self.bot = RightsBot(**kw)

    def texts(self, cb):
        return [o[1] for o in cb.message.out]

    async def test_success_invites_promotes_and_revokes_link(self):
        await self.setUp_bot()
        reader, cb = Reader(), FakeCb("chemoji:-100")
        await channels.cb_emoji(cb, self.bot, self.db, reader)
        self.assertEqual([e[0] for e in self.bot.log], ["link", "promote", "revoke"])
        self.assertEqual(self.bot.log[0][2]["member_limit"], 1)
        self.assertEqual(reader.joined, ["https://t.me/+AbCdEfGhIjKlMnOpQr"])
        _, chat, uid, kw = self.bot.log[1]
        # только публикация; блокировку пользователей отключаем явно: иначе Bot API включит её сам (RIGHT_FORBIDDEN)
        self.assertEqual((chat, uid, kw), (-100, ACCOUNT, {"can_post_messages": True, "can_restrict_members": False}))
        self.assertIn("добавлен в канал", self.texts(cb)[-1])

    async def test_expired_one_time_link_is_retried_with_fresh_link(self):
        await self.setUp_bot()

        class Expired(Exception):
            pass

        Expired.__name__ = "InviteHashExpiredError"

        class FlakyReader(Reader):
            async def join_invite(self, link):
                if not self.joined and not getattr(self, "tried", False):
                    self.tried = True
                    raise Expired("The chat the user tried to join has expired and is not valid anymore")
                self.joined.append(link)

        reader, cb = FlakyReader(), FakeCb("chemoji:-100")
        await channels.cb_emoji(cb, self.bot, self.db, reader)
        links = [e for e in self.bot.log if e[0] == "link"]
        self.assertEqual([k[2]["member_limit"] if "member_limit" in k[2] else None for k in links], [1, None])
        self.assertEqual(len(reader.joined), 1)
        self.assertIn("добавлен в канал", self.texts(cb)[-1])

    async def test_still_expired_gives_readable_message(self):
        await self.setUp_bot()
        cb = FakeCb("chemoji:-100")
        await channels.cb_emoji(cb, self.bot, self.db, Reader(fail=RuntimeError("The chat ... has expired")))
        text = self.texts(cb)[-1]
        self.assertIn("недействительной", text)
        self.assertNotIn("caused by", text)

    async def test_account_already_inside_skips_join(self):
        await self.setUp_bot()

        class Member(Reader):
            async def is_member(self, chat_id):
                return True

        reader, cb = Member(), FakeCb("chemoji:-100")
        await channels.cb_emoji(cb, self.bot, self.db, reader)
        self.assertEqual([e[0] for e in self.bot.log], ["promote"])  # ссылку не создаём и не вступаем
        self.assertEqual(reader.joined, [])

    async def test_missing_bot_rights_gives_instructions_without_side_effects(self):
        await self.setUp_bot(promote_rights=False)
        cb = FakeCb("chemoji:-100")
        await channels.cb_emoji(cb, self.bot, self.db, Reader())
        self.assertEqual(self.bot.log, [])
        self.assertIn("Добавление администраторов", self.texts(cb)[-1])
        self.assertIn("Добавление участников", self.texts(cb)[-1])
        self.assertNotIn("Пригласительные", self.texts(cb)[-1])

    async def test_partial_rights_name_exactly_what_is_missing(self):
        await self.setUp_bot()
        real = self.bot.get_chat_member

        async def member(chat_id, user_id):
            m = await real(chat_id, user_id)
            if user_id == 999:  # как на скриншоте: админов добавлять можно, участников — нет
                m.can_invite_users = False
            return m
        self.bot.get_chat_member = member
        cb = FakeCb("chemoji:-100")
        await channels.cb_emoji(cb, self.bot, self.db, Reader())
        text = self.texts(cb)[-1]
        self.assertIn("Добавление участников", text)
        self.assertNotIn("Добавление администраторов", text)
        self.assertEqual(self.bot.log, [])

    async def test_right_forbidden_explains_manual_admin_instead_of_raw_error(self):
        await self.setUp_bot()

        async def forbidden(chat_id, user_id, **kw):
            raise channels.TelegramAPIError("Bad Request: RIGHT_FORBIDDEN")
        self.bot.promote_chat_member = forbidden
        cb = FakeCb("chemoji:-100")
        await channels.cb_emoji(cb, self.bot, self.db, Reader())
        text = self.texts(cb)[-1]
        self.assertIn("Публикация сообщений", text)
        self.assertIn("Причина: Telegram ответил", text)
        self.assertIn("Права бота сейчас", text)  # видно, что именно включено и выключено
        self.assertIn("Включены: Публикация сообщений", text)
        self.assertIn("на время подключения", text)
        self.assertIn("revoke", [e[0] for e in self.bot.log])  # ссылка всё равно отозвана

    async def test_manually_added_admin_without_post_right_gets_instructions(self):
        await self.setUp_bot()
        self.bot.members[(-100, ACCOUNT)] = SimpleNamespace(status="administrator", can_post_messages=False)
        cb = FakeCb("chemoji:-100")
        await channels.cb_emoji(cb, self.bot, self.db, Reader())
        self.assertIn("Публикация сообщений", self.texts(cb)[-1])
        self.assertIn("уже админ канала", self.texts(cb)[-1])
        self.assertEqual(self.bot.log, [])  # ничего не пытаемся менять

    async def test_only_missing_rights_are_listed(self):
        text = channels.rights_text(["Добавление участников"], "@flypost_robot")
        self.assertIn("Добавление участников", text)
        self.assertNotIn("Добавление администраторов", text)
        self.assertIn("@flypost_robot", text)
        self.assertNotIn("pilotpost", text.lower())
        self.assertIn("Включите боту (канал", channels.rights_text(["x"]))  # имя неизвестно: без «@»

    async def test_already_admin(self):
        await self.setUp_bot()
        self.bot.members[(-100, ACCOUNT)] = SimpleNamespace(status="administrator", can_post_messages=True)
        cb = FakeCb("chemoji:-100")
        await channels.cb_emoji(cb, self.bot, self.db, Reader())
        self.assertEqual(self.bot.log, [])
        self.assertIn("уже включены", self.texts(cb)[-1])

    async def test_join_failure_still_revokes_link_and_does_not_promote(self):
        await self.setUp_bot()
        cb = FakeCb("chemoji:-100")
        await channels.cb_emoji(cb, self.bot, self.db, Reader(fail=SourceError("ссылка недействительна")))
        self.assertEqual([e[0] for e in self.bot.log], ["link", "revoke"])
        self.assertIn("не смог вступить", self.texts(cb)[-1])

    async def test_flood_wait_message(self):
        await self.setUp_bot()

        class Flood(Exception):
            seconds = 60

        cb = FakeCb("chemoji:-100")
        await channels.cb_emoji(cb, self.bot, self.db, Reader(fail=Flood()))
        self.assertIn("ограничил", self.texts(cb)[-1])
        self.assertEqual([e[0] for e in self.bot.log], ["link", "revoke"])

    async def test_no_reader_or_not_premium(self):
        await self.setUp_bot()
        for reader in (None, SimpleNamespace(premium=False, me_id=ACCOUNT)):
            cb = FakeCb("chemoji:-100")
            await channels.cb_emoji(cb, self.bot, self.db, reader)
            self.assertIn("недоступна", self.texts(cb)[-1])
        self.assertEqual(self.bot.log, [])

    async def test_foreign_channel_is_refused(self):
        await self.setUp_bot()
        cb = FakeCb("chemoji:-555")
        await channels.cb_emoji(cb, self.bot, self.db, Reader())
        self.assertIn("не подключён", self.texts(cb)[-1])
        self.assertEqual(self.bot.log, [])

    async def test_neuroposting_has_no_emoji_button(self):
        from bot.handlers import feeds as feeds_h

        _, kb = feeds_h.feeds_view([], {}, None)
        self.assertFalse([d for d in datas(kb) if d.startswith(("fe:list", "chemoji"))])
        self.assertFalse(hasattr(feeds_h, "emoji_channels"))
        self.assertFalse(channels._can_emoji(SimpleNamespace(premium=True, me_id=0)))