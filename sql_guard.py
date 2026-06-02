"""
sql_guard.py — валидация SQL, сгенерированного моделью.

ЭТО ВТОРОЙ СЛОЙ ЗАЩИТЫ, А НЕ ПЕРВЫЙ.

Первый слой — права в БД: приложение ходит под ролью, у которой есть только
SELECT на две таблицы, а транзакция открывается READ ONLY (см. sql/readonly_role.sql
и db.py). Он не обходится в принципе. Валидатор нужен, чтобы:
  * отказать быстро и с понятным пользователю текстом, не дёргая базу;
  * закрыть то, что правами не закрывается: системные каталоги pg_catalog
    читаемы любой ролью, а тяжёлый декартов запрос формально является SELECT;
  * принудительно навесить LIMIT.

Разбор идёт по AST (sqlglot), а не регулярками: проверка вида
`sql.strip().upper().startswith("SELECT")` даёт и ложные срабатывания
(легальный `WITH ... SELECT` отвергается), и пропуски (`SELECT 1; DELETE ...`).
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

DIALECT = "postgres"


class SqlValidationError(ValueError):
    """SQL не прошёл проверку. Текст пригоден для показа пользователю."""


# Типы узлов, недопустимые в запросе на чтение. Список собирается через getattr,
# потому что набор классов sqlglot меняется между версиями (например, Alter в
# старых версиях назывался AlterTable) — иначе валидатор упадёт при обновлении.
_FORBIDDEN_NODE_NAMES = (
    "Insert", "Update", "Delete", "Merge", "Drop", "Create", "Alter", "AlterTable",
    "TruncateTable", "Grant", "Revoke", "Copy", "Command", "Set", "SetItem",
    "Transaction", "Commit", "Rollback", "Use", "Attach", "Detach", "Export",
)
_FORBIDDEN_NODES = tuple(
    node for node in (getattr(exp, name, None) for name in _FORBIDDEN_NODE_NAMES)
    if isinstance(node, type)
)

# Корень запроса: обычный SELECT либо теоретико-множественная операция над ними.
_ALLOWED_ROOTS = tuple(
    node for node in (getattr(exp, name, None)
                      for name in ("Select", "Union", "Except", "Intersect", "Subquery"))
    if isinstance(node, type)
)

# Функции, которые формально вызываются внутри SELECT, но дают либо отказ в
# обслуживании, либо доступ к файловой системе и сети сервера БД.
_FORBIDDEN_FUNCTION_PREFIXES = ("pg_", "lo_", "dblink", "dbms_")
_FORBIDDEN_FUNCTIONS = frozenset({
    "query_to_xml", "xmlelement", "system", "shell",
})

# Схемы, обращение к которым запрещено (утечка метаданных, списка ролей и т.п.).
_FORBIDDEN_SCHEMAS = frozenset({"pg_catalog", "information_schema", "pg_toast"})


def _root_kind_name(statement: exp.Expression) -> str:
    return type(statement).__name__.upper()


def _iter_forbidden_nodes(statement: exp.Expression):
    for node in statement.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            yield node


def _collect_cte_names(statement: exp.Expression) -> set[str]:
    """Имена CTE — это не таблицы БД, их нельзя проверять по allowlist."""
    names = set()
    for cte in statement.find_all(exp.CTE):
        alias = cte.alias_or_name
        if alias:
            names.add(alias.lower())
    return names


def _check_tables(statement: exp.Expression, allowed_tables: frozenset[str]) -> None:
    cte_names = _collect_cte_names(statement)

    for table in statement.find_all(exp.Table):
        name = (table.name or "").lower()
        schema = (table.db or "").lower()

        if schema in _FORBIDDEN_SCHEMAS:
            raise SqlValidationError(
                f"Обращение к системной схеме `{schema}` запрещено."
            )
        # Ссылка на CTE, объявленный в этом же запросе.
        if not schema and name in cte_names:
            continue
        if schema and schema != "public":
            raise SqlValidationError(f"Схема `{schema}` недоступна ассистенту.")
        if name not in allowed_tables:
            allowed = ", ".join(sorted(allowed_tables))
            raise SqlValidationError(
                f"Таблица `{name}` недоступна. Разрешены только: {allowed}."
            )


def _check_functions(statement: exp.Expression) -> None:
    # Функции, неизвестные sqlglot (в том числе все pg_*), попадают в Anonymous.
    for node in statement.find_all(exp.Anonymous):
        name = str(node.this or "").lower()
        if name in _FORBIDDEN_FUNCTIONS or name.startswith(_FORBIDDEN_FUNCTION_PREFIXES):
            raise SqlValidationError(f"Функция `{name}` запрещена.")


def _existing_limit(statement: exp.Expression) -> int | None:
    limit = statement.args.get("limit")
    if limit is None:
        return None
    try:
        return int(limit.expression.name)
    except (AttributeError, TypeError, ValueError):
        # LIMIT с выражением или параметром — считаем, что лимита нет,
        # и перекрываем своим значением.
        return None


def _apply_limit(statement: exp.Expression, max_rows: int) -> exp.Expression:
    """Гарантирует, что запрос вернёт не больше max_rows строк.

    Раньше эту роль играло правило в системном промпте плюс постобработка
    регуляркой. Модель регулярно ставила LIMIT после точки с запятой, и
    регулярка чинила это уже после генерации. На уровне AST проблема
    исчезает: лимит навешивается детерминированно.
    """
    current = _existing_limit(statement)
    if current is not None and current <= max_rows:
        return statement
    try:
        return statement.limit(max_rows)
    except (AttributeError, TypeError):
        # Экзотический корень, у которого нет .limit() — заворачиваем в подзапрос.
        wrapped = sqlglot.parse_one(
            f"SELECT * FROM ({statement.sql(dialect=DIALECT)}) AS _guarded LIMIT {max_rows}",
            dialect=DIALECT,
        )
        return wrapped


def validate(sql: str, max_rows: int = 500, allowed_tables: frozenset[str] | None = None) -> str:
    """
    Проверяет сгенерированный SQL и возвращает нормализованный безопасный запрос.

    Бросает SqlValidationError с текстом, пригодным для показа пользователю
    и для передачи модели на исправление (self-correction).
    """
    if allowed_tables is None:
        from config import settings
        allowed_tables = settings.db.allowed_tables

    if not sql or not sql.strip():
        raise SqlValidationError("Модель вернула пустой запрос.")

    try:
        statements = [s for s in sqlglot.parse(sql, dialect=DIALECT) if s is not None]
    except sqlglot.ParseError as e:
        raise SqlValidationError(f"Синтаксическая ошибка SQL: {e}") from e

    if not statements:
        raise SqlValidationError("Модель вернула пустой запрос.")
    if len(statements) > 1:
        # Ровно этот случай пропускала старая проверка startswith("SELECT"):
        # `SELECT 1; DELETE FROM pss_departures;` начинается с SELECT.
        raise SqlValidationError(
            "Разрешён только один запрос — несколько операторов через `;` запрещены."
        )

    statement = statements[0]

    if not isinstance(statement, _ALLOWED_ROOTS):
        raise SqlValidationError(
            f"Разрешены только запросы на чтение (SELECT), получено: {_root_kind_name(statement)}."
        )

    for node in _iter_forbidden_nodes(statement):
        raise SqlValidationError(
            f"Операция `{_root_kind_name(node)}` запрещена — ассистент работает только на чтение."
        )

    _check_tables(statement, allowed_tables)
    _check_functions(statement)

    statement = _apply_limit(statement, max_rows)
    return statement.sql(dialect=DIALECT, pretty=False)
