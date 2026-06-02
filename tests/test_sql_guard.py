"""Тесты валидатора SQL.

Каждый заблокированный кейс — это конкретный обход старой проверки
`sql.strip().upper().startswith("SELECT")`, которая стояла здесь раньше.
Тесты не требуют ни базы данных, ни доступа к GigaChat.
"""

import pytest

from sql_guard import SqlValidationError, validate

ALLOWED = frozenset({"pss_departures", "pss_lessons"})


@pytest.mark.parametrize(
    "sql, reason",
    [
        ("SELECT 1; DELETE FROM pss_departures;",
         "несколько операторов через ; — начинается с SELECT и проходил старую проверку"),
        ("SELECT 1;\nDROP TABLE pss_departures;",
         "то же самое с переносом строки"),
        ("DELETE FROM pss_departures",
         "прямое удаление"),
        ("UPDATE pss_departures SET victims = 0",
         "изменение данных"),
        ("INSERT INTO pss_departures (record_id) VALUES ('x')",
         "вставка"),
        ("DROP TABLE pss_departures",
         "удаление таблицы"),
        ("TRUNCATE pss_departures",
         "очистка таблицы"),
        ("CREATE TABLE evil (id int)",
         "создание объектов"),
        ("ALTER TABLE pss_departures ADD COLUMN x int",
         "изменение схемы"),
        ("SELECT pg_sleep(600)",
         "отказ в обслуживании — формально это SELECT"),
        ("SELECT pg_read_file('/etc/passwd')",
         "чтение файловой системы сервера"),
        ("SELECT * FROM pg_user",
         "чтение системного каталога — таблицы нет в allowlist"),
        ("SELECT * FROM pg_catalog.pg_roles",
         "явное обращение к системной схеме"),
        ("SELECT * FROM information_schema.tables",
         "утечка метаданных"),
        ("SELECT * FROM users",
         "таблица вне allowlist"),
        ("SELECT * FROM public.salaries",
         "таблица вне allowlist с явной схемой"),
        ("SELECT * FROM other_schema.pss_departures",
         "разрешённое имя таблицы, но чужая схема"),
        ("", "пустой запрос"),
        ("это не sql", "не разбирается как запрос"),
    ],
)
def test_blocked(sql, reason):
    with pytest.raises(SqlValidationError):
        validate(sql, max_rows=500, allowed_tables=ALLOWED)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM pss_departures",
        "SELECT district, COUNT(*) FROM pss_departures GROUP BY district",
        # CTE — легальный запрос на чтение, который старая проверка отвергала,
        # потому что он начинается не с SELECT, а с WITH.
        "WITH d AS (SELECT district FROM pss_departures) SELECT * FROM d",
        "/* комментарий */ SELECT 1 FROM pss_departures",
        "SELECT * FROM pss_departures UNION ALL SELECT * FROM pss_departures",
        "SELECT d.district FROM pss_departures d "
        "JOIN pss_lessons l ON l.district = d.district",
        "SELECT 'NOT_APPLICABLE' AS result",
        "SELECT AVG(duration_total_min) FROM pss_departures WHERE victims > 0",
        "SELECT district, ROW_NUMBER() OVER (ORDER BY date) FROM pss_departures",
    ],
)
def test_allowed(sql):
    assert validate(sql, max_rows=500, allowed_tables=ALLOWED)


def test_limit_is_added_when_missing():
    result = validate("SELECT * FROM pss_departures", max_rows=100, allowed_tables=ALLOWED)
    assert "LIMIT 100" in result.upper()


def test_limit_is_lowered_when_too_large():
    result = validate(
        "SELECT * FROM pss_departures LIMIT 999999",
        max_rows=100,
        allowed_tables=ALLOWED,
    )
    assert "LIMIT 100" in result.upper()
    assert "999999" not in result


def test_smaller_limit_is_preserved():
    result = validate(
        "SELECT * FROM pss_departures LIMIT 10",
        max_rows=500,
        allowed_tables=ALLOWED,
    )
    assert "LIMIT 10" in result.upper()


def test_cte_alias_is_not_treated_as_table():
    # `d` — не таблица БД, а имя CTE: allowlist к нему применяться не должен.
    result = validate(
        "WITH d AS (SELECT district FROM pss_departures) SELECT * FROM d",
        max_rows=500,
        allowed_tables=ALLOWED,
    )
    assert "pss_departures" in result


def test_error_message_is_user_readable():
    with pytest.raises(SqlValidationError) as exc:
        validate("SELECT * FROM salaries", max_rows=500, allowed_tables=ALLOWED)
    # Текст ошибки уходит и пользователю, и модели на исправление,
    # поэтому он должен называть проблему, а не быть трейсбэком.
    assert "salaries" in str(exc.value)
