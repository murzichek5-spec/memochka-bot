"""Обёртка над OpenRouter и разбор ответов модели.

Модель может вернуть мусор даже когда мы попросили строгий JSON. Поэтому наружу из
этого модуля должны выходить уже проверенные значения, а не вера в то, что LLM "ну обещала же".
"""


from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from prompts import (
    DEMOTIVATOR_SYSTEM_PROMPT,
    IDLE_INTERVENTION_SYSTEM_PROMPT,
    INTERVENTION_SYSTEM_PROMPT,
    LIVE_SYSTEM_PROMPT,
    SHORT_REACTION_SYSTEM_SUFFIX,
    MEMORY_CURATOR_SYSTEM,
    PARODY_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

ALLOWED_REACTIONS = {
    "❤","👍","👎","🔥","🥰","👏","😁","🤔","🤯","😱","🤬","😢","🎉","🤩","🤮","💩","🙏","👌","🤡","🥱","🥴","😍","💯","🤣","💔","🤨","😐","🖕","😈","😴","😭","🤓","👀","🙈","😇","😨","🤝","🤗","🫡","💅","🤪","🗿","😘","😎","🤷","😡",
}


@dataclass
class AIResult:
    text: str | None
    error: str | None = None
    model_used: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class AIIntervention:
    action: str = "silent"
    text: str | None = None
    target_message_id: int | None = None
    reaction: str | None = None
    error: str | None = None


class OpenRouterAPIError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        super().__init__(f"HTTP {status}: {body[:1600]}")


# Один клиент AI обслуживает ответы, пародии, curator, автономные вмешательства и демотиваторы.
class AIBrain:
    def __init__(self, *, api_key: str, model: str, timeout_seconds: float = 45.0, max_retries: int = 2) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._session: aiohttp.ClientSession | None = None
        self.last_error: str | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        # Один ClientSession на весь процесс. Создавать новый TCP/TLS коннект на каждый запрос
        # медленно и тупо, поэтому переиспользуем соединения.
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout_seconds))
        return self._session

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            return "".join(parts)
        return str(content or "")

    @staticmethod
    def clean_text(text: str | None, *, lowercase: bool = True, max_chars: int = 1800) -> str | None:
        if not text:
            return None
        value = text.strip()
        value = re.sub(r"^```(?:text|markdown)?\s*|\s*```$", "", value, flags=re.I | re.S).strip()
        value = re.sub(r"^(ответ|сообщение|реплика)\s*:\s*", "", value, flags=re.I).strip()
        if len(value) >= 2 and ((value[0] == value[-1] == '"') or (value[0] == '«' and value[-1] == '»')):
            value = value[1:-1].strip()
        if lowercase:
            value = value.lower()
        value = value[:max_chars].strip()
        return value or None

    @staticmethod
    def parse_json(text: str | None) -> dict[str, Any] | None:
        # Даже json_mode не священная корова: некоторые модели заворачивают JSON в code fence
        # или добавляют текст. Сначала json.loads, потом аккуратно пытаемся вытащить объект.
        if not text:
            return None
        raw = text.strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, flags=re.S)
            if match:
                try:
                    obj = json.loads(match.group(0))
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    pass
        return None

    async def _request(
        self,
        *,
        system: str,
        user: str,
        temperature: float,
        top_p: float,
        max_tokens: int,
        presence_penalty: float = 0.0,
        json_mode: bool = False,
    ) -> tuple[str, dict[str, Any], str | None]:
        session = await self._get_session()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": max(0.0, min(float(temperature), 2.0)),
            "top_p": max(0.0, min(float(top_p), 1.0)),
            "max_tokens": max(16, int(max_tokens)),
            "presence_penalty": max(-2.0, min(float(presence_penalty), 2.0)),
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://localhost/memochka-server",
            "X-Title": "Memochka Server v4",
        }
        # Ретраим только временные ошибки. 401 повторять бессмысленно: неправильный ключ
        # от второй попытки внезапно правильным не станет, сука.
        retry_statuses = {408, 409, 429, 500, 502, 503, 504}
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with session.post(OPENROUTER_URL, json=payload, headers=headers) as response:
                    body = await response.text()
                    if response.status >= 400:
                        exc = OpenRouterAPIError(response.status, body)
                        if response.status in retry_statuses and attempt < self.max_retries:
                            await asyncio.sleep(1.1 * (2 ** attempt) + random.random() * 0.35)
                            last_exc = exc
                            continue
                        raise exc
                    data = json.loads(body)
                    choices = data.get("choices") or []
                    if not choices:
                        raise RuntimeError(f"OpenRouter вернул ответ без choices: {body[:1200]}")
                    content = self._content_to_text((choices[0].get("message") or {}).get("content"))
                    return content, (data.get("usage") or {}), data.get("model")
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(1.1 * (2 ** attempt) + random.random() * 0.35)
                    continue
                raise
        raise last_exc or RuntimeError("OpenRouter request failed")

    async def _chat(self, **kwargs: Any) -> AIResult:
        # Ошибка OpenRouter не должна убивать Telegram polling. Упаковываем её в AIResult,
        # а вызывающий код сам решит: промолчать, сделать fallback или показать сообщение.
        try:
            raw, usage, model_used = await self._request(**kwargs)
            self.last_error = None
            return AIResult(text=raw, usage=usage, model_used=model_used or self.model)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[-1800:]
            logger.exception("OpenRouter request failed")
            return AIResult(text=None, error=self.last_error, model_used=self.model)

    async def generate_reply(self, *, user_prompt: str, temperature: float, max_tokens: int = 140, short_reaction: bool = False) -> AIResult:
        # Короткая реакция — не шаблон из списка, а тот же AI с дополнительной установкой
        # "сейчас ответь по-человечески коротко". Поэтому она всё ещё учитывает контекст.
        system_prompt = LIVE_SYSTEM_PROMPT + (SHORT_REACTION_SYSTEM_SUFFIX if short_reaction else "")
        result = await self._chat(
            system=system_prompt,
            user=user_prompt,
            temperature=temperature,
            top_p=0.95,
            max_tokens=max_tokens,
            presence_penalty=1.0,
            json_mode=False,
        )
        result.text = self.clean_text(result.text, lowercase=True, max_chars=1800)
        return result

    async def generate_parody(self, *, user_prompt: str, temperature: float) -> AIResult:
        result = await self._chat(
            system=PARODY_SYSTEM_PROMPT,
            user=user_prompt,
            temperature=temperature,
            top_p=0.95,
            max_tokens=140,
            presence_penalty=1.0,
            json_mode=False,
        )
        result.text = self.clean_text(result.text, lowercase=True, max_chars=1400)
        return result

    async def curate_memory(self, *, user_prompt: str) -> tuple[dict[str, Any] | None, str | None]:
        result = await self._chat(
            system=MEMORY_CURATOR_SYSTEM,
            user=user_prompt,
            temperature=0.2,
            top_p=1.0,
            max_tokens=650,
            presence_penalty=0.0,
            json_mode=True,
        )
        if result.error:
            return None, result.error
        obj = self.parse_json(result.text)
        if not obj:
            return None, "Memory Curator вернул невалидный JSON"
        return obj, None

    async def generate_intervention(
        self,
        *,
        user_prompt: str,
        valid_targets: set[int],
        temperature: float,
        mode: str = "active",
        short_reaction: bool = False,
    ) -> AIIntervention:
        # active и idle — две разные социальные ситуации. Через 20 минут обычный reply
        # выглядит так, будто у бота пинг из космоса, поэтому idle получает отдельный промпт.
        system_prompt = IDLE_INTERVENTION_SYSTEM_PROMPT if mode == "idle" else INTERVENTION_SYSTEM_PROMPT
        if short_reaction and mode == "active":
            system_prompt += SHORT_REACTION_SYSTEM_SUFFIX
        result = await self._chat(
            system=system_prompt,
            user=user_prompt,
            temperature=temperature,
            top_p=0.95,
            max_tokens=220,
            presence_penalty=1.0,
            json_mode=True,
        )
        if result.error:
            return AIIntervention(error=result.error)
        obj = self.parse_json(result.text)
        if not obj:
            return AIIntervention(error="invalid intervention json")
        # Модели не доверяем: action, target и reaction валидируются ниже. Если она придумала
        # несуществующий message_id или левый emoji — безопаснее SILENT, чем странная херня в чат.
        action = str(obj.get("action") or "silent").casefold()
        if action not in {"silent","react","reply","message"}:
            return AIIntervention(action="silent")
        if action == "silent":
            return AIIntervention(action="silent")
        try:
            target = int(obj.get("target_message_id") or 0)
        except (ValueError, TypeError):
            target = 0
        reaction = str(obj.get("reaction") or "-").strip()
        text = self.clean_text(str(obj.get("text") or ""), lowercase=True, max_chars=1000)
        if action == "react":
            if target not in valid_targets or reaction not in ALLOWED_REACTIONS:
                return AIIntervention(action="silent")
            return AIIntervention(action="react", target_message_id=target, reaction=reaction)
        if action == "reply":
            if target not in valid_targets or not text:
                return AIIntervention(action="silent")
            return AIIntervention(action="reply", target_message_id=target, text=text)
        if action == "message" and text:
            return AIIntervention(action="message", text=text)
        return AIIntervention(action="silent")

    async def generate_demotivator_caption(
        self,
        *,
        user_prompt: str,
        custom_title: str | None = None,
        allow_silence: bool = False,
    ) -> tuple[str, str] | None:
        system_prompt = DEMOTIVATOR_SYSTEM_PROMPT
        # Автоматический демотиватор имеет право отказаться. Лучше ничего не отправить,
        # чем насильно рожать несмешную картинку только потому, что рандом выпал.
        if allow_silence:
            system_prompt += '\n\nесли демотиватор сейчас был бы натянутым или совсем неуместным, верни только json {"silent":true}.'
        result = await self._chat(
            system=system_prompt,
            user=user_prompt,
            temperature=1.0,
            top_p=0.95,
            max_tokens=120,
            presence_penalty=0.7,
            json_mode=True,
        )
        if result.error:
            return None
        obj = self.parse_json(result.text) or {}
        if allow_silence and bool(obj.get("silent")):
            return None
        title = (custom_title or str(obj.get("title") or "")).strip().lower()
        subtitle = str(obj.get("subtitle") or "").strip().lower()
        if not title:
            return None
        return title[:220], subtitle[:300]
