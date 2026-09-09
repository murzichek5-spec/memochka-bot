"""SQLite-слой Мёмочки.

Здесь только хранение и выборка данных. Решения "писать ли сейчас" сюда не тащим —
иначе база быстро станет бизнес-логикой, а потом никто не поймёт, какого хуя SQL решает характер бота.
"""


from __future__ import annotations

import random
import re
from collections import Counter
from typing import Any

import aiosqlite

WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_]+(?:[-'][A-Za-zА-Яа-яЁё0-9_]+)*", re.UNICODE)


def extract_words(text: str) -> list[str]:
    return WORD_RE.findall(text or "")


# Основная БД: сообщения, настройки чатов, пользователи, персональная память и фото.
class Database:
    def __init__(self, path: str, *, max_messages_per_chat: int = 10000, max_conversation_log: int = 20000) -> None:
        self.path = path
        self.max_messages_per_chat = max_messages_per_chat
        self.max_conversation_log = max_conversation_log

    async def _ensure_column(self, db: aiosqlite.Connection, table: str, name: str, definition: str) -> None:
        cur = await db.execute(f"PRAGMA table_info({table})")
        cols = {str(row[1]) for row in await cur.fetchall()}
        if name not in cols:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    async def init(self) -> None:
        # CREATE IF NOT EXISTS — мягкая миграция. Старый bot.db можно оставить рядом:
        # новые таблицы/индексы появятся сами, без "удали базу и потеряй всю историю".
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA busy_timeout=5000")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id, id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_user ON messages(chat_id, user_id, id)")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS chat_settings (
                    chat_id INTEGER PRIMARY KEY,
                    auto_reply_chance REAL,
                    chaos REAL,
                    auto_enabled INTEGER NOT NULL DEFAULT 1
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS chat_words (
                    chat_id INTEGER NOT NULL,
                    normalized TEXT NOT NULL,
                    word TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 1,
                    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, normalized)
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_chat_words_count ON chat_words(chat_id, count DESC)")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS chat_users (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    username_norm TEXT,
                    display_name TEXT,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, user_id)
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_chat_users_username ON chat_users(chat_id, username_norm)")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_words (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    normalized TEXT NOT NULL,
                    word TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 1,
                    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, user_id, normalized)
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_user_words_count ON user_words(chat_id, user_id, count DESC)")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS conversation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    is_bot INTEGER NOT NULL DEFAULT 0,
                    text TEXT NOT NULL,
                    username TEXT,
                    display_name TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Автоматическая миграция базы 1.0.3: старые записи остаются, новые поля добавляются.
            for name, definition in (
                ("message_id", "INTEGER"),
                ("reply_to_message_id", "INTEGER"),
                ("reply_to_user_id", "INTEGER"),
                ("reply_to_username", "TEXT"),
                ("reply_to_display_name", "TEXT"),
                ("reply_to_text", "TEXT"),
            ):
                await self._ensure_column(db, "conversation_log", name, definition)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_conversation_log_chat ON conversation_log(chat_id, id)")
            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_message_id ON conversation_log(chat_id, message_id) WHERE message_id IS NOT NULL")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_memories (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    known_name TEXT NOT NULL DEFAULT '',
                    memory TEXT NOT NULL DEFAULT '',
                    updated_at DATETIME,
                    PRIMARY KEY (chat_id, user_id)
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_user_memories_chat ON user_memories(chat_id, updated_at DESC)")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS chat_photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    file_id TEXT NOT NULL,
                    file_unique_id TEXT NOT NULL,
                    caption TEXT,
                    username TEXT,
                    display_name TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_used_at DATETIME,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(chat_id, message_id)
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_chat_photos_chat ON chat_photos(chat_id, id DESC)")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            await db.commit()

    async def add_user_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        user_id: int,
        text: str,
        username: str | None,
        display_name: str | None,
        reply_to_message_id: int | None = None,
        reply_to_user_id: int | None = None,
        reply_to_username: str | None = None,
        reply_to_display_name: str | None = None,
        reply_to_text: str | None = None,
    ) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        username = username.strip() if username else None
        display_name = display_name.strip() if display_name else None
        words = extract_words(text)
        counts = Counter(w.casefold() for w in words if w)
        surfaces = {w.casefold(): w for w in words if w}

        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            cur = await db.execute(
                "SELECT 1 FROM conversation_log WHERE chat_id=? AND message_id=? LIMIT 1",
                (chat_id, message_id),
            )
            if await cur.fetchone():
                return False

            await db.execute("INSERT INTO messages(chat_id,user_id,text) VALUES(?,?,?)", (chat_id, user_id, text))
            await db.execute("""
                INSERT INTO conversation_log(
                    chat_id,user_id,is_bot,text,username,display_name,message_id,
                    reply_to_message_id,reply_to_user_id,reply_to_username,reply_to_display_name,reply_to_text
                ) VALUES(?,?,0,?,?,?,?,?,?,?,?,?)
            """, (
                chat_id, user_id, text, username, display_name, message_id,
                reply_to_message_id, reply_to_user_id, reply_to_username, reply_to_display_name, reply_to_text,
            ))
            await db.execute("""
                INSERT INTO chat_users(chat_id,user_id,username,username_norm,display_name,message_count)
                VALUES(?,?,?,?,?,1)
                ON CONFLICT(chat_id,user_id) DO UPDATE SET
                    username=COALESCE(excluded.username,chat_users.username),
                    username_norm=COALESCE(excluded.username_norm,chat_users.username_norm),
                    display_name=COALESCE(excluded.display_name,chat_users.display_name),
                    message_count=chat_users.message_count+1,
                    last_seen=CURRENT_TIMESTAMP
            """, (chat_id, user_id, username, username.casefold() if username else None, display_name))

            for norm, count in counts.items():
                surface = surfaces[norm]
                await db.execute("""
                    INSERT INTO chat_words(chat_id,normalized,word,count) VALUES(?,?,?,?)
                    ON CONFLICT(chat_id,normalized) DO UPDATE SET
                        word=excluded.word,count=chat_words.count+excluded.count,last_seen=CURRENT_TIMESTAMP
                """, (chat_id, norm, surface, count))
                await db.execute("""
                    INSERT INTO user_words(chat_id,user_id,normalized,word,count) VALUES(?,?,?,?,?)
                    ON CONFLICT(chat_id,user_id,normalized) DO UPDATE SET
                        word=excluded.word,count=user_words.count+excluded.count,last_seen=CURRENT_TIMESTAMP
                """, (chat_id, user_id, norm, surface, count))

            if self.max_messages_per_chat > 0:
                await db.execute("""
                    DELETE FROM messages WHERE chat_id=? AND id NOT IN (
                        SELECT id FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?
                    )
                """, (chat_id, chat_id, self.max_messages_per_chat))
            if self.max_conversation_log > 0:
                await db.execute("""
                    DELETE FROM conversation_log WHERE chat_id=? AND id NOT IN (
                        SELECT id FROM conversation_log WHERE chat_id=? ORDER BY id DESC LIMIT ?
                    )
                """, (chat_id, chat_id, self.max_conversation_log))
            await db.commit()
        return True

    async def add_bot_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        user_id: int,
        text: str,
        username: str | None,
        display_name: str | None,
        reply_to_message_id: int | None = None,
        reply_to_user_id: int | None = None,
        reply_to_username: str | None = None,
        reply_to_display_name: str | None = None,
        reply_to_text: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("""
                INSERT OR IGNORE INTO conversation_log(
                    chat_id,user_id,is_bot,text,username,display_name,message_id,
                    reply_to_message_id,reply_to_user_id,reply_to_username,reply_to_display_name,reply_to_text
                ) VALUES(?,?,1,?,?,?,?,?,?,?,?,?)
            """, (
                chat_id, user_id, text, username, display_name, message_id,
                reply_to_message_id, reply_to_user_id, reply_to_username, reply_to_display_name, reply_to_text,
            ))
            if self.max_conversation_log > 0:
                await db.execute("""
                    DELETE FROM conversation_log WHERE chat_id=? AND id NOT IN (
                        SELECT id FROM conversation_log WHERE chat_id=? ORDER BY id DESC LIMIT ?
                    )
                """, (chat_id, chat_id, self.max_conversation_log))
            await db.commit()

    async def get_recent_context(self, chat_id: int, *, limit: int = 30, exclude_message_id: int | None = None) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            if exclude_message_id is None:
                cur = await db.execute("""
                    SELECT user_id,is_bot,text,username,display_name,created_at,message_id,
                           reply_to_message_id,reply_to_user_id,reply_to_username,reply_to_display_name,reply_to_text
                    FROM conversation_log WHERE chat_id=? ORDER BY id DESC LIMIT ?
                """, (chat_id, limit))
            else:
                cur = await db.execute("""
                    SELECT user_id,is_bot,text,username,display_name,created_at,message_id,
                           reply_to_message_id,reply_to_user_id,reply_to_username,reply_to_display_name,reply_to_text
                    FROM conversation_log WHERE chat_id=? AND (message_id IS NULL OR message_id<>?) ORDER BY id DESC LIMIT ?
                """, (chat_id, exclude_message_id, limit))
            rows = await cur.fetchall()
        rows.reverse()
        keys = ["user_id","is_bot","text","username","display_name","created_at","message_id",
                "reply_to_message_id","reply_to_user_id","reply_to_username","reply_to_display_name","reply_to_text"]
        return [dict(zip(keys, row)) for row in rows]

    async def get_recent_human_targets(self, chat_id: int, *, limit: int = 12, minutes: int = 4) -> list[dict[str, Any]]:
        modifier = f"-{max(1, int(minutes))} minutes"
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""
                SELECT user_id,text,username,display_name,message_id,reply_to_message_id,reply_to_user_id,
                       reply_to_username,reply_to_display_name,reply_to_text,created_at
                FROM conversation_log
                WHERE chat_id=? AND is_bot=0 AND message_id IS NOT NULL AND created_at>=datetime('now', ?)
                ORDER BY id DESC LIMIT ?
            """, (chat_id, modifier, limit))
            rows = await cur.fetchall()
        rows.reverse()
        keys = ["user_id","text","username","display_name","message_id","reply_to_message_id","reply_to_user_id",
                "reply_to_username","reply_to_display_name","reply_to_text","created_at"]
        return [dict(zip(keys, row)) for row in rows]

    async def get_message_by_id(self, chat_id: int, message_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""
                SELECT user_id,is_bot,text,username,display_name,message_id
                FROM conversation_log WHERE chat_id=? AND message_id=? LIMIT 1
            """, (chat_id, message_id))
            row = await cur.fetchone()
        if not row:
            return None
        return dict(zip(["user_id","is_bot","text","username","display_name","message_id"], row))

    async def get_messages(self, chat_id: int, *, limit: int = 1000, user_id: int | None = None) -> list[str]:
        async with aiosqlite.connect(self.path) as db:
            if user_id is None:
                cur = await db.execute("SELECT text FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit))
            else:
                cur = await db.execute("SELECT text FROM messages WHERE chat_id=? AND user_id=? ORDER BY id DESC LIMIT ?", (chat_id, user_id, limit))
            rows = await cur.fetchall()
        rows.reverse()
        return [str(r[0]) for r in rows]

    async def get_word_counts(self, chat_id: int, *, user_id: int | None = None, limit: int = 300) -> dict[str, int]:
        async with aiosqlite.connect(self.path) as db:
            if user_id is None:
                cur = await db.execute("SELECT word,count FROM chat_words WHERE chat_id=? ORDER BY count DESC LIMIT ?", (chat_id, limit))
            else:
                cur = await db.execute("SELECT word,count FROM user_words WHERE chat_id=? AND user_id=? ORDER BY count DESC LIMIT ?", (chat_id, user_id, limit))
            rows = await cur.fetchall()
        return {str(w): int(c) for w, c in rows}

    async def get_word_stats(self, chat_id: int) -> tuple[int, int]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COALESCE(SUM(count),0),COUNT(*) FROM chat_words WHERE chat_id=?", (chat_id,))
            row = await cur.fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    async def get_user_by_username(self, chat_id: int, username: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""
                SELECT user_id,username,display_name,message_count FROM chat_users
                WHERE chat_id=? AND username_norm=? LIMIT 1
            """, (chat_id, username.lstrip('@').casefold()))
            row = await cur.fetchone()
        if not row:
            return None
        return {"user_id": int(row[0]), "username": row[1], "display_name": row[2], "message_count": int(row[3])}

    async def get_user_memory(self, chat_id: int, user_id: int) -> dict[str, Any]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""
                SELECT cu.username,cu.display_name,COALESCE(um.known_name,''),COALESCE(um.memory,''),um.updated_at
                FROM chat_users cu LEFT JOIN user_memories um
                  ON um.chat_id=cu.chat_id AND um.user_id=cu.user_id
                WHERE cu.chat_id=? AND cu.user_id=? LIMIT 1
            """, (chat_id, user_id))
            row = await cur.fetchone()
            if not row:
                cur = await db.execute("SELECT known_name,memory,updated_at FROM user_memories WHERE chat_id=? AND user_id=?", (chat_id, user_id))
                mem = await cur.fetchone()
                if mem:
                    return {"user_id": user_id, "username": None, "display_name": None, "known_name": mem[0] or "", "memory": mem[1] or "", "updated_at": mem[2]}
                return {"user_id": user_id, "username": None, "display_name": None, "known_name": "", "memory": "", "updated_at": None}
        return {"user_id": user_id, "username": row[0], "display_name": row[1], "known_name": row[2] or "", "memory": row[3] or "", "updated_at": row[4]}

    async def get_all_user_memories(self, chat_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""
                SELECT cu.user_id,cu.username,cu.display_name,cu.message_count,
                       COALESCE(um.known_name,''),COALESCE(um.memory,''),um.updated_at
                FROM chat_users cu LEFT JOIN user_memories um
                  ON um.chat_id=cu.chat_id AND um.user_id=cu.user_id
                WHERE cu.chat_id=?
                ORDER BY (COALESCE(um.memory,'')<>'' OR COALESCE(um.known_name,'')<>'') DESC,
                         COALESCE(um.updated_at,cu.last_seen) DESC, cu.message_count DESC
                LIMIT ?
            """, (chat_id, limit))
            rows = await cur.fetchall()
        keys = ["user_id","username","display_name","message_count","known_name","memory","updated_at"]
        return [dict(zip(keys, row)) for row in rows]

    async def set_user_memory(self, chat_id: int, user_id: int, *, known_name: str, memory: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT INTO user_memories(chat_id,user_id,known_name,memory,updated_at)
                VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id,user_id) DO UPDATE SET
                    known_name=excluded.known_name,memory=excluded.memory,updated_at=CURRENT_TIMESTAMP
            """, (chat_id, user_id, (known_name or "")[:200], (memory or "")[:8000]))
            await db.commit()

    async def count_user_memories(self, chat_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM user_memories WHERE chat_id=? AND (memory<>'' OR known_name<>'')", (chat_id,))
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def count_chat_users(self, chat_id: int) -> int:
        # Считаем только реально замеченных участников. Боты сюда обычно не попадают,
        # потому что их сообщения основной обработчик отбрасывает ещё до БД.
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM chat_users WHERE chat_id=?", (chat_id,))
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def get_top_chatter(self, chat_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""
                SELECT user_id,username,display_name,message_count
                FROM chat_users
                WHERE chat_id=? AND message_count>0
                ORDER BY message_count DESC,last_seen DESC
                LIMIT 1
            """, (chat_id,))
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "user_id": int(row[0]),
            "username": row[1],
            "display_name": row[2],
            "message_count": int(row[3]),
        }

    async def count_messages(self, chat_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM messages WHERE chat_id=?", (chat_id,))
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def add_photo(
        # Сохраняем только Telegram file_id + метаданные, а не сами мегабайты картинки.
        # UNIQUE(chat_id, message_id) защищает от дубля, если update обработался повторно.
        self,
        *,
        chat_id: int,
        message_id: int,
        user_id: int,
        file_id: str,
        file_unique_id: str,
        caption: str | None = None,
        username: str | None = None,
        display_name: str | None = None,
    ) -> None:
        caption = " ".join((caption or "").split()).strip() or None
        username = username.strip() if username else None
        display_name = display_name.strip() if display_name else None
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("""
                INSERT INTO chat_photos(
                    chat_id,message_id,user_id,file_id,file_unique_id,caption,username,display_name
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(chat_id,message_id) DO UPDATE SET
                    user_id=excluded.user_id,
                    file_id=excluded.file_id,
                    file_unique_id=excluded.file_unique_id,
                    caption=COALESCE(excluded.caption,chat_photos.caption),
                    username=COALESCE(excluded.username,chat_photos.username),
                    display_name=COALESCE(excluded.display_name,chat_photos.display_name)
            """, (chat_id,message_id,user_id,file_id,file_unique_id,caption,username,display_name))
            await db.commit()

    async def count_photos(self, chat_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM chat_photos WHERE chat_id=?", (chat_id,))
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def pick_photo_for_demotivator(self, chat_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""
                SELECT id,message_id,user_id,file_id,file_unique_id,caption,username,display_name,created_at,last_used_at,use_count
                FROM chat_photos
                WHERE chat_id=?
                ORDER BY use_count ASC, CASE WHEN last_used_at IS NULL THEN 0 ELSE 1 END ASC,
                         COALESCE(last_used_at,created_at) ASC, id DESC
                LIMIT 10
            """, (chat_id,))
            rows = await cur.fetchall()
        if not rows:
            return None
        row = random.choice(rows[:min(4, len(rows))])
        keys = ["id","message_id","user_id","file_id","file_unique_id","caption","username","display_name","created_at","last_used_at","use_count"]
        obj = dict(zip(keys,row))
        obj["id"] = int(obj["id"])
        obj["message_id"] = int(obj["message_id"])
        obj["user_id"] = int(obj["user_id"])
        obj["use_count"] = int(obj["use_count"])
        return obj

    async def mark_photo_used(self, photo_id: int) -> None:
        # Вызывается только после успешной отправки. Если Telegram отвалился, фотка
        # не считается использованной — логично, пользователь её всё равно не увидел.
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE chat_photos SET use_count=use_count+1,last_used_at=CURRENT_TIMESTAMP WHERE id=?",
                (photo_id,),
            )
            await db.commit()

    async def get_settings(self, chat_id: int, *, default_chance: float, default_chaos: float) -> dict[str, float | bool]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT auto_reply_chance,chaos,auto_enabled FROM chat_settings WHERE chat_id=?", (chat_id,))
            row = await cur.fetchone()
        if not row:
            return {"auto_reply_chance": default_chance, "chaos": default_chaos, "auto_enabled": True}
        return {
            "auto_reply_chance": default_chance if row[0] is None else float(row[0]),
            "chaos": default_chaos if row[1] is None else float(row[1]),
            "auto_enabled": bool(row[2]),
        }

    async def _ensure_settings(self, chat_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR IGNORE INTO chat_settings(chat_id) VALUES(?)", (chat_id,))
            await db.commit()

    async def set_chance(self, chat_id: int, chance: float) -> None:
        await self._ensure_settings(chat_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE chat_settings SET auto_reply_chance=? WHERE chat_id=?", (chance, chat_id))
            await db.commit()

    async def set_chaos(self, chat_id: int, chaos: float) -> None:
        await self._ensure_settings(chat_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE chat_settings SET chaos=? WHERE chat_id=?", (chaos, chat_id))
            await db.commit()

    async def set_auto_enabled(self, chat_id: int, enabled: bool) -> None:
        await self._ensure_settings(chat_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE chat_settings SET auto_enabled=? WHERE chat_id=?", (1 if enabled else 0, chat_id))
            await db.commit()

    async def clear_chat(self, chat_id: int) -> int:
        # /forget должен снести ВСЁ chat-scoped. Если добавишь новую таблицу памяти,
        # не забудь добавить её в список ниже, иначе получится "забыл, но не совсем", что за хуйня.
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM messages WHERE chat_id=?", (chat_id,))
            row = await cur.fetchone()
            count = int(row[0]) if row else 0
            for table in ("messages","conversation_log","chat_words","chat_users","user_words","user_memories","chat_photos"):
                await db.execute(f"DELETE FROM {table} WHERE chat_id=?", (chat_id,))
            await db.commit()
        return count
