"""Главный файл бота: Telegram-события, автономность и склейка модулей.

Если коротко: принимаем сообщения -> запоминаем -> решаем, пора ли боту влезть ->
просим AI выбрать действие -> отправляем результат. Генерация и SQL вынесены отдельно,
чтобы этот файл не превратился в нечитаемую помойку.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from collections import defaultdict, deque
from io import BytesIO
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction, ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
    ReplyParameters,
)

from ai_brain import AIBrain, AIIntervention
from config import load_settings
from db import Database
from demotivator import make_demotivator
from quote_card import make_quote_card
from memory import MemoryCurator, MemoryObservation
from memory_store import GroupMemoryStore
from prompts import intervention_user_prompt, parody_user_prompt, reply_user_prompt

# Поднимаем зависимости один раз на процесс. Тут видно всю цепочку проекта:
# конфиг -> БД/память -> AI -> Telegram router. Никакой скрытой магии.
settings = load_settings()
router = Router()
db = Database(settings.db_path, max_messages_per_chat=settings.max_stored_messages, max_conversation_log=settings.max_conversation_log)
group_memory = GroupMemoryStore(settings.db_path, max_per_chat=settings.group_memory_max_per_chat)
ai = AIBrain(
    api_key=settings.openrouter_api_key or "missing-key",
    model=settings.openrouter_model,
    timeout_seconds=settings.openrouter_timeout_seconds,
    max_retries=settings.openrouter_max_retries,
)
curator = MemoryCurator(db, ai, enabled=settings.memory_curator_enabled)

# Всё ниже — временное состояние в RAM. После рестарта эти словари обнуляются,
# и это нормально: они отвечают за таймеры, cooldown'ы и последние секунды разговора,
# а не за долговременную память. Долгая память живёт в SQLite.
BOT_ID: int | None = None
BOT_USERNAME = ""
BOT_DISPLAY_NAME = "мёмочка"
LAST_AUTO_ACTION_AT: dict[int, float] = {}
LAST_AI_CHECK_AT: dict[int, float] = {}
RECENT_LIVE_MESSAGES: dict[int, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=64))
PENDING_AUTO_TASKS: dict[int, asyncio.Task] = {}
ACTIVE_GROUP_CHATS: set[int] = set()
LAST_HUMAN_MESSAGE_AT: dict[int, float] = {}
LAST_IDLE_ATTEMPT_AT: dict[int, float] = {}
LAST_AUTO_DEMOTIVATOR_AT: dict[int, float] = {}


def setup_logging() -> None:
    # Пишем и в консоль, и в файл с ротацией. Ротация нужна, чтобы однажды
    # memochka.log не сожрал весь диск, потому что API решил неделю хуярить ошибками.
    Path("logs").mkdir(exist_ok=True)
    level = getattr(logging, settings.log_level, logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)
        file_handler = RotatingFileHandler("logs/memochka.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)


def _text_of(message: Message | None) -> str:
    if not message:
        return ""
    return (message.text or message.caption or "").strip()


def quote_author_label(message: Message | None) -> str:
    if not message or not message.from_user:
        return "неизвестный гений"
    if message.from_user.username:
        return f"@{message.from_user.username}"
    return message.from_user.full_name or "неизвестный гений"


def reply_context_from_message(message: Message) -> dict[str, Any]:
    # Reply-контекст критичен для памяти. Фраза "ты долбоёб" обычно относится
    # к тому, КОМУ ответили. Без этих полей бот быстро начинает приписывать людям чужую херню.
    reply = message.reply_to_message
    user = reply.from_user if reply else None
    return {
        "reply_to_message_id": reply.message_id if reply else None,
        "reply_to_user_id": user.id if user else None,
        "reply_to_username": user.username if user else None,
        "reply_to_display_name": user.full_name if user else None,
        "reply_to_text": _text_of(reply),
    }


def format_reply_context(ctx: dict[str, Any]) -> str:
    if not ctx.get("reply_to_message_id"):
        return "(не reply)"
    return "\n".join([
        f"reply_to_message_id: {ctx.get('reply_to_message_id') or ''}",
        f"reply_to_user_id: {ctx.get('reply_to_user_id') or ''}",
        f"reply_to_name: {ctx.get('reply_to_display_name') or ''}",
        f"reply_to_username: {ctx.get('reply_to_username') or ''}",
        f"reply_to_text: {ctx.get('reply_to_text') or ''}",
    ])


def format_event(event: dict[str, Any]) -> str:
    # Явно подписываем user_id, имя и reply. Чем меньше двусмысленности в контексте,
    # тем меньше модель фантазирует, кто кому что сказал.
    role = "assistant" if bool(event.get("is_bot")) else "user"
    name = event.get("display_name") or (f"@{event['username']}" if event.get("username") else f"user{event.get('user_id')}")
    text = " ".join(str(event.get("text") or "").split())
    head = f"[date={event.get('created_at') or ''} | role={role} | user_id={event.get('user_id')} | name={name} | message_id={event.get('message_id') or ''}]"
    if event.get("reply_to_message_id"):
        reply = (
            "[REPLY_CONTEXT]\n"
            f"reply_to_message_id={event.get('reply_to_message_id') or ''}\n"
            f"reply_to_user_id={event.get('reply_to_user_id') or ''}\n"
            f"reply_to_name={event.get('reply_to_display_name') or ''}\n"
            f"reply_to_username={event.get('reply_to_username') or ''}\n"
            f"reply_to_text={event.get('reply_to_text') or ''}\n"
            "[/REPLY_CONTEXT]"
        )
        return f"{head}\n{text[:700]}\n{reply}"
    return f"{head}\n{text[:700]}"


def format_user_memories(cards: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for c in cards:
        known = str(c.get("known_name") or "").strip()
        memory = str(c.get("memory") or "").strip()
        if not known and not memory:

            blocks.append(f"[user_id={c.get('user_id')} | telegram_name={c.get('display_name') or ''} | username={c.get('username') or ''}] memory: (пусто)")
            continue
        blocks.append(
            f"[user_id={c.get('user_id')} | known_name={known} | telegram_name={c.get('display_name') or ''} | username={c.get('username') or ''}]\n"
            f"memory:\n{memory or '(пусто)'}"
        )
    return "\n\n".join(blocks)


def _style_examples(messages: list[str], limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in reversed(messages):
        text = " ".join((raw or "").split()).strip()
        key = text.casefold()
        if not text or text.startswith("/") or key in seen:
            continue
        if 2 <= len(text) <= 350:
            result.append(text)
            seen.add(key)
        if len(result) >= limit:
            break
    result.reverse()
    return result


def _top_words(counts: dict[str, int], limit: int = 24) -> str:
    return ", ".join(f"{w}×{c}" for w, c in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit])


def remember_live_user_message(message: Message) -> None:
    if not message.from_user:
        return
    body = " ".join((_text_of(message) or "[фото]").split()).strip()
    RECENT_LIVE_MESSAGES[message.chat.id].append({
        "message_id": message.message_id,
        "user_id": message.from_user.id,
        "is_bot": False,
        "text": body,
        "ts": time.monotonic(),
    })


def remember_live_bot_message(sent: Message, text: str) -> None:
    RECENT_LIVE_MESSAGES[sent.chat.id].append({
        "message_id": sent.message_id,
        "user_id": BOT_ID or 0,
        "is_bot": True,
        "text": text,
        "ts": time.monotonic(),
    })


async def chat_settings(chat_id: int) -> dict[str, float | bool]:
    return await db.get_settings(chat_id, default_chance=settings.default_auto_reply_chance, default_chaos=settings.default_creativity)


async def build_context(chat_id: int, *, exclude_message_id: int | None = None) -> tuple[str, list[dict[str, Any]]]:
    events = await db.get_recent_context(chat_id, limit=settings.context_messages, exclude_message_id=exclude_message_id)
    return "\n\n".join(format_event(x) for x in events), events


async def group_memory_lines(chat_id: int, query: str, recent_events: list[dict[str, Any]]) -> str:
    excluded = [str(x.get("text") or "") for x in recent_events]
    items = await group_memory.relevant(chat_id, query, limit=settings.group_memory_examples, exclude_texts=excluded)
    return "\n".join(f"- {x.prompt_line()}" for x in items)


# Прямой вызов: личка, упоминание или reply на бота. Тут случайного шанса нет:
# если человека позвали, надо отвечать, а не строить из себя загадочную хуету.
async def build_direct_reply(message: Message) -> str | None:
    assert message.from_user
    ctx = reply_context_from_message(message)
    context_text, events = await build_context(message.chat.id, exclude_message_id=message.message_id)
    current_mem = await db.get_user_memory(message.chat.id, message.from_user.id)
    all_cards = await db.get_all_user_memories(message.chat.id, limit=settings.max_user_memory_cards)
    history = await db.get_messages(message.chat.id, limit=900)
    counts = await db.get_word_counts(message.chat.id)
    query = _text_of(message)
    group_mem = await group_memory_lines(message.chat.id, query + "\n" + context_text[-2500:], events)
    current_mem_text = (
        f"known_name: {current_mem.get('known_name') or ''}\n"
        f"memory:\n{current_mem.get('memory') or '(пусто)'}"
    )
    prompt = reply_user_prompt(
        user_id=message.from_user.id,
        first_name=message.from_user.full_name or "",
        username=message.from_user.username or "",
        text=_text_of(message),
        reply_context=format_reply_context(ctx),
        current_memory=current_mem_text,
        all_memories=format_user_memories(all_cards),
        group_memories=group_mem,
        context=context_text,
        style_examples="\n".join(f"- {x}" for x in _style_examples(history, settings.style_examples)),
        top_words=_top_words(counts),
    )
    s = await chat_settings(message.chat.id)
    temperature = 0.85 + 0.55 * float(s["chaos"])
    result = await ai.generate_reply(
        user_prompt=prompt,
        temperature=temperature,
        max_tokens=140,
        short_reaction=random.random() < settings.short_reaction_chance,
    )
    if result.error:
        logging.getLogger(__name__).warning("Reply AI error: %s", result.error)
    return result.text


async def build_parody(chat_id: int, user_id: int, label: str, seed: str | None) -> str | None:
    context_text, events = await build_context(chat_id)
    history = await db.get_messages(chat_id, limit=1000, user_id=user_id)
    counts = await db.get_word_counts(chat_id, user_id=user_id)
    query = seed or context_text[-2000:]
    gm = await group_memory_lines(chat_id, query, events)
    prompt = parody_user_prompt(
        label=label,
        seed=seed or "",
        context=context_text,
        examples="\n".join(f"- {x}" for x in _style_examples(history, 24)),
        top_words=_top_words(counts),
        memories=gm,
    )
    s = await chat_settings(chat_id)
    result = await ai.generate_parody(user_prompt=prompt, temperature=0.9 + 0.5 * float(s["chaos"]))
    return result.text


# Автовмешательство не равно "сгенерируй текст". Сначала модель решает действие:
# silent / react / reply / message. Так бот может нормально промолчать или кинуть реакцию,
# а не обязан высрать абзац после каждого сообщения.
async def build_auto_intervention(
    chat_id: int,
    *,
    mode: str = "active",
    idle_seconds: float | None = None,
) -> AIIntervention:
    if mode == "idle":
        minutes = max(4, int(settings.idle_max_silence_seconds / 60) + 5)
    else:
        minutes = 4
    targets = await db.get_recent_human_targets(chat_id, limit=12, minutes=minutes)
    valid = {int(x["message_id"]) for x in targets if x.get("message_id")}
    if not valid:
        return AIIntervention(action="silent")
    context_text, events = await build_context(chat_id)
    all_cards = await db.get_all_user_memories(chat_id, limit=settings.max_user_memory_cards)
    history = await db.get_messages(chat_id, limit=1000)
    counts = await db.get_word_counts(chat_id)
    query = "\n".join(str(x.get("text") or "") for x in targets[-6:])
    gm = await group_memory_lines(chat_id, query, events)
    target_lines: list[str] = []
    for x in targets:
        label = f"@{x['username']}" if x.get("username") else (x.get("display_name") or f"user{x.get('user_id')}")
        extra = f" reply_to_user_id={x.get('reply_to_user_id')}" if x.get("reply_to_user_id") else ""
        target_lines.append(f"[id={x.get('message_id')} user_id={x.get('user_id')}{extra}] {label}: {str(x.get('text') or '')[:500]}")
    prompt = intervention_user_prompt(
        context=context_text,
        targets="\n".join(target_lines),
        all_memories=format_user_memories(all_cards),
        group_memories=gm,
        style_examples="\n".join(f"- {x}" for x in _style_examples(history, settings.style_examples)),
        top_words=_top_words(counts),
    )
    if mode == "idle":
        mins = max(1, round(float(idle_seconds or 0) / 60))
        prompt = f"с момента последнего человеческого сообщения прошло примерно {mins} мин.\n\n" + prompt
    s = await chat_settings(chat_id)
    temperature = 0.85 + 0.50 * float(s["chaos"])
    return await ai.generate_intervention(
        user_prompt=prompt,
        valid_targets=valid,
        temperature=temperature,
        mode=mode,
        short_reaction=(mode == "active" and random.random() < settings.short_reaction_chance),
    )


def is_directed_at_bot(message: Message) -> bool:
    if BOT_ID and message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.id == BOT_ID:
        return True
    text = _text_of(message).casefold()
    if BOT_USERNAME and f"@{BOT_USERNAME}" in text:
        return True
    return any(alias in text for alias in settings.bot_name_aliases)


def dynamic_auto_probability(chat_id: int, base_chance: float, current: Message) -> float:
    # /chance задаёт базу, а дальше шанс адаптируется под реальный разговор:
    # активный чат и несколько участников повышают шанс, недавняя реплика самого бота — режет.
    # Без этого бот либо молчит сутками, либо спамит как ебанутый.
    """Реальный шанс проверить возможность самостоятельного вмешательства.

    /chance 100 — специальный тестовый/максимальный режим: каждое подходящее
    групповое сообщение проходит к AI-selector. Для остальных значений шанс
    остаётся динамическим, но Мёмочка теперь заметно активнее.
    """
    base = max(0.0, min(float(base_chance), 1.0))
    if base >= 0.999:
        return 1.0

    now = time.monotonic()
    recent = list(RECENT_LIVE_MESSAGES[chat_id])
    humans = [x for x in recent if not bool(x.get("is_bot"))]
    count_30 = sum(1 for x in humans if now - float(x.get("ts") or 0) <= 30)
    count_90 = sum(1 for x in humans if now - float(x.get("ts") or 0) <= 90)
    unique_90 = {int(x.get("user_id") or 0) for x in humans if now - float(x.get("ts") or 0) <= 90}

    p = base
    if count_30 >= 5:
        p *= 1.70
    elif count_30 >= 3:
        p *= 1.45
    elif count_90 >= 4:
        p *= 1.25
    elif count_90 <= 1:
        p *= 0.90

    if len(unique_90) >= 3:
        p += 0.05

    text = _text_of(current)
    lowered = text.casefold()
    if "?" in text:
        p += 0.06
    if current.reply_to_message:
        p += 0.04
    if re.search(r"\b(ахах|ахаха|ахахах|лол|ору|ржу|пиздец|жесть|блять|бля)\b", lowered):
        p += 0.04
    if 12 <= len(text) <= 260:
        p += 0.025

    last = recent[-10:]
    bot_positions = [i for i, x in enumerate(last) if bool(x.get("is_bot"))]
    if bot_positions:
        distance = len(last) - 1 - bot_positions[-1]
        if distance <= 2:
            p *= 0.72
        elif distance <= 5:
            p *= 0.84

    return max(0.0, min(p, 0.95))


def effective_auto_cooldowns(base_chance: float) -> tuple[float, float]:
    # Чем выше /chance, тем короче фактические cooldown'ы. Поэтому /chance 100
    # реально годится для теста, а не означает "100%, но подожди всё равно минуту".
    """Чем выше /chance, тем меньше пауза между самостоятельными проверками."""
    base = max(0.0, min(float(base_chance), 1.0))
    if base >= 0.999:
        return (3.0, 1.5)

    auto = float(settings.auto_cooldown_seconds)
    ai_check = float(settings.ai_check_cooldown_seconds)
    factor = 1.0 - 0.58 * base
    return (max(5.0, auto * factor), max(2.0, ai_check * factor))


async def is_chat_admin(message: Message, bot: Bot) -> bool:
    if message.chat.type == "private":
        return True
    if not message.from_user:
        return False
    member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}


async def is_user_admin(chat_id: int, user_id: int, chat_type: str, bot: Bot) -> bool:
    # Для inline-кнопок у нас нет обычного Message от команды, поэтому проверяем админа отдельно.
    if chat_type == "private":
        return True
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}


def user_label(profile: dict[str, Any] | None, fallback: str = "этот человек") -> str:
    if not profile:
        return fallback
    return f"@{profile['username']}" if profile.get("username") else str(profile.get("display_name") or fallback)


async def remember_bot_message(sent: Message, text: str, *, target: dict[str, Any] | None = None) -> None:
    # Собственные сообщения бота тоже пишем в conversation_log. Иначе модель не знает,
    # что она уже только что пошутила эту же шутку, и начинает повторяться как дед за столом.
    if BOT_ID is None:
        return
    await db.add_bot_message(
        chat_id=sent.chat.id,
        message_id=sent.message_id,
        user_id=BOT_ID,
        text=text,
        username=BOT_USERNAME or None,
        display_name=BOT_DISPLAY_NAME,
        reply_to_message_id=int(target["message_id"]) if target and target.get("message_id") else None,
        reply_to_user_id=int(target["user_id"]) if target and target.get("user_id") is not None else None,
        reply_to_username=str(target.get("username") or "") or None if target else None,
        reply_to_display_name=str(target.get("display_name") or "") or None if target else None,
        reply_to_text=str(target.get("text") or "") or None if target else None,
    )
    remember_live_bot_message(sent, text)


async def send_reply_to_message(message: Message, text: str) -> Message:
    target = {
        "message_id": message.message_id,
        "user_id": message.from_user.id if message.from_user else 0,
        "username": message.from_user.username if message.from_user else None,
        "display_name": message.from_user.full_name if message.from_user else None,
        "text": _text_of(message),
    }
    sent = await message.reply(text)
    await remember_bot_message(sent, text, target=target)
    return sent


async def send_plain(message: Message, text: str) -> Message:
    sent = await message.answer(text)
    await remember_bot_message(sent, text)
    return sent


async def send_auto_text(bot: Bot, chat_id: int, text: str, target_message_id: int | None = None) -> Message:
    target = await db.get_message_by_id(chat_id, target_message_id) if target_message_id else None
    rp = ReplyParameters(message_id=target_message_id, allow_sending_without_reply=True) if target_message_id else None
    sent = await bot.send_message(chat_id, text, reply_parameters=rp)
    await remember_bot_message(sent, text, target=target)
    return sent


# AI здесь придумывает только подпись. Само фото уже реальное и когда-то было отправлено
# в этом чате. Это важно: демотиваторы должны ощущаться локальным мемом, а не генератором рандома.
async def build_auto_demotivator_caption(
    chat_id: int,
    photo: dict[str, Any],
    *,
    mode: str,
    idle_seconds: float | None = None,
) -> tuple[str, str] | None:
    context_text, _ = await build_context(chat_id)
    history = await db.get_messages(chat_id, limit=700)
    author = user_label(photo, fallback="кто-то")
    hints = [f"это старая фотография из этой беседы, её отправил {author}"]
    caption = " ".join(str(photo.get("caption") or "").split()).strip()
    if caption:
        hints.append(f"исходная подпись к фото: {caption[:300]}")
    if mode == "idle" and idle_seconds:
        hints.append(f"после последних сообщений прошло примерно {max(1, round(idle_seconds / 60))} мин")
        hints.append("это должен быть естественный callback, а не ответ как будто фото прислали секунду назад")
    else:
        hints.append("привяжи демотиватор к недавней теме или общему вайбу разговора, если это естественно")
    prompt = (
        "ИНФОРМАЦИЯ О ФОТО:\n" + "\n".join(hints)
        + "\n\nПОСЛЕДНИЙ КОНТЕКСТ:\n" + context_text[-5000:]
        + "\n\nПРИМЕРЫ РЕЧИ:\n" + "\n".join(f"- {x}" for x in _style_examples(history, 16))
    )
    return await ai.generate_demotivator_caption(
        user_prompt=prompt,
        allow_silence=True,
    )


async def send_auto_demotivator(
    bot: Bot,
    chat_id: int,
    photo: dict[str, Any],
    title: str,
    subtitle: str,
) -> Message | None:
    try:
        tg_file = await bot.get_file(str(photo["file_id"]))
        image_bytes = BytesIO()
        await bot.download_file(tg_file.file_path, destination=image_bytes)
        result = make_demotivator(image_bytes.getvalue(), title=title, subtitle=subtitle)
        sent = await bot.send_photo(chat_id, BufferedInputFile(result, filename="demotivator.jpg"))
    except Exception:
        logging.getLogger(__name__).exception("Не удалось отправить авто-демотиватор в чат %s", chat_id)
        return None

    await remember_bot_message(sent, f"[демотиватор] {title}" + (f" — {subtitle}" if subtitle else ""))
    await db.mark_photo_used(int(photo["id"]))
    stamp = time.monotonic()
    LAST_AUTO_ACTION_AT[chat_id] = stamp
    LAST_AUTO_DEMOTIVATOR_AT[chat_id] = stamp
    return sent


async def maybe_send_auto_demotivator(
    # Проверки специально идут до AI: включено ли, накопились ли фото, прошёл ли cooldown,
    # выпал ли шанс. Не тратим запрос к модели просто ради того, чтобы потом ничего не отправить.
    bot: Bot,
    chat_id: int,
    *,
    mode: str,
    idle_seconds: float | None = None,
    base_chance: float | None = None,
) -> bool:
    if not settings.auto_demotivators_enabled:
        return False
    if await db.count_photos(chat_id) < settings.auto_demotivator_min_photos:
        return False
    now = time.monotonic()
    if settings.auto_demotivator_cooldown_seconds > 0 and now - LAST_AUTO_DEMOTIVATOR_AT.get(chat_id, 0.0) < settings.auto_demotivator_cooldown_seconds:
        return False

    chance = settings.auto_demotivator_idle_chance if mode == "idle" else settings.auto_demotivator_chance
    if base_chance is not None:
        chance *= max(0.35, min(float(base_chance), 1.0))
    if random.random() > max(0.0, min(chance, 1.0)):
        return False

    photo = await db.pick_photo_for_demotivator(chat_id)
    if not photo:
        return False
    caption = await build_auto_demotivator_caption(chat_id, photo, mode=mode, idle_seconds=idle_seconds)
    if not caption:
        return False
    sent = await send_auto_demotivator(bot, chat_id, photo, *caption)
    return sent is not None


async def execute_auto_intervention(
    bot: Bot,
    chat_id: int,
    *,
    mode: str = "active",
    idle_seconds: float | None = None,
) -> bool:
    action = await build_auto_intervention(chat_id, mode=mode, idle_seconds=idle_seconds)
    logging.getLogger(__name__).debug("AI intervention: chat=%s mode=%s action=%s", chat_id, mode, action.action)
    acted = False
    if action.action == "react" and action.target_message_id and action.reaction:
        acted = await try_reaction(bot, chat_id, action.target_message_id, action.reaction)
    elif action.action == "reply" and action.target_message_id and action.text:
        await send_auto_text(bot, chat_id, action.text, action.target_message_id)
        acted = True
    elif action.action == "message" and action.text:
        await send_auto_text(bot, chat_id, action.text)
        acted = True
    if acted:
        LAST_AUTO_ACTION_AT[chat_id] = time.monotonic()
    return acted


def cancel_scheduled_intervention(chat_id: int) -> None:
    # Debounce: новое сообщение делает старый таймер неактуальным. Отменяем его,
    # иначе бот может внезапно ответить на тему, которая уже пять сообщений как закончилась.
    task = PENDING_AUTO_TASKS.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


def schedule_delayed_intervention(bot: Bot, chat_id: int, current: Message) -> None:
    # Главный трюк автономности: после каждого сообщения ждём естественную паузу.
    # Если люди продолжают писать — таймер переносится. Бот вклинивается между кусками разговора,
    # а не между двумя сообщениями одного человека.
    cancel_scheduled_intervention(chat_id)
    low = max(1.0, min(settings.auto_delay_min_seconds, settings.auto_delay_max_seconds))
    high = max(low, settings.auto_delay_max_seconds)
    delay = random.uniform(low, high)
    logging.getLogger(__name__).debug("Авто-вмешательство запланировано: chat=%s delay=%.1fs", chat_id, delay)
    PENDING_AUTO_TASKS[chat_id] = asyncio.create_task(delayed_auto_intervention(bot, chat_id, current, delay))


async def delayed_auto_intervention(bot: Bot, chat_id: int, current: Message, delay: float) -> None:
    # Фильтры до AI экономят деньги и нервы: silent mode, мало сообщений, cooldown,
    # не выпал шанс — сразу выходим. Гонять модель ради гарантированного молчания нахер не надо.
    try:
        await asyncio.sleep(delay)
        s = await chat_settings(chat_id)
        if not bool(s["auto_enabled"]):
            return
        if await db.count_messages(chat_id) < settings.min_messages_for_auto_reply:
            return

        now = time.monotonic()
        base_chance = float(s["auto_reply_chance"])
        auto_cooldown, ai_check_cooldown = effective_auto_cooldowns(base_chance)
        if auto_cooldown and now - LAST_AUTO_ACTION_AT.get(chat_id, 0.0) < auto_cooldown:
            return
        if ai_check_cooldown and now - LAST_AI_CHECK_AT.get(chat_id, 0.0) < ai_check_cooldown:
            return

        chance = dynamic_auto_probability(chat_id, base_chance, current)
        if random.random() > chance:
            return
        LAST_AI_CHECK_AT[chat_id] = now

        if await maybe_send_auto_demotivator(bot, chat_id, mode="active", base_chance=chance):
            return

        acted = await execute_auto_intervention(bot, chat_id, mode="active")
        if not acted and base_chance >= 0.999 and current.from_user:
            try:
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
            except Exception:
                pass
            fallback = await build_direct_reply(current)
            if fallback:
                await send_reply_to_message(current, fallback)
                LAST_AUTO_ACTION_AT[chat_id] = time.monotonic()
    except asyncio.CancelledError:
        return
    except Exception:
        logging.getLogger(__name__).exception("Ошибка отложенного авто-вмешательства в чате %s", chat_id)
    finally:
        if PENDING_AUTO_TASKS.get(chat_id) is asyncio.current_task():
            PENDING_AUTO_TASKS.pop(chat_id, None)


async def idle_chat_loop(bot: Bot) -> None:
    # Это НЕ "каждые N минут напиши какую-нибудь херню". Цикл лишь иногда даёт AI шанс
    # сделать callback после тишины. Для этого есть отдельные шанс и cooldown.
    while True:
        try:
            await asyncio.sleep(settings.idle_scan_interval_seconds)
            now = time.monotonic()
            for chat_id in list(ACTIVE_GROUP_CHATS):
                last_human = LAST_HUMAN_MESSAGE_AT.get(chat_id)
                if last_human is None:
                    continue
                silence = now - last_human
                if silence < settings.idle_min_silence_seconds or silence > settings.idle_max_silence_seconds:
                    continue
                if settings.idle_cooldown_seconds > 0 and now - LAST_IDLE_ATTEMPT_AT.get(chat_id, 0.0) < settings.idle_cooldown_seconds:
                    continue

                s = await chat_settings(chat_id)
                if not bool(s["auto_enabled"]):
                    continue
                if await db.count_messages(chat_id) < settings.min_messages_for_auto_reply:
                    continue
                base_chance = float(s["auto_reply_chance"])
                auto_cooldown, _ = effective_auto_cooldowns(base_chance)
                if auto_cooldown and now - LAST_AUTO_ACTION_AT.get(chat_id, 0.0) < auto_cooldown:
                    continue

                LAST_IDLE_ATTEMPT_AT[chat_id] = now
                chance = max(0.0, min(1.0, settings.idle_intervention_chance * (0.5 + base_chance)))
                if random.random() > chance:
                    continue

                if await maybe_send_auto_demotivator(
                    bot, chat_id, mode="idle", idle_seconds=silence, base_chance=chance
                ):
                    continue
                await execute_auto_intervention(bot, chat_id, mode="idle", idle_seconds=silence)
        except asyncio.CancelledError:
            return
        except Exception:
            logging.getLogger(__name__).exception("Ошибка фонового idle-цикла")


async def try_reaction(bot: Bot, chat_id: int, message_id: int, emoji: str) -> bool:
    # Telegram в разных чатах разрешает разные реакции. Если выбранный emoji не прошёл,
    # пробуем безопасный 👍 и не роняем обработчик из-за одной ебаной реакции.
    if not settings.reactions_enabled:
        return False
    for candidate in ((emoji, "👍") if emoji != "👍" else (emoji,)):
        try:
            await bot.set_message_reaction(chat_id=chat_id, message_id=message_id, reaction=[ReactionTypeEmoji(emoji=candidate)])
            return True
        except TelegramBadRequest:
            continue
        except Exception:
            logging.getLogger(__name__).exception("Не удалось поставить реакцию")
            return False
    return False


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "йо, я мёмочка. живу в этом чате, помню ваши приколы, иногда сама влезаю в разговор и периодически делаю хуйню из ваших фоток 😼\n\n"
        "команды:\n"
        "/mgen — реплика в стиле чата\n"
        "/mgen @username — пародия на участника\n"
        "/mdem — демотиватор из фото или памяти чата\n"
        "/mcit — карточка-цитата по reply\n"
        "/mstats — память и статистика\n"
        "/mset — настройки самостоятельности\n"
        "/mvpn — маркевич впн\n"
        "/mforget — стереть память текущего чата (админ)\n"
        "/mhelp — показать эту подсказку"
    )


@router.message(Command("mhelp"))
async def cmd_help(message: Message) -> None:
    await cmd_start(message)


def settings_main_keyboard(auto_enabled: bool) -> InlineKeyboardMarkup:
    status = "вкл ✅" if auto_enabled else "выкл ❌"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Шанс вмешательства", callback_data="mset:chance")],
        [InlineKeyboardButton(text="🌀 Хаос", callback_data="mset:chaos")],
        [InlineKeyboardButton(text=f"🤖 Самостоятельность: {status}", callback_data="mset:toggle")],
        [InlineKeyboardButton(text="✖️ Закрыть", callback_data="mset:close")],
    ])


def settings_value_keyboard(kind: str, values: list[int]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for value in values:
        row.append(InlineKeyboardButton(text=f"{value}%", callback_data=f"mset:{kind}:{value}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="mset:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def render_settings_text(chat_id: int) -> str:
    s = await chat_settings(chat_id)
    return (
        "настройки мёмочки\n\n"
        f"🎯 шанс вмешательства: {float(s['auto_reply_chance']) * 100:.0f}%\n"
        f"🌀 хаос: {float(s['chaos']) * 100:.0f}%\n"
        f"🤖 самостоятельность: {'включена' if bool(s['auto_enabled']) else 'выключена'}\n\n"
        "тыкни кнопку ниже — всё меняется прямо тут, без шаманства с командами"
    )


@router.message(Command("mset"))
async def cmd_settings(message: Message, bot: Bot) -> None:
    if not await is_chat_admin(message, bot):
        await message.answer("настройки могут крутить только админы")
        return
    s = await chat_settings(message.chat.id)
    await message.answer(
        await render_settings_text(message.chat.id),
        reply_markup=settings_main_keyboard(bool(s["auto_enabled"])),
    )


@router.callback_query(F.data.startswith("mset:"))
async def on_settings_callback(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.message or not callback.from_user:
        await callback.answer()
        return

    chat = callback.message.chat
    if not await is_user_admin(chat.id, callback.from_user.id, chat.type, bot):
        await callback.answer("это меню только для админов", show_alert=True)
        return

    data = callback.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "close":
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("закрыл")
        return

    if action == "chance" and len(parts) == 2:
        await callback.message.edit_text(
            "🎯 выбери базовый шанс самостоятельного вмешательства\n\n"
            "чем выше — тем чаще мёмочка получает шанс влезть в разговор",
            reply_markup=settings_value_keyboard("chance", [5, 10, 20, 30, 40, 50, 65, 80, 100]),
        )
        await callback.answer()
        return

    if action == "chaos" and len(parts) == 2:
        await callback.message.edit_text(
            "🌀 выбери уровень хаоса\n\n"
            "ниже — спокойнее и логичнее, выше — смелее, страннее и мемнее",
            reply_markup=settings_value_keyboard("chaos", [0, 15, 30, 45, 60, 75, 90, 100]),
        )
        await callback.answer()
        return

    if action == "toggle":
        s = await chat_settings(chat.id)
        enabled = not bool(s["auto_enabled"])
        await db.set_auto_enabled(chat.id, enabled)
        if not enabled:
            cancel_scheduled_intervention(chat.id)
        s2 = await chat_settings(chat.id)
        await callback.message.edit_text(
            await render_settings_text(chat.id),
            reply_markup=settings_main_keyboard(bool(s2["auto_enabled"])),
        )
        await callback.answer("самостоятельность включена" if enabled else "самостоятельность выключена")
        return

    if action in {"chance", "chaos"} and len(parts) == 3:
        try:
            value = int(parts[2])
        except ValueError:
            await callback.answer("кривое значение", show_alert=True)
            return
        value = max(0, min(100, value))
        if action == "chance":
            await db.set_chance(chat.id, value / 100)
            note = f"шанс теперь {value}%"
        else:
            await db.set_chaos(chat.id, value / 100)
            note = f"хаос теперь {value}%"
        s = await chat_settings(chat.id)
        await callback.message.edit_text(
            await render_settings_text(chat.id),
            reply_markup=settings_main_keyboard(bool(s["auto_enabled"])),
        )
        await callback.answer(note)
        return

    if action == "back":
        s = await chat_settings(chat.id)
        await callback.message.edit_text(
            await render_settings_text(chat.id),
            reply_markup=settings_main_keyboard(bool(s["auto_enabled"])),
        )
        await callback.answer()
        return

    await callback.answer()


def _pretty_num(value: int) -> str:
    # 18742 -> "18 742". Мелочь, а сводка сразу выглядит не как дамп SQL.
    return f"{int(value):,}".replace(",", " ")


def _stats_user_label(profile: dict[str, Any] | None) -> str:
    if not profile:
        return "пока никто не отличился"
    if profile.get("username"):
        return f"@{profile['username']}"
    if profile.get("display_name"):
        return str(profile["display_name"])
    return f"user{profile.get('user_id', '')}"


def _clean_stat_fragment(text: str, limit: int = 54) -> str:
    clean = " ".join((text or "").split()).strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _chat_diagnosis(*, messages: int, memes: int, chaos: float, photos: int) -> str:
    # Не просим AI ради одной финальной строчки. Несколько локальных вариантов дают
    # характер, а /mstats остаётся мгновенной и ничего не стоит по API.
    variants = [
        "чат потерян, но мне нравится",
        "данных уже достаточно для шантажа",
        "вы общаетесь подозрительно много",
        "мне страшно открывать эту базу, но поздно",
        "социальный эксперимент явно вышел из-под контроля",
    ]
    if messages < 50:
        variants += ["пока тихо. я ещё только собираю компромат", "чат молодой, дурь ещё впереди"]
    if memes >= 10:
        variants += ["локальных приколов уже больше, чем здравого смысла"]
    if chaos >= 0.75:
        variants += ["уровень хаоса официально нездоровый"]
    if photos >= 50:
        variants += ["фотопапка уже выглядит как вещдок"]
    return random.choice(variants)


@router.message(Command("mstats"))
async def cmd_stats(message: Message) -> None:
    chat_id = message.chat.id
    count = await db.count_messages(chat_id)
    total_words, unique_words = await db.get_word_stats(chat_id)
    participants = await db.count_chat_users(chat_id)
    gm_count, meme_count = await group_memory.stats(chat_id)
    user_mem_count = await db.count_user_memories(chat_id)
    photo_count = await db.count_photos(chat_id)
    top_chatter = await db.get_top_chatter(chat_id)
    top_meme = await group_memory.top_meme(chat_id)
    word_counts = await db.get_word_counts(chat_id, limit=30)
    s = await chat_settings(chat_id)

    # Команды, цифры и совсем короткая ерунда в "слово чата" не тащим.
    # Иначе победит какая-нибудь "mgen" или "123", что выглядит уныло.
    top_word = None
    top_word_count = 0
    for word, amount in word_counts.items():
        clean = word.strip()
        if len(clean) < 2 or clean.startswith("m") and clean in {"mgen", "mdem", "mcit", "mstats", "mset", "mvpn", "mhelp", "mforget"}:
            continue
        if clean.isdigit():
            continue
        top_word = clean
        top_word_count = int(amount)
        break

    top_word_line = (
        f'слово чата: “{top_word}” ×{_pretty_num(top_word_count)}'
        if top_word else "слово чата: пока не определилось"
    )
    chatter_line = (
        f"главный спамер: {_stats_user_label(top_chatter)} — {_pretty_num(int(top_chatter['message_count']))} сообщ."
        if top_chatter else "главный спамер: пока никто"
    )
    meme_line = (
        f'локальный мем: “{_clean_stat_fragment(top_meme.text)}” ×{top_meme.occurrences}'
        if top_meme else "локальный мем: ещё не родился"
    )

    diagnosis = _chat_diagnosis(
        messages=count,
        memes=meme_count,
        chaos=float(s["chaos"]),
        photos=photo_count,
    )

    stats_text = "\n".join([
        "📊 ЧАТ",
        f"{_pretty_num(count)} сообщений",
        f"{_pretty_num(total_words)} слов · {_pretty_num(unique_words)} уникальных",
        f"{_pretty_num(participants)} участников",
        "",
        "🧠 ПАМЯТЬ",
        f"{_pretty_num(user_mem_count)} карточек людей",
        f"{_pretty_num(gm_count)} воспоминаний",
        f"{_pretty_num(photo_count)} фоток",
        "",
        "🔥 ТОПЫ",
        top_word_line,
        chatter_line,
        meme_line,
        "",
        "🤖 МЁМОЧКА",
        f"самостоятельность: {'✅' if bool(s['auto_enabled']) else '❌'}",
        f"шанс вмешательства: {float(s['auto_reply_chance']) * 100:.0f}%",
        f"хаос: {float(s['chaos']) * 100:.0f}%",
        f"короткие реакции: {settings.short_reaction_chance * 100:.0f}%",
        f"авто-демотиваторы: {'✅' if settings.auto_demotivators_enabled else '❌'}",
        "",
        f"диагноз: {diagnosis}",
    ])
    await message.answer(stats_text)


@router.message(Command("mgen"))
async def cmd_gen(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) >= 2 and parts[1].startswith("@"):
        profile = await db.get_user_by_username(message.chat.id, parts[1])
        if not profile:
            await message.answer("я пока не знаю этого @username. пусть он сначала что-нибудь напишет")
            return
        seed = parts[2].strip() if len(parts) >= 3 else None
        label = user_label(profile)
        text = await build_parody(message.chat.id, int(profile["user_id"]), label, seed)
        if not text:
            await message.answer("мозг чето отъехал. глянь логи/openrouter")
            return
        await send_plain(message, f"пародия на {label}:\n{text}")
        return
    seed = parts[1].strip() if len(parts) >= 2 else (_text_of(message.reply_to_message) or None)

    fake_prompt = seed or "скажи что-нибудь уместное по текущему чату"
    context_text, events = await build_context(message.chat.id)
    all_cards = await db.get_all_user_memories(message.chat.id, limit=settings.max_user_memory_cards)
    history = await db.get_messages(message.chat.id, limit=900)
    counts = await db.get_word_counts(message.chat.id)
    gm = await group_memory_lines(message.chat.id, fake_prompt, events)
    p = reply_user_prompt(
        user_id=message.from_user.id if message.from_user else 0,
        first_name=message.from_user.full_name if message.from_user else "",
        username=message.from_user.username if message.from_user else "",
        text=fake_prompt,
        reply_context="(команда /mgen)",
        current_memory="",
        all_memories=format_user_memories(all_cards),
        group_memories=gm,
        context=context_text,
        style_examples="\n".join(f"- {x}" for x in _style_examples(history, settings.style_examples)),
        top_words=_top_words(counts),
    )
    s = await chat_settings(message.chat.id)
    result = await ai.generate_reply(user_prompt=p, temperature=0.85 + 0.55*float(s["chaos"]))
    if result.text:
        await send_plain(message, result.text)
    else:
        await message.answer("мозг чето отъехал. глянь логи/openrouter")


@router.message(F.text.regexp(r"^/(?:mcit)(?:@\w+)?(?:\s|$)"))
async def cmd_quote(message: Message) -> None:
    # Через regexp ловим /mcit с возможным @botname и не плодим лишнюю магию.
    reply = message.reply_to_message
    if not reply:
        await message.answer("ответь /mcit на сообщение, которое надо увековечить")
        return

    body = _text_of(reply)
    if not body:
        await message.answer("в этом сообщении нечего цитировать, там пусто")
        return

    # Не даём карточке разъехаться в космос, если кто-то решил процитировать полромана.
    result = make_quote_card(body, author=quote_author_label(reply), title="цитаты великих людей")
    sent = await message.answer_photo(BufferedInputFile(result, filename="quote.jpg"))
    await remember_bot_message(sent, f"[цитата] {body[:180]}")


@router.message(Command("mvpn"))
async def cmd_vpn(message: Message) -> None:
    await message.answer(
        'Искали стабильный и быстрый VPN? Считайте что вы его нашли '
        '"Маркевич ВПН" - скорость и стабильность по приятным ценам! '
        'Подключайтесь по ссылке ниже : https://sub.djtoshik.ru/0wFco1xWSq7uh__p'
    )


@router.message(Command("mforget"))
async def cmd_forget(message: Message, bot: Bot) -> None:
    if not await is_chat_admin(message, bot):
        await message.answer("стереть память могут только админы")
        return
    deleted = await db.clear_chat(message.chat.id)
    await group_memory.clear_chat(message.chat.id)
    RECENT_LIVE_MESSAGES.pop(message.chat.id, None)
    LAST_AUTO_ACTION_AT.pop(message.chat.id, None)
    LAST_AI_CHECK_AT.pop(message.chat.id, None)
    LAST_HUMAN_MESSAGE_AT.pop(message.chat.id, None)
    LAST_IDLE_ATTEMPT_AT.pop(message.chat.id, None)
    LAST_AUTO_DEMOTIVATOR_AT.pop(message.chat.id, None)
    ACTIVE_GROUP_CHATS.discard(message.chat.id)
    cancel_scheduled_intervention(message.chat.id)
    await message.answer(f"забыл {deleted} сообщений, людей, факты и локальные приколы этого чата")


@router.message(Command("mdem"))
async def cmd_dem(message: Message, bot: Bot) -> None:
    reply = message.reply_to_message
    custom = None
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) == 2:
            custom = parts[1].strip() or None

    stored_photo: dict[str, Any] | None = None
    if reply and reply.photo:
        file_id = reply.photo[-1].file_id
        photo_hint = "фото выбрано пользователем через reply"
    else:
        stored_photo = await db.pick_photo_for_demotivator(message.chat.id)
        if not stored_photo:
            await message.answer("не нашёл сохранённых фоток. ответь /mdem на фото или сначала накопи фотки в чате")
            return
        file_id = str(stored_photo["file_id"])
        photo_hint = f"это старое фото из чата, его отправил {user_label(stored_photo, fallback='кто-то')}"
        old_caption = " ".join(str(stored_photo.get("caption") or "").split()).strip()
        if old_caption:
            photo_hint += f"; исходная подпись: {old_caption[:300]}"

    context_text, _ = await build_context(message.chat.id)
    history = await db.get_messages(message.chat.id, limit=700)
    prompt = (
        (f"заголовок уже задан пользователем и менять его нельзя: {custom}\n" if custom else "")
        + f"информация о фото: {photo_hint}\n\n"
        + f"последний контекст:\n{context_text[-5000:]}\n\n"
        + "примеры речи:\n" + "\n".join(f"- {x}" for x in _style_examples(history, 16))
    )
    caption = await ai.generate_demotivator_caption(user_prompt=prompt, custom_title=custom)
    if not caption:
        await message.answer("не придумалось. openrouter чето выпендривается")
        return
    title, subtitle = caption
    tg_file = await bot.get_file(file_id)
    image_bytes = BytesIO()
    await bot.download_file(tg_file.file_path, destination=image_bytes)
    result = make_demotivator(image_bytes.getvalue(), title=title, subtitle=subtitle)
    await message.answer_photo(BufferedInputFile(result, filename="demotivator.jpg"))
    if stored_photo:
        await db.mark_photo_used(int(stored_photo["id"]))


# Фото обрабатываем отдельно от текста. На диск оригиналы не складируем: сохраняем file_id,
# и Telegram потом сам отдаст файл, когда понадобится демотиватор. Дешево и без мусорной папки.
@router.message(F.photo)
async def on_photo(message: Message, bot: Bot) -> None:
    if not message.from_user or message.from_user.is_bot or not message.photo:
        return

    photo = message.photo[-1]
    await db.add_photo(
        chat_id=message.chat.id,
        message_id=message.message_id,
        user_id=message.from_user.id,
        file_id=photo.file_id,
        file_unique_id=photo.file_unique_id,
        caption=message.caption,
        username=message.from_user.username,
        display_name=message.from_user.full_name,
    )

    caption_text = _text_of(message)
    if caption_text and not caption_text.startswith("/"):
        ctx = reply_context_from_message(message)
        inserted = await db.add_user_message(
            chat_id=message.chat.id,
            message_id=message.message_id,
            user_id=message.from_user.id,
            text=caption_text,
            username=message.from_user.username,
            display_name=message.from_user.full_name,
            **ctx,
        )
        if inserted:
            await group_memory.observe(
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                text=caption_text,
                username=message.from_user.username,
                display_name=message.from_user.full_name,
                source_message_id=message.message_id,
                reply_to_text=ctx.get("reply_to_text") or None,
            )
            curator.schedule(MemoryObservation(
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                first_name=message.from_user.full_name or "",
                username=message.from_user.username or "",
                text=caption_text,
                reply_context=format_reply_context(ctx),
            ))

    remember_live_user_message(message)
    if message.chat.type in {"group", "supergroup"}:
        ACTIVE_GROUP_CHATS.add(message.chat.id)
        LAST_HUMAN_MESSAGE_AT[message.chat.id] = time.monotonic()

    if caption_text and (message.chat.type == "private" or is_directed_at_bot(message)):
        cancel_scheduled_intervention(message.chat.id)
        try:
            await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        except Exception:
            pass
        generated = await build_direct_reply(message)
        if generated:
            await send_reply_to_message(message, generated)
        else:
            await message.reply("чето мозг отвалился. глянь openrouter")
        return

    if message.chat.type not in {"group", "supergroup"}:
        return
    if await db.count_messages(message.chat.id) < settings.min_messages_for_auto_reply:
        return
    s = await chat_settings(message.chat.id)
    if not bool(s["auto_enabled"]):
        cancel_scheduled_intervention(message.chat.id)
        return
    schedule_delayed_intervention(bot, message.chat.id, message)


# Главный обработчик обычной беседы. Порядок важен: сначала БД и память, потом прямой ответ,
# потом планирование автономности. Если переставить всё наугад, AI начнёт видеть контекст с дырками.
@router.message(F.text)
async def on_text(message: Message, bot: Bot) -> None:
    if not message.text or message.text.startswith("/"):
        return
    if not message.from_user or message.from_user.is_bot:
        return
    text = message.text.strip()
    if not text:
        return
    ctx = reply_context_from_message(message)
    inserted = await db.add_user_message(
        chat_id=message.chat.id,
        message_id=message.message_id,
        user_id=message.from_user.id,
        text=text,
        username=message.from_user.username,
        display_name=message.from_user.full_name,
        **ctx,
    )
    if not inserted:
        return
    await group_memory.observe(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        text=text,
        username=message.from_user.username,
        display_name=message.from_user.full_name,
        source_message_id=message.message_id,
        reply_to_text=ctx.get("reply_to_text") or None,
    )
    remember_live_user_message(message)

    curator.schedule(MemoryObservation(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        first_name=message.from_user.full_name or "",
        username=message.from_user.username or "",
        text=text,
        reply_context=format_reply_context(ctx),
    ))

    if message.chat.type in {"group", "supergroup"}:
        ACTIVE_GROUP_CHATS.add(message.chat.id)
        LAST_HUMAN_MESSAGE_AT[message.chat.id] = time.monotonic()

    directed = message.chat.type == "private" or is_directed_at_bot(message)
    if directed:
        cancel_scheduled_intervention(message.chat.id)
        try:
            await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        except Exception:
            pass
        generated = await build_direct_reply(message)
        if generated:
            await send_reply_to_message(message, generated)
        else:
            await message.reply("чето мозг отвалился. глянь openrouter")
        return

    if message.chat.type not in {"group","supergroup"}:
        return
    if await db.count_messages(message.chat.id) < settings.min_messages_for_auto_reply:
        return
    s = await chat_settings(message.chat.id)
    if not bool(s["auto_enabled"]):
        cancel_scheduled_intervention(message.chat.id)
        return

    # Debounce: не решаем вмешиваться в ту же миллисекунду, когда пришло сообщение.
    # Каждое новое сообщение переносит момент размышления.
    schedule_delayed_intervention(bot, message.chat.id, message)



async def main() -> None:
    # Инициализируем хранилища, узнаём собственный Telegram id, запускаем idle-задачу
    # и polling. На shutdown фоновые задачи обязательно отменяем, иначе asyncio будет материться.
    global BOT_ID, BOT_USERNAME, BOT_DISPLAY_NAME
    setup_logging()
    if not settings.bot_token:
        raise RuntimeError("Не задан BOT_TOKEN в .env")
    if not settings.openrouter_api_key:
        raise RuntimeError("Не задан OPENROUTER_API_KEY в .env")
    await db.init()
    await group_memory.init()
    bot = Bot(settings.bot_token)
    try:
        # Make использует webhook. Для перехода на сервер удаляем старый webhook автоматически.
        await bot.delete_webhook(drop_pending_updates=False)
        me = await bot.get_me()
        BOT_ID = me.id
        BOT_USERNAME = (me.username or "").casefold()
        BOT_DISPLAY_NAME = me.full_name or "мёмочка"
        dp = Dispatcher()
        dp.include_router(router)
        await bot.set_my_commands([
            BotCommand(command="mgen", description="спиздануть что-нибудь / пародия на @username"),
            BotCommand(command="mdem", description="демотиватор из фото или памяти чата"),
            BotCommand(command="mcit", description="карточка-цитата по reply"),
            BotCommand(command="mstats", description="память и статистика"),
            BotCommand(command="mset", description="настройки самостоятельности"),
            BotCommand(command="mvpn", description="Маркевич ВПН"),
            BotCommand(command="mforget", description="стереть память чата"),
            BotCommand(command="mhelp", description="помощь"),
        ])
        logging.info("Memochka autonomous patch started: @%s, model=%s", BOT_USERNAME, settings.openrouter_model)
        idle_task = asyncio.create_task(idle_chat_loop(bot))
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        finally:
            idle_task.cancel()
            await asyncio.gather(idle_task, return_exceptions=True)
            for task in list(PENDING_AUTO_TASKS.values()):
                task.cancel()
            if PENDING_AUTO_TASKS:
                await asyncio.gather(*PENDING_AUTO_TASKS.values(), return_exceptions=True)
            PENDING_AUTO_TASKS.clear()
    finally:
        await curator.close()
        await ai.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())