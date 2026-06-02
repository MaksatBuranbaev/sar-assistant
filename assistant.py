"""
assistant.py — Text-to-SQL пайплайн поверх GigaChat.

Схема одного вопроса:

    вопрос
      │
      ├─ retriever.search()        → k похожих примеров «вопрос → SQL» (RAG)
      ├─ db.get_schema()           → схема из information_schema + частые значения
      │
      ├─ GigaChat #1               → SQL
      ├─ sql_guard.validate()      → AST-проверка + принудительный LIMIT
      ├─ db.execute_generated_sql()→ строки
      │     ↑ ошибка валидатора или БД возвращается модели вместе с её же SQL,
      │       и генерация повторяется (self-correction, до MAX_SQL_ATTEMPTS)
      │
      └─ GigaChat #2               → ответ на русском по полученным строкам

Почему self-correction: большая часть ошибок модели — не логика, а мелочи
(неверное имя колонки, забытый GROUP BY, неподходящий тип). Модель исправляет
их сама, если показать ей текст ошибки PostgreSQL.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

import httpx
import psycopg
from gigachat import GigaChat
from gigachat.models import Chat, Messages, MessagesRole
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

import db
from config import settings
from retriever import Example, format_examples, get_retriever
from sql_guard import SqlValidationError

logger = logging.getLogger(__name__)

NOT_APPLICABLE = "NOT_APPLICABLE"

SQL_SYSTEM_PROMPT = """Ты — аналитический ассистент Поисково-спасательной службы МЧС.
Ты переводишь вопросы пользователя в корректные SELECT-запросы к PostgreSQL.

СХЕМА БАЗЫ ДАННЫХ:
{schema}

ПРАВИЛА:
1. Возвращай ТОЛЬКО SQL внутри блока ```sql ... ```. Никаких пояснений вне блока.
2. Только SELECT и только один запрос. Никаких INSERT, UPDATE, DELETE, DROP, ALTER,
   нескольких операторов через точку с запятой.
3. Обращайся только к таблицам pss_departures и pss_lessons.
4. Строковые сравнения — через ILIKE. Опирайся на фактические значения колонок,
   перечисленные в схеме выше: если в данных тип происшествия записан как
   'ДТП (столкновение)', то фильтр должен быть incident_type ILIKE '%ДТП%'.
5. Даты — через EXTRACT(YEAR FROM date), EXTRACT(MONTH FROM date),
   EXTRACT(QUARTER FROM date).
6. В агрегатах учитывай NULL: COALESCE(SUM(x), 0), а в AVG добавляй
   WHERE x IS NOT NULL.
7. Перечисляй нужные колонки явно вместо SELECT *, если вопрос этого не требует.
8. Давай понятные псевдонимы колонкам агрегатов (AS departures, AS avg_total_min).
9. Если вопрос не относится к данным ПСС — верни ровно:
   ```sql
   SELECT 'NOT_APPLICABLE' AS result;
   ```
"""

ANSWER_SYSTEM_PROMPT = """Ты — аналитический ассистент МЧС ПСС.
Тебе дают вопрос пользователя и результат запроса к базе данных.

ПРАВИЛА:
1. Отвечай на русском языке, кратко и по существу.
2. Используй ТОЛЬКО те числа, которые есть в результате. Ничего не досчитывай
   в уме и не додумывай — если данных для вывода не хватает, так и скажи.
3. Если результат пустой — сообщи об этом прямо и предположи причину
   (нет записей за период, слишком узкий фильтр).
4. Если в результате есть NOT_APPLICABLE — объясни, что вопрос не относится
   к данным о выездах и занятиях ПСС.
5. Не упоминай SQL, названия таблиц и колонок, технические детали.
6. Если строк много — дай сводку и выдели главное, не перечисляй всё подряд.
"""

SQL_FIX_PROMPT = """Твой предыдущий запрос не выполнился.

Запрос:
```sql
{sql}
```

Ошибка:
{error}

Исправь запрос с учётом схемы и правил. Верни ТОЛЬКО исправленный SQL
в блоке ```sql ... ```."""


@dataclass
class Turn:
    """Один ход диалога — нужен, чтобы работали уточняющие вопросы."""
    question: str
    sql: str | None = None


@dataclass
class AskResult:
    question: str
    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    answer: str = ""
    error: str | None = None
    attempts: int = 0
    elapsed_ms: int = 0
    examples_used: list[str] = field(default_factory=list)
    not_applicable: bool = False


# --------------------------------------------------------------------------- #
# Клиент GigaChat
# --------------------------------------------------------------------------- #

_client: GigaChat | None = None


def get_client() -> GigaChat:
    """Один клиент на процесс.

    Раньше клиент создавался на каждый вопрос, а значит на каждый вопрос
    заново выпускался OAuth-токен — лишний сетевой круг перед каждым запросом.
    """
    global _client
    if _client is None:
        llm = settings.llm
        if not llm.credentials:
            raise RuntimeError(
                "Не задан GIGACHAT_CREDENTIALS — заполните .env по образцу .env.example."
            )
        if not llm.verify_ssl and not llm.ca_bundle_file:
            logger.warning(
                "Проверка TLS-сертификата GigaChat отключена. Так можно только "
                "локально: соединение уязвимо к MITM. Для прода укажите "
                "GIGACHAT_CA_BUNDLE (корневой сертификат «Russian Trusted Root CA») "
                "и GIGACHAT_VERIFY_SSL=true."
            )
        _client = GigaChat(
            credentials=llm.credentials,
            scope=llm.scope,
            model=llm.model,
            ca_bundle_file=llm.ca_bundle_file,
            verify_ssl_certs=llm.verify_ssl,
            timeout=llm.timeout_s,
        )
    return _client


def reset_client() -> None:
    global _client
    _client = None


# Сетевые сбои и 5xx лечатся повтором, ошибки в самом запросе — нет,
# поэтому ретраим только транспортные исключения и таймауты.
_RETRYABLE = (httpx.TransportError, httpx.TimeoutException)


@retry(
    retry=retry_if_exception_type(_RETRYABLE),
    stop=stop_after_attempt(settings.llm.max_retries),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
def _chat(messages: list[Messages], temperature: float) -> str:
    client = get_client()
    response = client.chat(
        Chat(
            messages=messages,
            model=settings.llm.model,
            temperature=temperature,
        )
    )
    return response.choices[0].message.content


# --------------------------------------------------------------------------- #
# Разбор ответа модели
# --------------------------------------------------------------------------- #

# Язык в открывающем ограждении должен быть либо `sql`, либо отсутствовать:
# иначе ```python ... ``` тоже уезжал бы в валидатор как «запрос».
_SQL_BLOCK = re.compile(r"```[ \t]*(?:sql)?[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_sql(text: str) -> str:
    """Достаёт SQL из ответа модели.

    Постобработки регулярками здесь больше нет: раньше _fix_sql() чинил
    LIMIT, уехавший за точку с запятой, потому что промпт просил модель
    ставить его самостоятельно. Теперь LIMIT навешивает sql_guard на уровне
    AST, и чинить нечего.
    """
    match = _SQL_BLOCK.search(text or "")
    if match:
        candidate = match.group(1).strip()
        if candidate:
            return candidate

    candidate = (text or "").strip()
    if re.match(r"^\s*(SELECT|WITH)\b", candidate, re.IGNORECASE):
        return candidate

    raise SqlValidationError(
        "Модель вернула ответ без SQL-запроса. Попробуйте переформулировать вопрос."
    )


def rows_to_text(columns: list[str], rows: list[dict], limit: int) -> str:
    """Готовит результат запроса для второго вызова модели.

    В модель уходит ограниченное число строк: это и токены, и приватность —
    в description_raw попадают адреса и обстоятельства происшествий.
    Полную таблицу пользователь видит в интерфейсе напрямую, минуя LLM.
    """
    if not rows:
        return "Результат пустой — подходящих записей не найдено."

    preview = rows[:limit]
    header = " | ".join(str(c) for c in columns)
    body = "\n".join(
        " | ".join("" if row.get(c) is None else str(row.get(c)) for c in columns)
        for row in preview
    )
    note = f"\n(показано {len(preview)} строк из {len(rows)})" if len(rows) > limit else ""
    return f"{header}\n{'-' * min(len(header), 120)}\n{body}{note}"


def _is_not_applicable(columns: list[str], rows: list[dict]) -> bool:
    return any(
        isinstance(value, str) and value == NOT_APPLICABLE
        for row in rows
        for value in row.values()
    )


# --------------------------------------------------------------------------- #
# Построение промпта
# --------------------------------------------------------------------------- #

def _build_sql_messages(
    question: str,
    schema: str,
    examples: list[Example],
    history: list[Turn],
) -> list[Messages]:
    system = SQL_SYSTEM_PROMPT.format(schema=schema)

    examples_block = format_examples(examples)
    if examples_block:
        system += (
            "\nПРИМЕРЫ ПОХОЖИХ ЗАПРОСОВ (ориентируйся на их стиль):\n\n"
            + examples_block
            + "\n"
        )

    messages = [Messages(role=MessagesRole.SYSTEM, content=system)]

    # История нужна для уточняющих вопросов вида «а теперь по районам?»:
    # без неё модель не знает, что уточняют, и генерирует запрос с нуля.
    for turn in history[-settings.history_turns:]:
        messages.append(Messages(role=MessagesRole.USER, content=turn.question))
        if turn.sql:
            messages.append(
                Messages(role=MessagesRole.ASSISTANT, content=f"```sql\n{turn.sql}\n```")
            )

    messages.append(Messages(role=MessagesRole.USER, content=question))
    return messages


def _describe_db_error(error: Exception) -> str:
    """Человекочитаемый текст ошибки БД — он же уходит модели на исправление."""
    if isinstance(error, psycopg.errors.QueryCanceled):
        return (
            "Запрос выполнялся слишком долго и был остановлен по таймауту. "
            "Упростите его: добавьте фильтр или агрегацию."
        )
    if isinstance(error, psycopg.Error):
        message = (error.diag.message_primary if error.diag else None) or str(error)
        hint = error.diag.message_hint if error.diag else None
        return f"{message}" + (f" (подсказка PostgreSQL: {hint})" if hint else "")
    return str(error)


# --------------------------------------------------------------------------- #
# Основной сценарий
# --------------------------------------------------------------------------- #

def generate_sql(
    question: str,
    schema: str | None = None,
    history: list[Turn] | None = None,
) -> tuple[str, list[Example]]:
    """Генерирует SQL без выполнения — используется в тестах и в eval."""
    schema = schema if schema is not None else db.get_schema()
    examples = get_retriever().search(question, k=settings.few_shot_k)
    messages = _build_sql_messages(question, schema, examples, history or [])
    raw = _chat(messages, temperature=settings.llm.temperature_sql)
    return extract_sql(raw), examples


def ask(question: str, history: list[Turn] | None = None) -> AskResult:
    """
    Полный цикл: вопрос → SQL → данные → ответ на русском.

    Никогда не бросает исключений — все отказы возвращаются в AskResult.error,
    чтобы интерфейс мог показать понятное сообщение вместо трейсбэка.
    """
    started = time.monotonic()
    result = AskResult(question=question)
    history = history or []

    try:
        schema = db.get_schema()
    except db.DatabaseUnavailable as e:
        result.error = str(e)
        result.answer = "База данных недоступна, повторите попытку позже."
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.error("БД недоступна: %s", e)
        return result

    examples = get_retriever().search(question, k=settings.few_shot_k)
    result.examples_used = [ex.question for ex in examples]
    messages = _build_sql_messages(question, schema, examples, history)

    last_error: str | None = None
    columns: list[str] = []
    rows: list[dict] = []

    for attempt in range(1, settings.max_sql_attempts + 1):
        result.attempts = attempt
        try:
            raw = _chat(messages, temperature=settings.llm.temperature_sql)
            sql = extract_sql(raw)
            result.sql = sql

            columns, rows = db.execute_generated_sql(sql)
            last_error = None
            break

        except (SqlValidationError, psycopg.Error) as e:
            # Ошибка, которую модель может исправить сама: невалидный запрос,
            # несуществующая колонка, неверный тип, таймаут.
            last_error = _describe_db_error(e)
            logger.warning("попытка %s/%s не удалась: %s",
                           attempt, settings.max_sql_attempts, last_error)
            if attempt < settings.max_sql_attempts:
                messages = messages + [
                    Messages(role=MessagesRole.ASSISTANT, content=f"```sql\n{result.sql}\n```"),
                    Messages(
                        role=MessagesRole.USER,
                        content=SQL_FIX_PROMPT.format(sql=result.sql or "", error=last_error),
                    ),
                ]

        except db.DatabaseUnavailable as e:
            # Повторять генерацию бессмысленно — проблема не в запросе.
            result.error = str(e)
            result.answer = "База данных недоступна, повторите попытку позже."
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            return result

        except _RETRYABLE as e:
            result.error = f"Сервис GigaChat недоступен: {e}"
            result.answer = "Не удалось связаться с языковой моделью, повторите попытку позже."
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            return result

        except Exception as e:  # noqa: BLE001 — последний рубеж, чтобы UI не упал
            logger.exception("непредвиденная ошибка при обработке вопроса")
            result.error = f"Непредвиденная ошибка: {e}"
            result.answer = "Произошла внутренняя ошибка при обработке запроса."
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            return result

    if last_error is not None:
        result.error = last_error
        result.answer = (
            f"Не удалось построить корректный запрос за {settings.max_sql_attempts} "
            "попытки. Попробуйте переформулировать вопрос — например, уточнить "
            "период или район."
        )
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    result.columns = columns
    result.rows = rows
    result.not_applicable = _is_not_applicable(columns, rows)

    try:
        data_text = rows_to_text(columns, rows, settings.max_rows_to_llm)
        result.answer = _chat(
            [
                Messages(role=MessagesRole.SYSTEM, content=ANSWER_SYSTEM_PROMPT),
                Messages(
                    role=MessagesRole.USER,
                    content=f"Вопрос пользователя: {question}\n\nРезультат из базы данных:\n{data_text}",
                ),
            ],
            temperature=settings.llm.temperature_answer,
        )
    except Exception as e:  # noqa: BLE001
        # Данные уже получены — показываем их, даже если пересказать не удалось.
        logger.warning("не удалось сформировать текстовый ответ: %s", e)
        result.error = f"Не удалось сформировать текстовый ответ: {e}"
        result.answer = f"Запрос выполнен, получено строк: {len(rows)}. Результат — в таблице ниже."

    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "вопрос обработан за %s мс, попыток: %s, строк: %s",
        result.elapsed_ms, result.attempts, len(rows),
    )
    return result
