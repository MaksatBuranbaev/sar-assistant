"""
config.py — единая точка чтения настроек.

Все параметры приходят из окружения (.env локально, переменные контейнера в проде),
в коде нет ни одного захардкоженного хоста, пароля или ключа.
"""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "да")


@dataclass(frozen=True)
class DBConfig:
    host: str = os.getenv("PG_HOST", "localhost")
    port: int = _int("PG_PORT", 5432)
    dbname: str = os.getenv("PG_DB", "pss_db")
    user: str = os.getenv("PG_USER", "pss_readonly")
    password: str = os.getenv("PG_PASSWORD", "")

    # Оба параметра уходят в libpq через `options` и действуют на всю сессию.
    # Дублируют то, что задано на уровне роли (sql/readonly_role.sql), — чтобы
    # приложение оставалось безопасным даже если БД поднял кто-то другой.
    statement_timeout_ms: int = _int("PG_STATEMENT_TIMEOUT_MS", 8000)
    connect_timeout_s: int = _int("PG_CONNECT_TIMEOUT_S", 5)

    pool_min_size: int = _int("PG_POOL_MIN", 1)
    pool_max_size: int = _int("PG_POOL_MAX", 4)

    # Таблицы, к которым ассистенту разрешено обращаться. Тот же список
    # используется валидатором как allowlist (sql_guard.validate).
    allowed_tables: frozenset[str] = frozenset({"pss_departures", "pss_lessons"})


@dataclass(frozen=True)
class LLMConfig:
    credentials: str | None = os.getenv("GIGACHAT_CREDENTIALS")
    scope: str = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
    model: str = os.getenv("GIGACHAT_MODEL", "GigaChat")

    # Сертификаты GigaChat подписаны «Russian Trusted Root CA», которого нет в
    # системном хранилище. Правильный путь — положить его рядом и указать здесь.
    # verify_ssl=False оставлен как аварийный вариант для локальной отладки и
    # сопровождается предупреждением в логе (см. assistant._client).
    ca_bundle_file: str | None = os.getenv("GIGACHAT_CA_BUNDLE") or None
    verify_ssl: bool = _bool("GIGACHAT_VERIFY_SSL", False)

    # Генерация SQL — детерминированная задача, креативность вредна.
    temperature_sql: float = 0.1
    temperature_answer: float = 0.3
    timeout_s: int = _int("GIGACHAT_TIMEOUT_S", 60)
    max_retries: int = _int("GIGACHAT_MAX_RETRIES", 3)


@dataclass(frozen=True)
class AppConfig:
    # Максимум строк, который валидатор навешивает на запрос без LIMIT.
    max_rows: int = _int("MAX_ROWS", 500)
    # Сколько строк результата уходит во второй вызов модели.
    max_rows_to_llm: int = _int("MAX_ROWS_TO_LLM", 50)
    # Попыток сгенерировать корректный SQL: первая + исправления по ошибке БД.
    max_sql_attempts: int = _int("MAX_SQL_ATTEMPTS", 3)
    # Сколько похожих примеров «вопрос → SQL» подмешивать в промпт.
    few_shot_k: int = _int("FEW_SHOT_K", 4)
    # Сколько предыдущих ходов диалога передавать модели.
    history_turns: int = _int("HISTORY_TURNS", 3)
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    db: DBConfig = field(default_factory=DBConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)


settings = AppConfig()
