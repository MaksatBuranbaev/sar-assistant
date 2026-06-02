"""
db.py — доступ к PostgreSQL.

ПЕРВЫЙ СЛОЙ ЗАЩИТЫ. Три независимых механизма, каждый работает без остальных:

1. Роль. Приложение подключается под ролью с GRANT SELECT ровно на две таблицы
   (sql/readonly_role.sql). DELETE не выполнится, даже если валидатор его пропустит.
2. default_transaction_read_only=on. Любая транзакция в сессии — только чтение.
3. statement_timeout. Запрос, который выполняется дольше лимита, снимается
   сервером. Это ответ на `SELECT pg_sleep(...)` и на декартовы произведения.

Пункты 2 и 3 задаются здесь через параметр libpq `options`, то есть действуют
даже если БД поднял кто-то другой и роль настроить забыли.

Схема БД не хранится в Python: описания таблиц и колонок читаются из
information_schema и COMMENT ON COLUMN. Добавление колонки в БД не требует
правки кода — раньше рассинхронизация приводила к тихой генерации неверного SQL.
"""

from __future__ import annotations

import functools
import logging

import psycopg
import psycopg.rows
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

from config import settings
from sql_guard import SqlValidationError, validate

logger = logging.getLogger(__name__)

# Колонки, по которым имеет смысл показать модели реальные значения из БД.
# Без этого модель угадывает, как записан тип происшествия, и приходится
# держать в промпте костыли вида «если про ДТП — используй ILIKE '%дтп%'».
_ENUM_LIKE_COLUMNS = {
    "pss_departures": ("incident_type", "district", "pss_unit", "object_type", "result"),
    "pss_lessons": ("lesson_type", "district", "pss_unit", "result"),
}
_ENUM_VALUES_LIMIT = 25

_pool: ConnectionPool | None = None


class DatabaseUnavailable(RuntimeError):
    """Не удалось получить соединение с БД."""


def _is_connection_problem(error: psycopg.OperationalError) -> bool:
    """Отличает «база недоступна» от «сервер отверг конкретный запрос».

    psycopg наследует от OperationalError не только обрывы связи, но и,
    например, QueryCanceled — срабатывание statement_timeout. Разница
    принципиальна: обрыв связи повторять бессмысленно, а слишком тяжёлый
    запрос модель может переписать сама. Признак — sqlstate: если сервер
    успел вернуть код ошибки, значит связь была.
    """
    return getattr(error, "sqlstate", None) is None


def _conninfo() -> str:
    db = settings.db
    return make_conninfo(
        host=db.host,
        port=db.port,
        dbname=db.dbname,
        user=db.user,
        password=db.password,
        connect_timeout=db.connect_timeout_s,
        application_name="pss-assistant",
        options=(
            f"-c statement_timeout={db.statement_timeout_ms} "
            f"-c default_transaction_read_only=on "
            f"-c idle_in_transaction_session_timeout=10000"
        ),
    )


def get_pool() -> ConnectionPool:
    """Ленивый пул соединений.

    Открывать соединение на каждый вопрос — это TCP-хендшейк, аутентификация и
    инициализация сессии каждый раз; под нагрузкой это заметно дороже самого
    запроса. Пул переиспользует уже открытые соединения.
    """
    global _pool
    if _pool is None:
        pool = ConnectionPool(
            conninfo=_conninfo(),
            min_size=settings.db.pool_min_size,
            max_size=settings.db.pool_max_size,
            timeout=settings.db.connect_timeout_s,
            open=False,
        )
        # wait=False: приложение поднимается, даже если БД ещё не готова
        # (актуально для docker compose, где контейнеры стартуют параллельно).
        pool.open(wait=False)
        _pool = pool
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _fetch(sql: str, params: tuple | None = None) -> list[dict]:
    """Выполняет доверенный внутренний запрос (интроспекция схемы, health-check).

    Не для SQL, пришедшего от модели, — для него есть execute_generated_sql,
    который сначала прогоняет запрос через валидатор.
    """
    try:
        with (
            get_pool().connection() as conn,
            conn.cursor(row_factory=psycopg.rows.dict_row) as cur,
        ):
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]
    except psycopg.OperationalError as e:
        if _is_connection_problem(e):
            raise DatabaseUnavailable(f"Нет связи с базой данных: {e}") from e
        raise


def ping() -> bool:
    try:
        _fetch("SELECT 1 AS ok")
        return True
    except Exception:
        logger.warning("health-check БД не прошёл", exc_info=True)
        return False


def check_status() -> tuple[bool, str | None]:
    """Готова ли БД обслуживать запросы: (ok, причина отказа).

    Исключений не бросает — результат идёт прямо в индикатор интерфейса.

    Кэширование намеренно оставлено вызывающей стороне и НЕ делается здесь:
    состояние базы меняется во времени, и кэш без срока жизни превращает
    индикатор в ложь — один раз покраснев, он уже не позеленеет, даже когда
    база вернётся.
    """
    if not ping():
        return False, "Нет связи с базой данных"
    try:
        get_schema()
    except Exception as e:  # noqa: BLE001 — причина уходит в интерфейс текстом
        return False, str(e)
    return True, None


_SCHEMA_QUERY = """
SELECT c.relname                                   AS table_name,
       obj_description(c.oid, 'pg_class')          AS table_comment,
       a.attname                                   AS column_name,
       format_type(a.atttypid, a.atttypmod)        AS data_type,
       col_description(c.oid, a.attnum)            AS column_comment,
       a.attnotnull                                AS not_null,
       EXISTS (
           SELECT 1
           FROM pg_index i
           WHERE i.indrelid = c.oid
             AND i.indisprimary
             AND a.attnum = ANY (i.indkey)
       )                                           AS is_primary_key
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
WHERE n.nspname = 'public'
  AND c.relname = ANY(%s)
  AND c.relkind IN ('r', 'v', 'm', 'p')
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY c.relname, a.attnum;
"""


def _enum_values(table: str, column: str) -> list[str]:
    """Наиболее частые значения колонки — чтобы модель не угадывала формулировки."""
    # Идентификаторы нельзя передать параметром, поэтому подставляем их через
    # psycopg.sql.Identifier — он экранирует имя и делает инъекцию невозможной.
    # Сами имена к тому же берутся из _ENUM_LIKE_COLUMNS, а не от пользователя.
    query = psycopg.sql.SQL(
        "SELECT {col}::text AS value, COUNT(*) AS cnt "
        "FROM {tbl} WHERE {col} IS NOT NULL "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT {limit}"
    ).format(
        col=psycopg.sql.Identifier(column),
        tbl=psycopg.sql.Identifier(table),
        limit=psycopg.sql.Literal(_ENUM_VALUES_LIMIT),
    )
    try:
        with (
            get_pool().connection() as conn,
            conn.cursor(row_factory=psycopg.rows.dict_row) as cur,
        ):
            cur.execute(query)
            return [row["value"] for row in cur.fetchall()]
    except Exception:
        logger.debug("не удалось получить значения %s.%s", table, column, exc_info=True)
        return []


@functools.lru_cache(maxsize=1)
def get_schema() -> str:
    """Описание схемы для системного промпта, собранное из самой БД.

    Кэшируется на процесс: схема между запросами не меняется, а интроспекция —
    это несколько запросов к каталогу. Сбросить кэш можно через refresh_schema().
    """
    tables = sorted(settings.db.allowed_tables)
    rows = _fetch(_SCHEMA_QUERY, (list(tables),))

    if not rows:
        raise DatabaseUnavailable(
            "В базе не найдены таблицы: " + ", ".join(tables)
        )

    by_table: dict[str, list[dict]] = {}
    comments: dict[str, str | None] = {}
    for row in rows:
        by_table.setdefault(row["table_name"], []).append(row)
        comments[row["table_name"]] = row["table_comment"]

    blocks = []
    for table, columns in by_table.items():
        header = f"Таблица `{table}`"
        if comments.get(table):
            header += f" — {comments[table]}"
        lines = [header + ":"]

        for col in columns:
            parts = [f"  - {col['column_name']} ({col['data_type']}"]
            if col["is_primary_key"]:
                parts.append(", PK")
            parts.append(")")
            line = "".join(parts)
            if col["column_comment"]:
                line += f" — {col['column_comment']}"
            lines.append(line)

        enum_lines = []
        for column in _ENUM_LIKE_COLUMNS.get(table, ()):
            values = _enum_values(table, column)
            if values:
                listed = ", ".join(f"'{v}'" for v in values)
                enum_lines.append(f"  - {column}: {listed}")
        if enum_lines:
            lines.append("  Фактические значения в данных:")
            lines.extend(enum_lines)

        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def refresh_schema() -> str:
    get_schema.cache_clear()
    return get_schema()


def execute_generated_sql(sql: str) -> tuple[list[str], list[dict]]:
    """
    Выполняет SQL, сгенерированный моделью.

    Валидация вызывается здесь, а не в assistant.py, намеренно: так её
    невозможно случайно обойти, добавив новый вызов БД в другом месте.

    Возвращает (columns, rows). Бросает SqlValidationError, psycopg.Error
    или DatabaseUnavailable — все три assistant.py обрабатывает по-разному.
    """
    safe_sql = validate(
        sql,
        max_rows=settings.max_rows,
        allowed_tables=settings.db.allowed_tables,
    )
    logger.info("выполняется SQL: %s", safe_sql)

    try:
        with get_pool().connection() as conn:
            # Дублирует default_transaction_read_only из options — на случай,
            # если параметр перекрыт на уровне БД или пользователя.
            conn.read_only = True
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(safe_sql)
                if cur.description is None:
                    return [], []
                columns = [desc.name for desc in cur.description]
                rows = [dict(row) for row in cur.fetchall()]
                return columns, rows
    except psycopg.OperationalError as e:
        # QueryCanceled (statement_timeout) — тоже OperationalError, но его
        # нужно отдать наверх как ошибку запроса, чтобы сработал self-correction.
        if _is_connection_problem(e):
            raise DatabaseUnavailable(f"Нет связи с базой данных: {e}") from e
        raise


__all__ = [
    "DatabaseUnavailable",
    "SqlValidationError",
    "check_status",
    "close_pool",
    "execute_generated_sql",
    "get_pool",
    "get_schema",
    "ping",
    "refresh_schema",
]
