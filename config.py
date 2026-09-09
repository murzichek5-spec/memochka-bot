"""Конфиг Мёмочки.

Тут собраны все крутилки из .env. Если добавляешь новую переменную окружения —
добавь её и в Settings/load_settings, иначе она будет лежать в .env для красоты.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


# .env всегда отдаёт строки. Эти помощники нужны, чтобы по всему проекту
# не разводить одинаковые try/except и не ловить ебанину из-за "0,25" вместо "0.25".
def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() not in {"0", "false", "off", "no", "нет"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).replace(",", "."))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


# Один объект со всеми настройками удобнее сотни os.getenv по всему проекту.
# frozen=True: после старта настройки не должны тихо мутировать у нас за спиной.
@dataclass(frozen=True)
class Settings:
    bot_token: str
    openrouter_api_key: str
    openrouter_model: str
    openrouter_timeout_seconds: float
    openrouter_max_retries: int
    db_path: str

    default_auto_reply_chance: float
    default_creativity: float
    min_messages_for_auto_reply: int
    auto_cooldown_seconds: float
    ai_check_cooldown_seconds: float
    auto_delay_min_seconds: float
    auto_delay_max_seconds: float
    idle_scan_interval_seconds: float
    idle_min_silence_seconds: float
    idle_max_silence_seconds: float
    idle_cooldown_seconds: float
    idle_intervention_chance: float
    short_reaction_chance: float

    auto_demotivators_enabled: bool
    auto_demotivator_chance: float
    auto_demotivator_idle_chance: float
    auto_demotivator_cooldown_seconds: float
    auto_demotivator_min_photos: int

    context_messages: int
    style_examples: int
    group_memory_examples: int
    max_stored_messages: int
    max_conversation_log: int
    group_memory_max_per_chat: int
    max_user_memory_cards: int

    reactions_enabled: bool
    memory_curator_enabled: bool
    log_level: str
    bot_name_aliases: tuple[str, ...]


def load_settings() -> Settings:
    # Здесь заодно зажимаем значения в безопасные границы. Если кто-то напишет
    # AUTO_REPLY_CHANCE=1488, бот не должен превращаться в радио, которое херачит без остановки.
    aliases_raw = os.getenv("BOT_NAME_ALIASES", "мёмоч,мемоч,memochka")
    aliases = tuple(x.strip().casefold() for x in aliases_raw.split(",") if x.strip())
    return Settings(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
        openrouter_model=os.getenv("OPENROUTER_MODEL", "google/gemma-4-26b-a4b-it").strip(),
        openrouter_timeout_seconds=_float("OPENROUTER_TIMEOUT_SECONDS", 45.0),
        openrouter_max_retries=max(0, _int("OPENROUTER_MAX_RETRIES", 2)),
        db_path=os.getenv("DB_PATH", "bot.db").strip() or "bot.db",

        default_auto_reply_chance=max(0.0, min(_float("AUTO_REPLY_CHANCE", 0.50), 1.0)),
        default_creativity=max(0.0, min(_float("CREATIVITY", 0.64), 1.0)),
        min_messages_for_auto_reply=max(0, _int("MIN_MESSAGES_FOR_AUTO_REPLY", 10)),
        auto_cooldown_seconds=max(0.0, _float("AUTO_COOLDOWN_SECONDS", 25.0)),
        ai_check_cooldown_seconds=max(0.0, _float("AI_CHECK_COOLDOWN_SECONDS", 6.0)),
        auto_delay_min_seconds=max(1.0, _float("AUTO_DELAY_MIN_SECONDS", 18.0)),
        auto_delay_max_seconds=max(1.0, _float("AUTO_DELAY_MAX_SECONDS", 45.0)),
        idle_scan_interval_seconds=max(30.0, _float("IDLE_SCAN_INTERVAL_SECONDS", 180.0)),
        idle_min_silence_seconds=max(30.0, _float("IDLE_MIN_SILENCE_SECONDS", 300.0)),
        idle_max_silence_seconds=max(60.0, _float("IDLE_MAX_SILENCE_SECONDS", 7200.0)),
        idle_cooldown_seconds=max(0.0, _float("IDLE_COOLDOWN_SECONDS", 900.0)),
        idle_intervention_chance=max(0.0, min(_float("IDLE_INTERVENTION_CHANCE", 0.22), 1.0)),
        short_reaction_chance=max(0.0, min(_float("SHORT_REACTION_CHANCE", 0.28), 1.0)),

        auto_demotivators_enabled=_bool("AUTO_DEMOTIVATORS_ENABLED", True),
        auto_demotivator_chance=max(0.0, min(_float("AUTO_DEMOTIVATOR_CHANCE", 0.10), 1.0)),
        auto_demotivator_idle_chance=max(0.0, min(_float("AUTO_DEMOTIVATOR_IDLE_CHANCE", 0.18), 1.0)),
        auto_demotivator_cooldown_seconds=max(0.0, _float("AUTO_DEMOTIVATOR_COOLDOWN_SECONDS", 3600.0)),
        auto_demotivator_min_photos=max(1, _int("AUTO_DEMOTIVATOR_MIN_PHOTOS", 6)),

        context_messages=max(8, _int("CONTEXT_MESSAGES", 30)),
        style_examples=max(4, _int("STYLE_EXAMPLES", 16)),
        group_memory_examples=max(4, _int("GROUP_MEMORY_EXAMPLES", 14)),
        max_stored_messages=max(500, _int("MAX_STORED_MESSAGES", 10000)),
        max_conversation_log=max(1000, _int("MAX_CONVERSATION_LOG", 20000)),
        group_memory_max_per_chat=max(100, _int("GROUP_MEMORY_MAX_PER_CHAT", 3000)),
        max_user_memory_cards=max(10, _int("MAX_USER_MEMORY_CARDS", 50)),

        reactions_enabled=_bool("REACTIONS_ENABLED", True),
        memory_curator_enabled=_bool("MEMORY_CURATOR_ENABLED", True),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        bot_name_aliases=aliases,
    )
