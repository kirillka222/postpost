"""SQLite-хранилище. Только стандартная библиотека: sqlite3 + asyncio.to_thread."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .plans import FREE, PRO, Plan

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    tz TEXT NOT NULL,
    pro_until INTEGER NOT NULL DEFAULT 0,
    rewrite_day TEXT NOT NULL DEFAULT '',
    rewrite_count INTEGER NOT NULL DEFAULT 0,
    limit_notified_day TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    UNIQUE(owner_id, chat_id)
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    src_chat INTEGER NOT NULL,
    src_msg INTEGER NOT NULL,
    kind TEXT NOT NULL,
    text_override TEXT,
    chat_ids TEXT NOT NULL,
    run_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_due ON posts(status, run_at);
CREATE TABLE IF NOT EXISTS mirrors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    source_chat_id INTEGER NOT NULL,
    rewrite INTEGER NOT NULL DEFAULT 0,
    style TEXT NOT NULL DEFAULT 'normal',
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_mirrors_src ON mirrors(source_chat_id);
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    title TEXT NOT NULL,
    last_msg_id INTEGER NOT NULL DEFAULT 0,
    rewrite INTEGER NOT NULL DEFAULT 1,
    style TEXT NOT NULL DEFAULT 'normal',
    credit INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(owner_id, username)
);
CREATE TABLE IF NOT EXISTS feed_prefs (
    owner_id INTEGER PRIMARY KEY,
    strip_links INTEGER NOT NULL DEFAULT 1,
    footer TEXT NOT NULL DEFAULT '',
    footer_entities TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS feed_targets (
    feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    chat_id INTEGER NOT NULL,
    PRIMARY KEY (feed_id, chat_id)
);
CREATE TABLE IF NOT EXISTS mirror_targets (
    mirror_id INTEGER NOT NULL REFERENCES mirrors(id) ON DELETE CASCADE,
    chat_id INTEGER NOT NULL,
    PRIMARY KEY (mirror_id, chat_id)
);
"""


def _d(row: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(row) if row is not None else None


class Database:
    def __init__(self, path: str):
        self._path = path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        def _open() -> sqlite3.Connection:
            if self._path != ":memory:":
                Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            if self._path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            ucols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
            for name, ddl in (
                ("username", "TEXT NOT NULL DEFAULT ''"), ("full_name", "TEXT NOT NULL DEFAULT ''"),
                ("last_seen", "INTEGER NOT NULL DEFAULT 0"), ("posts_manual", "INTEGER NOT NULL DEFAULT 0"),
                ("posts_feed", "INTEGER NOT NULL DEFAULT 0"), ("feeds_access", "INTEGER NOT NULL DEFAULT 0"),
                ("banned", "INTEGER NOT NULL DEFAULT 0"), ("ban_reason", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in ucols:  # данные для админки
                    conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(posts)")}
            if "payload" not in cols:  # снимок поста с премиум-эмодзи для публикации от имени аккаунта
                conn.execute("ALTER TABLE posts ADD COLUMN payload TEXT NOT NULL DEFAULT ''")
            if "options" not in cols:  # URL-кнопки и переключатели поста (звук, закрепление, защита)
                conn.execute("ALTER TABLE posts ADD COLUMN options TEXT NOT NULL DEFAULT ''")
            if "extra_msgs" not in cols:  # альбомы: остальные сообщения альбома кроме первого (src_msg)
                conn.execute("ALTER TABLE posts ADD COLUMN extra_msgs TEXT NOT NULL DEFAULT '[]'")
            conn.commit()
            return conn

        self._conn = await asyncio.to_thread(_open)

    async def close(self) -> None:
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    async def _run(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        conn = self._conn
        assert conn is not None, "Database.connect() не вызван"

        def _call() -> Any:
            try:
                result = fn(conn)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

        async with self._lock:
            return await asyncio.to_thread(_call)

    # ---------- пользователи ----------
    async def ensure_user(self, user_id: int, default_tz: str) -> dict:
        def fn(c: sqlite3.Connection) -> dict:
            c.execute(
                "INSERT OR IGNORE INTO users(user_id, tz, created_at) VALUES (?,?,?)",
                (user_id, default_tz, int(time.time())),
            )
            return dict(c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone())

        return await self._run(fn)

    async def touch_user(
        self, user_id: int, username: str, full_name: str, default_tz: str, now: Optional[int] = None
    ) -> dict:
        """Запоминает пользователя и время последней активности (для админки)."""
        now = int(time.time()) if now is None else now

        def fn(c: sqlite3.Connection) -> dict:
            c.execute("INSERT OR IGNORE INTO users(user_id, tz, created_at) VALUES (?,?,?)", (user_id, default_tz, now))
            c.execute(
                "UPDATE users SET username=?, full_name=?, last_seen=? WHERE user_id=?",
                ((username or "").lstrip("@"), full_name or "", now, user_id),
            )
            return dict(c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone())

        return await self._run(fn)

    async def set_feeds_access(self, user_id: int, allowed: bool) -> None:
        await self._run(
            lambda c: c.execute("UPDATE users SET feeds_access=? WHERE user_id=?", (int(allowed), user_id))
        )

    async def set_banned(self, user_id: int, banned: bool, reason: str = "") -> None:
        await self._run(
            lambda c: c.execute(
                "UPDATE users SET banned=?, ban_reason=? WHERE user_id=?",
                (int(banned), reason[:200] if banned else "", user_id),
            )
        )

    async def users_where(self, column: str, limit: int = 50) -> list[dict]:
        """Список пользователей с флагом (banned / feeds_access) — для команд /banned и /access."""
        assert column in ("banned", "feeds_access")
        return await self._run(
            lambda c: [
                dict(r) for r in c.execute(
                    f"SELECT * FROM users WHERE {column}=1 ORDER BY last_seen DESC, user_id LIMIT ?", (limit,)
                ).fetchall()
            ]
        )
    async def bump_published(self, user_id: int, manual: int = 0, feed: int = 0) -> None:
        await self._run(
            lambda c: c.execute(
                "UPDATE users SET posts_manual=posts_manual+?, posts_feed=posts_feed+? WHERE user_id=?",
                (manual, feed, user_id),
            )
        )

    # ---------- админка ----------
    async def admin_stats(self, now: int, day_start: int) -> dict:
        def fn(c: sqlite3.Connection) -> dict:
            def one(sql: str, *a: Any) -> int:
                return int(c.execute(sql, a).fetchone()[0] or 0)

            return {
                "users": one("SELECT COUNT(*) FROM users"),
                "new_today": one("SELECT COUNT(*) FROM users WHERE created_at>=?", day_start),
                "active_today": one("SELECT COUNT(*) FROM users WHERE last_seen>=?", day_start),
                "active_7d": one("SELECT COUNT(*) FROM users WHERE last_seen>=?", now - 7 * 86400),
                "pro": one("SELECT COUNT(*) FROM users WHERE pro_until>?", now),
                "channels": one("SELECT COUNT(*) FROM channels"),
                "pending": one("SELECT COUNT(*) FROM posts WHERE status='pending'"),
                "posts_manual": one("SELECT COALESCE(SUM(posts_manual),0) FROM users"),
                "posts_feed": one("SELECT COALESCE(SUM(posts_feed),0) FROM users"),
                "feeds": one("SELECT COUNT(*) FROM feeds WHERE active=1"),
                "banned": one("SELECT COUNT(*) FROM users WHERE banned=1"),
                "access": one("SELECT COUNT(*) FROM users WHERE feeds_access=1"),
            }

        return await self._run(fn)

    async def find_user(self, query: str) -> Optional[dict]:
        q = query.strip().lstrip("@")
        if not q:
            return None
        if q.lstrip("-").isdigit():
            return await self.get_user(int(q))
        return await self._run(
            lambda c: _d(c.execute("SELECT * FROM users WHERE lower(username)=lower(?)", (q,)).fetchone())
        )

    async def recent_users(self, limit: int = 10, offset: int = 0) -> list[dict]:
        return await self._run(
            lambda c: [
                dict(r) for r in c.execute(
                    "SELECT * FROM users ORDER BY last_seen DESC, created_at DESC LIMIT ? OFFSET ?", (limit, offset)
                ).fetchall()
            ]
        )

    async def user_overview(self, user_id: int, day: str) -> dict:
        def fn(c: sqlite3.Connection) -> dict:
            def one(sql: str, *a: Any) -> int:
                return int(c.execute(sql, a).fetchone()[0] or 0)

            channels = [dict(r) for r in c.execute(
                "SELECT chat_id, title, username FROM channels WHERE owner_id=? ORDER BY id", (user_id,)).fetchall()]
            feeds = [dict(r) for r in c.execute(
                "SELECT username, title, active FROM feeds WHERE owner_id=? ORDER BY id", (user_id,)).fetchall()]
            row = c.execute("SELECT rewrite_day, rewrite_count FROM users WHERE user_id=?", (user_id,)).fetchone()
            return {
                "channels": channels,
                "feeds": feeds,
                "pending": one("SELECT COUNT(*) FROM posts WHERE owner_id=? AND status='pending'", user_id),
                "scheduled_done": one("SELECT COUNT(*) FROM posts WHERE owner_id=? AND status='done'", user_id),
                "rewrites_today": int(row["rewrite_count"]) if row and row["rewrite_day"] == day else 0,
            }

        return await self._run(fn)

    async def set_tz(self, user_id: int, tz: str) -> None:
        await self._run(lambda c: c.execute("UPDATE users SET tz=? WHERE user_id=?", (tz, user_id)))

    async def get_user(self, user_id: int) -> Optional[dict]:
        return await self._run(
            lambda c: _d(c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone())
        )

    async def get_plan(self, user_id: int, now: Optional[int] = None) -> Plan:
        now = int(time.time()) if now is None else now
        user = await self.get_user(user_id)
        return PRO if user and user["pro_until"] > now else FREE

    async def extend_pro(self, user_id: int, days: int, now: Optional[int] = None) -> int:
        now = int(time.time()) if now is None else now

        def fn(c: sqlite3.Connection) -> int:
            row = c.execute("SELECT pro_until FROM users WHERE user_id=?", (user_id,)).fetchone()
            base = max(now, row["pro_until"]) if row else now
            until = base + days * 86400
            c.execute("UPDATE users SET pro_until=? WHERE user_id=?", (until, user_id))
            return until

        return await self._run(fn)

    async def revoke_pro(self, user_id: int) -> None:
        await self._run(lambda c: c.execute("UPDATE users SET pro_until=0 WHERE user_id=?", (user_id,)))

    async def try_consume_rewrite(self, user_id: int, day: str, limit: int) -> tuple[bool, int]:
        """Атомарно списывает одну попытку ИИ за сутки. Возвращает (успех, использовано)."""

        def fn(c: sqlite3.Connection) -> tuple[bool, int]:
            row = c.execute(
                "SELECT rewrite_day, rewrite_count FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            if row is None:
                return False, 0
            used = row["rewrite_count"] if row["rewrite_day"] == day else 0
            if used >= limit:
                return False, used
            c.execute(
                "UPDATE users SET rewrite_day=?, rewrite_count=? WHERE user_id=?",
                (day, used + 1, user_id),
            )
            return True, used + 1

        return await self._run(fn)

    async def refund_rewrite(self, user_id: int, day: str) -> None:
        await self._run(
            lambda c: c.execute(
                "UPDATE users SET rewrite_count=MAX(rewrite_count-1,0) "
                "WHERE user_id=? AND rewrite_day=?",
                (user_id, day),
            )
        )

    async def rewrites_used(self, user_id: int, day: str) -> int:
        user = await self.get_user(user_id)
        if not user or user["rewrite_day"] != day:
            return 0
        return user["rewrite_count"]

    async def claim_limit_notice(self, user_id: int, day: str) -> bool:
        """True, если сегодня ещё не говорили пользователю, что лимит ИИ исчерпан."""

        def fn(c: sqlite3.Connection) -> bool:
            row = c.execute(
                "SELECT limit_notified_day FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            if row is None or row["limit_notified_day"] == day:
                return False
            c.execute("UPDATE users SET limit_notified_day=? WHERE user_id=?", (day, user_id))
            return True

        return await self._run(fn)

    # ---------- каналы ----------
    async def add_channel(self, owner_id: int, chat_id: int, title: str, username: str = "") -> bool:
        def fn(c: sqlite3.Connection) -> bool:
            cur = c.execute(
                "INSERT OR IGNORE INTO channels(owner_id, chat_id, title, username) VALUES (?,?,?,?)",
                (owner_id, chat_id, title, username),
            )
            if cur.rowcount == 0:
                c.execute(
                    "UPDATE channels SET title=?, username=? WHERE owner_id=? AND chat_id=?",
                    (title, username, owner_id, chat_id),
                )
                return False
            return True

        return await self._run(fn)

    async def list_channels(self, owner_id: int) -> list[dict]:
        return await self._run(
            lambda c: [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM channels WHERE owner_id=? ORDER BY id", (owner_id,)
                ).fetchall()
            ]
        )

    async def get_channel(self, owner_id: int, chat_id: int) -> Optional[dict]:
        return await self._run(
            lambda c: _d(
                c.execute(
                    "SELECT * FROM channels WHERE owner_id=? AND chat_id=?", (owner_id, chat_id)
                ).fetchone()
            )
        )

    async def count_channels(self, owner_id: int) -> int:
        return await self._run(
            lambda c: c.execute(
                "SELECT COUNT(*) FROM channels WHERE owner_id=?", (owner_id,)
            ).fetchone()[0]
        )

    async def remove_channel(self, owner_id: int, chat_id: int) -> bool:
        def fn(c: sqlite3.Connection) -> bool:
            c.execute(
                "DELETE FROM mirrors WHERE owner_id=? AND source_chat_id=?", (owner_id, chat_id)
            )
            c.execute(
                "DELETE FROM mirror_targets WHERE chat_id=? AND mirror_id IN "
                "(SELECT id FROM mirrors WHERE owner_id=?)",
                (chat_id, owner_id),
            )
            c.execute(
                "DELETE FROM mirrors WHERE owner_id=? AND id NOT IN "
                "(SELECT DISTINCT mirror_id FROM mirror_targets)",
                (owner_id,),
            )
            c.execute(
                "DELETE FROM feed_targets WHERE chat_id=? AND feed_id IN "
                "(SELECT id FROM feeds WHERE owner_id=?)",
                (chat_id, owner_id),
            )
            c.execute(
                "DELETE FROM feeds WHERE owner_id=? AND id NOT IN "
                "(SELECT DISTINCT feed_id FROM feed_targets)",
                (owner_id,),
            )
            cur = c.execute(
                "DELETE FROM channels WHERE owner_id=? AND chat_id=?", (owner_id, chat_id)
            )
            return cur.rowcount > 0

        return await self._run(fn)

    # ---------- отложенные посты ----------
    async def add_post(
        self,
        owner_id: int,
        src_chat: int,
        src_msg: int,
        kind: str,
        text_override: Optional[str],
        chat_ids: list[int],
        run_at: int,
        extra_msgs: Optional[list[int]] = None,
        payload: Optional[dict] = None,
        options: Optional[dict] = None,
    ) -> int:
        def fn(c: sqlite3.Connection) -> int:
            cur = c.execute(
                "INSERT INTO posts(owner_id, src_chat, src_msg, kind, text_override, chat_ids, "
                "run_at, created_at, extra_msgs, payload, options) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (owner_id, src_chat, src_msg, kind, text_override, json.dumps(chat_ids),
                 run_at, int(time.time()), json.dumps(extra_msgs or []),
                 json.dumps(payload, ensure_ascii=False) if payload else "",
                 json.dumps(options, ensure_ascii=False) if options else ""),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

        return await self._run(fn)

    @staticmethod
    def _post(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["chat_ids"] = json.loads(d["chat_ids"])
        d["extra_msgs"] = json.loads(d.get("extra_msgs") or "[]")
        d["payload"] = json.loads(d["payload"]) if d.get("payload") else None
        d["options"] = json.loads(d["options"]) if d.get("options") else None
        return d

    async def claim_due_posts(self, now: int) -> list[dict]:
        def fn(c: sqlite3.Connection) -> list[dict]:
            rows = c.execute(
                "SELECT * FROM posts WHERE status='pending' AND run_at<=? AND owner_id NOT IN "
                "(SELECT user_id FROM users WHERE banned=1) ORDER BY run_at, id",
                (now,),
            ).fetchall()
            for r in rows:
                c.execute("UPDATE posts SET status='sending' WHERE id=?", (r["id"],))
            return [self._post(r) for r in rows]

        return await self._run(fn)

    async def finish_post(self, post_id: int, status: str, error: str = "") -> None:
        await self._run(
            lambda c: c.execute(
                "UPDATE posts SET status=?, error=? WHERE id=?", (status, error[:1000], post_id)
            )
        )

    async def recover_stuck_posts(self) -> int:
        """После перезапуска вернуть в очередь посты, которые не успели отправиться."""
        return await self._run(
            lambda c: c.execute("UPDATE posts SET status='pending' WHERE status='sending'").rowcount
        )

    async def list_pending_posts(self, owner_id: int, limit: int = 20) -> list[dict]:
        return await self._run(
            lambda c: [
                self._post(r)
                for r in c.execute(
                    "SELECT * FROM posts WHERE owner_id=? AND status='pending' "
                    "ORDER BY run_at, id LIMIT ?",
                    (owner_id, limit),
                ).fetchall()
            ]
        )

    async def count_pending_posts(self, owner_id: int) -> int:
        return await self._run(
            lambda c: c.execute(
                "SELECT COUNT(*) FROM posts WHERE owner_id=? AND status='pending'", (owner_id,)
            ).fetchone()[0]
        )

    async def cancel_post(self, owner_id: int, post_id: int) -> bool:
        return await self._run(
            lambda c: c.execute(
                "DELETE FROM posts WHERE id=? AND owner_id=? AND status='pending'",
                (post_id, owner_id),
            ).rowcount
            > 0
        )

    async def get_post(self, post_id: int) -> Optional[dict]:
        return await self._run(
            lambda c: (lambda r: self._post(r) if r else None)(
                c.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
            )
        )

    # ---------- нейропостинг: источники (чужие каналы) → наши каналы ----------
    @staticmethod
    def _feeds_with_targets(c: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
        out = []
        for r in rows:
            d = dict(r)
            for k in ("rewrite", "credit", "active"):
                d[k] = bool(d[k])
            d["targets"] = [
                t["chat_id"]
                for t in c.execute(
                    "SELECT chat_id FROM feed_targets WHERE feed_id=? ORDER BY chat_id", (d["id"],)
                ).fetchall()
            ]
            out.append(d)
        return out

    async def add_feed(
        self, owner_id: int, username: str, title: str, last_msg_id: int, targets: list[int],
        rewrite: bool = True, style: str = "normal", credit: bool = True,
    ) -> int:
        targets = list(dict.fromkeys(targets))
        if not targets:
            raise ValueError("Нужен хотя бы один канал-получатель")
        username = username if username.startswith("+") else username.lower()

        def fn(c: sqlite3.Connection) -> int:
            try:
                cur = c.execute(
                    "INSERT INTO feeds(owner_id, username, title, last_msg_id, rewrite, style, credit) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (owner_id, username, title, last_msg_id, int(rewrite), style, int(credit)),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Этот источник уже добавлен") from exc
            fid = cur.lastrowid
            assert fid is not None
            c.executemany(
                "INSERT INTO feed_targets(feed_id, chat_id) VALUES (?,?)", [(fid, t) for t in targets]
            )
            return fid

        return await self._run(fn)

    async def get_feed_prefs(self, owner_id: int) -> dict:
        """Настройки нейропостинга владельца: чистка ссылок и своя подпись (одна на все источники)."""
        def q(c: sqlite3.Connection) -> dict:
            r = c.execute("SELECT * FROM feed_prefs WHERE owner_id=?", (owner_id,)).fetchone()
            if r is None:
                return {"strip_links": True, "footer": "", "footer_entities": []}
            return {"strip_links": bool(r["strip_links"]), "footer": r["footer"],
                    "footer_entities": json.loads(r["footer_entities"] or "[]")}

        return await self._run(q)

    async def toggle_strip_links(self, owner_id: int) -> bool:
        def q(c: sqlite3.Connection) -> bool:
            c.execute("INSERT OR IGNORE INTO feed_prefs(owner_id) VALUES(?)", (owner_id,))
            c.execute("UPDATE feed_prefs SET strip_links=1-strip_links WHERE owner_id=?", (owner_id,))
            return bool(c.execute("SELECT strip_links FROM feed_prefs WHERE owner_id=?", (owner_id,)).fetchone()[0])

        return await self._run(q)

    async def set_footer(self, owner_id: int, text: str, entities: list[dict]) -> None:
        def q(c: sqlite3.Connection) -> None:
            c.execute("INSERT OR IGNORE INTO feed_prefs(owner_id) VALUES(?)", (owner_id,))
            c.execute("UPDATE feed_prefs SET footer=?, footer_entities=? WHERE owner_id=?",
                      (text, json.dumps(entities), owner_id))

        await self._run(q)

    async def list_feeds(self, owner_id: int) -> list[dict]:
        return await self._run(
            lambda c: self._feeds_with_targets(
                c, c.execute("SELECT * FROM feeds WHERE owner_id=? ORDER BY id", (owner_id,)).fetchall()
            )
        )

    async def list_active_feeds(self) -> list[dict]:
        return await self._run(
            lambda c: self._feeds_with_targets(
                c, c.execute(
                    "SELECT * FROM feeds WHERE active=1 AND owner_id NOT IN "
                    "(SELECT user_id FROM users WHERE banned=1) ORDER BY id"
                ).fetchall()
            )
        )

    async def count_feeds(self, owner_id: int) -> int:
        return await self._run(
            lambda c: c.execute("SELECT COUNT(*) FROM feeds WHERE owner_id=?", (owner_id,)).fetchone()[0]
        )

    async def update_feed_last(self, feed_id: int, last_msg_id: int) -> None:
        await self._run(
            lambda c: c.execute(
                "UPDATE feeds SET last_msg_id=MAX(last_msg_id, ?) WHERE id=?", (last_msg_id, feed_id)
            )
        )

    async def toggle_feed(self, owner_id: int, feed_id: int, field: str) -> Optional[bool]:
        """Переключает rewrite / credit / active. Возвращает новое значение или None, если правила нет."""
        if field not in ("rewrite", "credit", "active"):
            raise ValueError(field)

        def fn(c: sqlite3.Connection) -> Optional[bool]:
            row = c.execute(
                f"SELECT {field} FROM feeds WHERE id=? AND owner_id=?", (feed_id, owner_id)
            ).fetchone()
            if row is None:
                return None
            new = 0 if row[0] else 1
            c.execute(f"UPDATE feeds SET {field}=? WHERE id=?", (new, feed_id))
            return bool(new)

        return await self._run(fn)

    async def set_feed_style(self, owner_id: int, feed_id: int, style: str) -> bool:
        return await self._run(
            lambda c: c.execute(
                "UPDATE feeds SET style=? WHERE id=? AND owner_id=?", (style, feed_id, owner_id)
            ).rowcount
            > 0
        )

    async def delete_feed(self, owner_id: int, feed_id: int) -> bool:
        return await self._run(
            lambda c: c.execute("DELETE FROM feeds WHERE id=? AND owner_id=?", (feed_id, owner_id)).rowcount
            > 0
        )


def today_str(now: Optional[datetime] = None) -> str:
    """Сутки для лимита ИИ считаются по UTC, чтобы не зависеть от пояса пользователя."""
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d")