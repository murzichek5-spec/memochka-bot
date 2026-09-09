"""Групповая память: старые события, повторяющиеся фразы и локальные мемы.

Это не биография конкретного человека. Здесь хранится культура беседы — то, к чему
бот потом может естественно вернуться через несколько сообщений или дней.
"""


from __future__ import annotations

import math
import re
from dataclasses import dataclass

import aiosqlite

from db import extract_words

LOW_VALUE_SINGLE = {"да","нет","ага","угу","ок","окей","пон","понял","поняла","хз","лол","ахах","ахаха","ахахах","бля","блять","спс","спасибо","ку","привет"}
EVENT_HINTS = {
    "опять","снова","сломал","сломала","сломалось","упал","упала","упало","купил","купила",
    "потерял","потеряла","забыл","забыла","опоздал","опоздала","уволили","уволился","уволилась",
    "сдал","сдала","провалил","провалила","удалил","удалила","забанили","заблокировали",
    "родил","родила","родились","женился","вышла","развелся","развелась","переехал","переехала",
    "выиграл","выиграла","проиграл","проиграла","сломали","починил","починила",
}
EMOTION_HINTS = {"ахах","ахаха","ахахах","лол","ору","ржу","пиздец","капец","жесть","блять","бля","легенда","гений","база","кринж","имба"}
STOPWORDS = {
    "и","а","но","или","в","во","на","с","со","к","ко","у","за","из","от","до","по","не","ни","я","ты","он","она","мы","вы","они","это","тот","та","те","что","как","ну","да","нет","же","ли","бы","б","то","там","тут","здесь","мне","тебе","его","ее","её","их","мой","твой","наш","ваш","есть","был","была","были","будет","быть",
}


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().casefold()[:600]


def keywords(text: str) -> set[str]:
    return {w.casefold() for w in extract_words(text) if len(w) >= 3 and w.casefold() not in STOPWORDS and not w.isdigit()}


def memory_score(text: str) -> tuple[float, str]:
    # Дешёвый фильтр без AI. Не каждое "ага" достойно вечной памяти, иначе через неделю
    # база будет на 90% состоять из бесполезной хуйни и релевантность просядет.
    clean = " ".join((text or "").split()).strip()
    words = [w.casefold() for w in extract_words(clean)]
    if not clean or len(clean) > 500 or not words:
        return 0.0, "quote"
    if len(words) == 1 and words[0] in LOW_VALUE_SINGLE:
        return 0.0, "quote"
    score = 0.10
    kind = "quote"
    if 2 <= len(words) <= 18:
        score += 0.10
    if any(w in EVENT_HINTS for w in words):
        score += 0.38
        kind = "event"
    if any(w in EMOTION_HINTS for w in words):
        score += 0.12
    if "!" in clean or "?" in clean:
        score += 0.04
    if len(clean) <= 120:
        score += 0.05
    if len(clean) > 320:
        score -= 0.12
    # В отличие от 1.0.3 слова «сегодня/завтра/пойду» НЕ делают фразу долгой памятью.
    return max(0.0, min(score, 1.25)), kind


@dataclass
class MemoryItem:
    text: str
    kind: str
    score: float
    occurrences: int
    username: str | None = None
    display_name: str | None = None

    def prompt_line(self) -> str:
        author = f"@{self.username}" if self.username else (self.display_name or "участник")
        tag = f"локальный мем ×{self.occurrences}" if self.occurrences >= 2 else ("событие" if self.kind == "event" else "воспоминание")
        return f"[{tag}; {author}] {self.text}"


# Повторы повышают occurrences: обычная фраза постепенно может стать локальным мемом.
class GroupMemoryStore:
    def __init__(self, path: str, *, max_per_chat: int = 3000) -> None:
        self.path = path
        self.max_per_chat = max_per_chat

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS chat_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    display_name TEXT,
                    text TEXT NOT NULL,
                    normalized TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'quote',
                    score REAL NOT NULL DEFAULT 0.0,
                    occurrences INTEGER NOT NULL DEFAULT 1,
                    source_message_id INTEGER,
                    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_used DATETIME,
                    UNIQUE(chat_id, normalized)
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_chat_memories_rank ON chat_memories(chat_id,occurrences DESC,score DESC,last_seen DESC)")
            await db.commit()

    async def observe(self, *, chat_id: int, user_id: int, text: str, username: str | None, display_name: str | None, source_message_id: int | None, reply_to_text: str | None = None) -> None:
        clean = " ".join((text or "").split()).strip()
        norm = normalize_text(clean)
        base_score, kind = memory_score(clean)
        if not norm or base_score <= 0:
            return
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT INTO chat_memories(chat_id,user_id,username,display_name,text,normalized,kind,score,occurrences,source_message_id)
                VALUES(?,?,?,?,?,?,?,?,1,?)
                ON CONFLICT(chat_id,normalized) DO UPDATE SET
                    user_id=excluded.user_id,
                    username=COALESCE(excluded.username,chat_memories.username),
                    display_name=COALESCE(excluded.display_name,chat_memories.display_name),
                    text=excluded.text,
                    kind=CASE WHEN chat_memories.occurrences+1>=2 THEN 'meme' WHEN excluded.kind='event' THEN 'event' ELSE chat_memories.kind END,
                    score=MIN(1.75,MAX(chat_memories.score,excluded.score)+0.10),
                    occurrences=chat_memories.occurrences+1,
                    source_message_id=COALESCE(excluded.source_message_id,chat_memories.source_message_id),
                    last_seen=CURRENT_TIMESTAMP
            """, (chat_id,user_id,username,display_name,clean[:500],norm,kind,base_score,source_message_id))
            if reply_to_text:
                rnorm = normalize_text(reply_to_text)
                if rnorm:
                    await db.execute("UPDATE chat_memories SET score=MIN(1.75,score+0.14),last_seen=CURRENT_TIMESTAMP WHERE chat_id=? AND normalized=?", (chat_id,rnorm))
            if self.max_per_chat > 0:
                await db.execute("""
                    DELETE FROM chat_memories WHERE chat_id=? AND id NOT IN (
                        SELECT id FROM chat_memories WHERE chat_id=?
                        ORDER BY (occurrences>=2) DESC,score DESC,last_seen DESC LIMIT ?
                    )
                """, (chat_id,chat_id,self.max_per_chat))
            await db.commit()

    async def relevant(self, chat_id: int, query: str, *, limit: int = 14, exclude_texts: list[str] | None = None) -> list[MemoryItem]:
        # Рейтинг = качество памяти + частота + совпадение ключевых слов. Частый мем может
        # всплыть и без точного overlap, а случайная одноразовая фраза — обычно нет.
        q = keywords(query)
        exclude = {normalize_text(x) for x in (exclude_texts or []) if x}
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""
                SELECT text,normalized,kind,score,occurrences,username,display_name
                FROM chat_memories WHERE chat_id=? AND (score>=0.42 OR occurrences>=2)
                ORDER BY occurrences DESC,score DESC,last_seen DESC LIMIT 600
            """, (chat_id,))
            rows = await cur.fetchall()
        ranked: list[tuple[float,MemoryItem]] = []
        for text,norm,kind,score,occ,username,display_name in rows:
            if str(norm) in exclude:
                continue
            iw = keywords(str(text))
            overlap = len(q & iw) if q else 0
            if q and overlap == 0 and int(occ) < 2 and float(score) < 0.78:
                continue
            rank = float(score) + math.log1p(int(occ))*0.28 + overlap*0.85
            if str(kind) == "event" and overlap:
                rank += 0.15
            if int(occ) >= 2:
                rank += 0.18
            ranked.append((rank, MemoryItem(str(text),str(kind),float(score),int(occ),username,display_name)))
        ranked.sort(key=lambda x: x[0], reverse=True)
        chosen = [x[1] for x in ranked[:limit]]
        if chosen:
            async with aiosqlite.connect(self.path) as db:
                for item in chosen:
                    await db.execute("UPDATE chat_memories SET last_used=CURRENT_TIMESTAMP WHERE chat_id=? AND normalized=?", (chat_id,normalize_text(item.text)))
                await db.commit()
        return chosen

    async def top_meme(self, chat_id: int) -> MemoryItem | None:
        # Самый живучий локальный прикол: сначала частота повторов, потом качество памяти.
        # Если повторов ещё нет, не называем случайную фразу "мемом" просто ради красоты.
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""
                SELECT text,kind,score,occurrences,username,display_name
                FROM chat_memories
                WHERE chat_id=? AND occurrences>=2
                ORDER BY occurrences DESC,score DESC,last_seen DESC
                LIMIT 1
            """, (chat_id,))
            row = await cur.fetchone()
        if not row:
            return None
        return MemoryItem(
            text=str(row[0]),
            kind=str(row[1]),
            score=float(row[2]),
            occurrences=int(row[3]),
            username=row[4],
            display_name=row[5],
        )

    async def stats(self, chat_id: int) -> tuple[int,int]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""
                SELECT COUNT(*),COALESCE(SUM(CASE WHEN occurrences>=2 THEN 1 ELSE 0 END),0)
                FROM chat_memories WHERE chat_id=? AND (score>=0.42 OR occurrences>=2)
            """, (chat_id,))
            row = await cur.fetchone()
        return (int(row[0]),int(row[1])) if row else (0,0)

    async def clear_chat(self, chat_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM chat_memories WHERE chat_id=?", (chat_id,))
            await db.commit()
