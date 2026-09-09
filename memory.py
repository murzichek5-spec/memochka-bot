"""Персональная долговременная память участников.

Curator не отвечает в чат. Он отдельно решает, появился ли устойчивый факт именно об
авторе сообщения. Это сделано, чтобы основная модель могла болтать, а память — не пиздела.
"""


from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from dataclasses import dataclass

from ai_brain import AIBrain
from db import Database
from prompts import curator_user_prompt

logger = logging.getLogger(__name__)

NOISE_RE = re.compile(r"(?iu)^\s*(?:ах+а*х*|лол+|ору+|ржу+|пон|ок|ага|угу|да|нет|бля+|\++|[\W_]+)\s*$")


@dataclass(frozen=True)
class MemoryObservation:
    chat_id: int
    user_id: int
    first_name: str
    username: str
    text: str
    reply_context: str


# Фоновый куратор карточек пользователей. Не путать с GroupMemoryStore — там мемы чата.
class MemoryCurator:
    def __init__(self, db: Database, ai: AIBrain, *, enabled: bool = True) -> None:
        self.db = db
        self.ai = ai
        self.enabled = enabled
        self._tasks: set[asyncio.Task] = set()
        self._locks: dict[tuple[int,int], asyncio.Lock] = defaultdict(asyncio.Lock)

    def should_consider(self, text: str) -> bool:
        clean = (text or "").strip()
        if not self.enabled or not clean or clean.startswith("/"):
            return False
        if len(clean) < 3 or NOISE_RE.match(clean):
            return False
        return True

    def schedule(self, obs: MemoryObservation) -> None:
        if not self.should_consider(obs.text):
            return
        task = asyncio.create_task(self._curate(obs), name=f"memory:{obs.chat_id}:{obs.user_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _curate(self, obs: MemoryObservation) -> None:
        key = (obs.chat_id, obs.user_id)
        async with self._locks[key]:
            try:
                current = await self.db.get_user_memory(obs.chat_id, obs.user_id)
                prompt = curator_user_prompt(
                    user_id=obs.user_id,
                    first_name=obs.first_name,
                    username=obs.username,
                    known_name=str(current.get("known_name") or ""),
                    memory=str(current.get("memory") or ""),
                    text=obs.text,
                    reply_context=obs.reply_context,
                )
                obj, error = await self.ai.curate_memory(user_prompt=prompt)
                if error or not obj:
                    if error:
                        logger.warning("Memory curator skipped: %s", error)
                    return
                changed = bool(obj.get("changed"))
                known_name = str(obj.get("known_name") or "").strip()
                memory = str(obj.get("memory") or "").strip()
                if not changed:
                    return
                await self.db.set_user_memory(obs.chat_id, obs.user_id, known_name=known_name, memory=memory)
                logger.info("Long-term memory updated for chat=%s user=%s", obs.chat_id, obs.user_id)
            except Exception:
                logger.exception("Memory curator task failed")

    async def close(self) -> None:
        # На остановке даём фоновым обновлениям шанс закончить и только потом отменяем хвост.
        if not self._tasks:
            return
        done, pending = await asyncio.wait(self._tasks, timeout=8.0)
        for task in pending:
            task.cancel()
