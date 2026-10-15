"""Читатель чужих публичных каналов на Telethon (обычный аккаунт, не бот).

Почему так: Bot API показывает боту только те каналы, где он админ. Чужие публичные
каналы можно читать только от имени пользовательского аккаунта. Рекомендуется отдельный
аккаунт, а не личный.

Опрос вместо подписки на события: так не нужно вступать в каналы, и пропущенные за время
простоя посты догоняются по id. Класс принимает любого клиента с методами Telethon
(get_entity, get_messages, download_media), поэтому тестируется без сети.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .entities import Entity, from_telethon, to_telethon
from .feeds import MAX_ALBUM, MAX_MEDIA_BYTES, FetchedPost, MediaItem, SourceError, SourceInfo, is_private

log = logging.getLogger(__name__)

FETCH_LIMIT = 50
_DEFAULT_NAME = {"photo": "photo.jpg", "video": "video.mp4", "document": "file"}


class TelethonReader:
    def __init__(
        self,
        client: Any,
        settle_seconds: float = 20.0,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self.client = client
        # Свежие сообщения (младше N секунд) не трогаем: альбом приходит по частям
        self.settle_seconds = settle_seconds
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.me_id = 0  # id аккаунта-читателя (его бот делает админом канала)
        self.staging_chat = 0  # служебный канал (FEED_STAGING_CHAT): через него идут файлы больше 20 МБ
        self.premium = False  # у аккаунта-читателя есть Telegram Premium (нужен для премиум-эмодзи)
        self._cache: dict[str, Any] = {}  # ключ источника → сущность канала (не дёргаем API каждый опрос)

    @classmethod
    def create(cls, api_id: int, api_hash: str, session: str, session_string: str = "") -> "TelethonReader":
        from telethon import TelegramClient  # импорт здесь: без Telethon остальной бот работает

        if session_string:  # сессия из переменной окружения: файлы на диске не нужны
            from telethon.sessions import StringSession

            return cls(TelegramClient(StringSession(session_string), api_id, api_hash))
        return cls(TelegramClient(session, api_id, api_hash))

    async def start(self) -> bool:
        """True, если аккаунт авторизован (вход делается один раз: python -m bot.login)."""
        await self.client.connect()
        if not await self.client.is_user_authorized():
            return False
        try:
            me = await self.client.get_me()
            self.premium = bool(getattr(me, "premium", False))
            self.me_id = int(getattr(me, "id", 0) or 0)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось узнать, есть ли у аккаунта-читателя Premium: %s", exc)
        return True

    async def join_invite(self, link: str) -> None:
        """Вступает в канал по пригласительной ссылке (нужно, чтобы бот мог назначить аккаунт админом)."""
        from .feeds import normalize_username

        key = normalize_username(link)
        if not is_private(key):
            raise SourceError("Это не пригласительная ссылка.")
        await self._invite_entity(key[1:])

    async def is_member(self, chat_id: int) -> bool:
        """Аккаунт уже состоит в канале (тогда вступать по ссылке не нужно)."""
        try:
            await self.client.get_permissions(await self._peer(chat_id), "me")
            return True
        except Exception:  # noqa: BLE001 — не участник, канал не найден и т.п.
            return False

    async def stop(self) -> None:
        await self.client.disconnect()

    async def _invite_entity(self, invite_hash: str) -> Any:
        """Закрытый канал по инвайт-ссылке: если аккаунт уже внутри — берём канал, иначе вступаем."""
        from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
        from telethon.tl.types import ChatInviteAlready

        info = await self.client(CheckChatInviteRequest(invite_hash))
        if isinstance(info, ChatInviteAlready):
            return info.chat
        if not getattr(info, "broadcast", True):
            raise SourceError("Эта ссылка ведёт в группу, а не в канал (группы не поддерживаются).")
        try:
            updates = await self.client(ImportChatInviteRequest(invite_hash))
            chats = getattr(updates, "chats", None) or []
            if chats:
                return chats[0]
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ != "UserAlreadyParticipantError":
                raise  # FloodWait, отозванная ссылка и т.д.
        # Вступили (или уже были внутри), но в ответе нет канала: повторная проверка вернёт его
        info = await self.client(CheckChatInviteRequest(invite_hash))
        if isinstance(info, ChatInviteAlready):
            return info.chat
        raise SourceError("Не удалось вступить в канал по ссылке.")

    async def _entity(self, key: str) -> Any:
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        try:
            if is_private(key):
                ent = await self._invite_entity(key[1:])
            else:
                ent = await self.client.get_entity(key)
        except SourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "seconds", None) is not None:
                raise  # FloodWait должен дойти до движка как есть
            if is_private(key):
                raise SourceError(
                    "Не удалось открыть закрытый канал: ссылка недействительна или отозвана, "
                    "либо для вступления нужно одобрение заявки."
                ) from exc
            raise SourceError(f"Канал @{key} не найден или недоступен.") from exc
        self._cache[key] = ent
        return ent

    async def resolve(self, username: str) -> SourceInfo:
        ent = await self._entity(username)
        if not getattr(ent, "broadcast", False):
            raise SourceError("Это не канал (группы и чаты не поддерживаются).")
        return SourceInfo(chat_id=int(ent.id), username=username, title=getattr(ent, "title", username))

    async def latest_id(self, username: str) -> int:
        ent = await self._entity(username)
        msgs = await self.client.get_messages(ent, limit=1)
        return int(msgs[0].id) if msgs else 0

    async def fetch_oldest(self, username: str, after_id: int, limit: int = 5) -> tuple[list[FetchedPost], int]:
        """Следующие по порядку посты после after_id (от старых к новым) для копирования всей истории канала.

        Возвращает (посты, id последнего просмотренного сообщения). Служебные и пустые сообщения постами не
        становятся, но курсор через них обязан пройти: иначе пачка из одних «Канал создан» застопорит копирование.
        """
        ent = await self._entity(username)
        # берём с запасом на самый длинный альбом: пачка из одного большого альбома не должна выглядеть как «пусто»
        msgs = await self.client.get_messages(ent, offset_id=after_id, reverse=True, limit=limit + MAX_ALBUM)
        msgs = sorted(msgs, key=lambda m: m.id)
        cut = min(limit, len(msgs))
        while 0 < cut < len(msgs) and getattr(msgs[cut - 1], "grouped_id", None) is not None \
                and getattr(msgs[cut], "grouped_id", None) == msgs[cut - 1].grouped_id:
            cut += 1  # альбом на границе пачки забираем целиком
        msgs = msgs[:cut]
        now = self._now()
        settled = []
        for m in msgs:
            if (now - m.date).total_seconds() < self.settle_seconds:
                break
            settled.append(m)
        posts = await self._convert(settled)
        scanned = max([after_id, *[m.id for m in settled]])
        log.info("%s: копирование после id %d — прочитано сообщений %d, постов %d, дошли до id %d",
                 username if not is_private(username) else "закрытый канал", after_id, len(msgs), len(posts), scanned)
        return posts, scanned

    async def fetch_new(self, username: str, after_id: int) -> list[FetchedPost]:
        ent = await self._entity(username)
        msgs = await self.client.get_messages(ent, min_id=after_id, limit=FETCH_LIMIT)
        msgs = sorted(msgs, key=lambda m: m.id)
        now = self._now()
        settled = []
        for m in msgs:
            if (now - m.date).total_seconds() < self.settle_seconds:
                break  # дальше только более свежие
            settled.append(m)
        posts = await self._convert(settled)
        if msgs:
            log.info("%s: сообщений новее id %d — %d (из них «созревших» %d), публикуемых постов — %d",
                     username if not is_private(username) else "закрытый канал", after_id, len(msgs),
                     len(settled), len(posts))
        return posts

    # ---------- преобразование ----------
    @staticmethod
    def _kind(m: Any) -> Optional[str]:
        if getattr(m, "sticker", None):
            return None
        if getattr(m, "photo", None):
            return "photo"
        if getattr(m, "video", None) or getattr(m, "gif", None):
            return "video"
        if getattr(m, "video_note", None):
            return None
        if getattr(m, "document", None):
            return "document"
        return None

    @staticmethod
    def _plain(media: Any) -> Any:
        """Медиа без спойлера: копия объекта Telethon с выключенным флагом (иначе фото уйдёт закрытым)."""
        if getattr(media, "spoiler", False):
            import copy

            media = copy.copy(media)
            media.spoiler = False
        return media

    async def _download(self, m: Any, kind: str) -> Optional[MediaItem]:
        f = getattr(m, "file", None)
        if f is not None and (getattr(f, "size", 0) or 0) > MAX_MEDIA_BYTES:
            # Бот такой файл не загрузит. Не качаем: аккаунт отправит его по ссылке через служебный канал
            log.info("Сообщение %s: файл %d МБ больше лимита %d МБ, уйдёт через служебный канал", m.id,
                     (f.size or 0) // 2**20, MAX_MEDIA_BYTES // 2**20)
            name = getattr(f, "name", None) or (
                f"{kind}{f.ext}" if getattr(f, "ext", None) else _DEFAULT_NAME[kind])
            item = MediaItem(kind=kind, data=b"", filename=name, source=self._plain(getattr(m, "media", None)), big=True,
                             size=int(f.size or 0))
            if kind == "video":
                item.duration = int(getattr(f, "duration", 0) or 0)
                item.width = int(getattr(f, "width", 0) or 0)
                item.height = int(getattr(f, "height", 0) or 0)
            return item
        try:
            data = await self.client.download_media(m, file=bytes)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "seconds", None) is not None:
                raise
            log.warning("Не удалось скачать медиа сообщения %s: %s", m.id, exc)
            return None
        if not data:
            log.info("Сообщение %s: медиа не скачалось (пустой ответ)", m.id)
            return None
        name = getattr(f, "name", None) if f is not None else None
        if not name:
            ext = getattr(f, "ext", None) if f is not None else None
            name = f"{kind}{ext}" if ext else _DEFAULT_NAME[kind]
        item = MediaItem(kind=kind, data=bytes(data), filename=name, source=self._plain(getattr(m, "media", None)))
        if kind == "video" and f is not None:
            item.duration = int(getattr(f, "duration", 0) or 0)
            item.width = int(getattr(f, "width", 0) or 0)
            item.height = int(getattr(f, "height", 0) or 0)
            try:
                thumb = await self.client.download_media(m, file=bytes, thumb=-1)
                item.thumb = bytes(thumb) if thumb else None
            except Exception as exc:  # noqa: BLE001
                if getattr(exc, "seconds", None) is not None:
                    raise
                log.info("Сообщение %s: превью видео не скачалось: %s", m.id, exc)
        return item

    async def _convert(self, msgs: list[Any]) -> list[FetchedPost]:
        groups: list[list[Any]] = []
        for m in msgs:
            gid = getattr(m, "grouped_id", None)
            if gid is not None and groups and getattr(groups[-1][0], "grouped_id", None) == gid:
                groups[-1].append(m)
            else:
                groups.append([m])

        posts: list[FetchedPost] = []
        for group in groups:
            if getattr(group[0], "action", None):
                continue  # служебные сообщения канала
            text_msg = next((m for m in group if getattr(m, "message", None)), None)
            text = (text_msg.message if text_msg is not None else "") or ""
            entities = from_telethon(getattr(text_msg, "entities", None)) if text_msg is not None else []
            entities = [e for e in entities if e.type != "spoiler"]  # текст под спойлером публикуем открытым
            media: list[MediaItem] = []
            for m in group:
                kind = self._kind(m)
                if kind and len(media) < MAX_ALBUM:
                    item = await self._download(m, kind)
                    if item:
                        media.append(item)
            if not text.strip() and not media:
                log.info("Сообщения %s пропущены: нет ни текста, ни поддерживаемого медиа", [m.id for m in group])
                continue
            posts.append(FetchedPost(message_ids=[m.id for m in group], text=text, media=media,
                                     entities=entities))
        return posts

    # ---------- публикация от имени аккаунта (для премиум-эмодзи) ----------
    async def _peer(self, chat_id: int) -> Any:
        try:
            return await self.client.get_input_entity(chat_id)
        except ValueError:
            await self.client.get_dialogs()  # подтянуть каналы, где аккаунт админ, в кеш сессии
            try:
                return await self.client.get_input_entity(chat_id)
            except ValueError:
                raise SourceError(
                    "аккаунт-читатель не состоит в этом канале. Добавьте его администратором канала "
                    "с правом публикации сообщений"
                ) from None

    async def media_from(self, chat_id: int, message_ids: list[int]) -> list[MediaItem]:
        """Медиа сообщений канала для отправки по ссылке (без скачивания: подходит для файлов любого размера)."""
        msgs = await self.client.get_messages(await self._peer(chat_id), ids=message_ids)
        items: list[MediaItem] = []
        for m in msgs:
            kind = self._kind(m) if m is not None else None
            if kind is None:
                continue
            f = getattr(m, "file", None)
            name = getattr(f, "name", None) if f is not None else None
            if not name:
                ext = getattr(f, "ext", None) if f is not None else None
                name = f"{kind}{ext}" if ext else _DEFAULT_NAME[kind]
            item = MediaItem(kind=kind, data=b"", filename=name, source=self._plain(getattr(m, "media", None)),
                             size=int(getattr(f, "size", 0) or 0))
            if kind == "video" and f is not None:
                item.duration = int(getattr(f, "duration", 0) or 0)
                item.width = int(getattr(f, "width", 0) or 0)
                item.height = int(getattr(f, "height", 0) or 0)
            items.append(item)
        return items

    async def download_item(self, item: MediaItem) -> bytes:
        """Скачивает файл любого размера от имени аккаунта (бот ограничен 20 МБ): нужно для водяного знака."""
        data = await self.client.download_media(item.source, file=bytes)
        if not data:
            raise SourceError("файл не скачался")
        return bytes(data)

    async def delete(self, chat_id: int, message_ids: list[int]) -> None:
        """Удаляет сообщения аккаунта (чистка служебного канала после копирования)."""
        await self.client.delete_messages(await self._peer(chat_id), message_ids)

    async def send(self, chat_id: int, text: str, entities: list[Entity], media: list[Any]) -> list[int]:
        """Публикует пост в канал от имени аккаунта-читателя.

        Нужно, чтобы аккаунт был админом канала с правом публикации и имел Telegram Premium:
        так премиум-эмодзи сохраняются. Возвращает id отправленных сообщений.
        """
        from telethon.tl import types as tl_types

        tl_entities = to_telethon(entities, tl_types)

        def parse(t: str) -> tuple[str, list[Any]]:
            return t, (tl_entities if t == text else [])

        peer = await self._peer(chat_id)
        sent: list[Any] = []

        def video_kwargs(item: Any) -> dict[str, Any]:
            """Для видео, загружаемого заново: длительность, размер, превью и стриминг."""
            if item.kind != "video" or getattr(item, "source", None) is not None:
                return {}
            attr = tl_types.DocumentAttributeVideo(
                duration=item.duration, w=item.width, h=item.height, supports_streaming=True)
            kw: dict[str, Any] = {"attributes": [attr], "supports_streaming": True}
            if item.thumb:
                kw["thumb"] = io.BytesIO(item.thumb)
            return kw

        def as_file(item: Any) -> Any:
            if getattr(item, "source", None) is not None:
                return item.source  # по ссылке на исходный файл: все атрибуты видео сохраняются
            bio = io.BytesIO(item.data)
            bio.name = item.filename
            return bio

        if not media:
            if text.strip():
                sent.append(await self.client.send_message(peer, text, parse_mode=parse))
        else:
            caption = text if len(text) <= 1024 else ""
            kinds = {m.kind for m in media}
            if len(media) == 1 or ("document" in kinds and kinds != {"document"}):
                for n, item in enumerate(media):
                    sent.append(await self.client.send_file(
                        peer, as_file(item), caption=caption if n == 0 else "", parse_mode=parse,
                        force_document=item.kind == "document", **video_kwargs(item),
                    ))
            else:
                captions = [caption] + [""] * (len(media) - 1)
                res = await self.client.send_file(
                    peer, [as_file(m) for m in media], caption=captions, parse_mode=parse,
                    force_document=kinds == {"document"},
                )
                sent.extend(res if isinstance(res, list) else [res])
            if text and not caption:
                sent.append(await self.client.send_message(peer, text, parse_mode=parse))
        if tl_entities:
            def count(entities: Any) -> int:
                return sum(1 for e in entities or [] if type(e).__name__ == "MessageEntityCustomEmoji")

            want, kept = count(tl_entities), sum(count(getattr(m, "entities", None)) for m in sent)
            if want:  # диагностика: принял ли Telegram премиум-эмодзи (аккаунт без Premium их теряет)
                log.info("Аккаунт отправил в %s: премиум-эмодзи сохранилось %d из %d", chat_id, kept, want)
                if kept < want:
                    log.warning("Telegram не оставил премиум-эмодзи в посте: проверьте, что у аккаунта-читателя "
                                "есть Telegram Premium")
        return [int(m.id) for m in sent]