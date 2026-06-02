"""Тесты разбора ответа модели и подготовки данных.

Сеть и БД не используются — проверяется только чистая логика,
поэтому тесты гоняются в CI без секретов и без PostgreSQL.
"""

import pytest

from assistant import Turn, _build_sql_messages, _is_not_applicable, extract_sql, rows_to_text
from retriever import Example
from sql_guard import SqlValidationError


@pytest.mark.parametrize(
    "raw, expected_start",
    [
        ("```sql\nSELECT 1\n```", "SELECT 1"),
        ("```SQL\nSELECT 1\n```", "SELECT 1"),
        ("```\nSELECT 1\n```", "SELECT 1"),
        ("Вот запрос:\n```sql\nSELECT 1\n```\nГотово", "SELECT 1"),
        ("SELECT 1", "SELECT 1"),
        ("  select 1  ", "select 1"),
        ("WITH d AS (SELECT 1) SELECT * FROM d", "WITH"),
    ],
)
def test_extract_sql(raw, expected_start):
    assert extract_sql(raw).startswith(expected_start)


@pytest.mark.parametrize("raw", ["", "Я не могу ответить на этот вопрос", "```python\nprint(1)\n```"])
def test_extract_sql_rejects_non_sql(raw):
    with pytest.raises(SqlValidationError):
        extract_sql(raw)


def test_rows_to_text_empty():
    assert "пустой" in rows_to_text(["a"], [], limit=10)


def test_rows_to_text_truncates_and_reports():
    rows = [{"district": f"Район {i}", "cnt": i} for i in range(30)]
    text = rows_to_text(["district", "cnt"], rows, limit=10)
    assert "Район 9" in text
    assert "Район 15" not in text
    # Модель должна знать, что видит не весь результат, иначе она
    # сформулирует вывод так, будто это все данные.
    assert "из 30" in text


def test_rows_to_text_handles_none():
    text = rows_to_text(["a", "b"], [{"a": None, "b": 1}], limit=10)
    assert "None" not in text


def test_not_applicable_detected():
    assert _is_not_applicable(["result"], [{"result": "NOT_APPLICABLE"}])
    assert not _is_not_applicable(["result"], [{"result": "Спасено 3 человека"}])
    assert not _is_not_applicable([], [])


def test_history_is_passed_to_the_model():
    """Без истории уточняющий вопрос «а по районам?» не с чем соотнести."""
    messages = _build_sql_messages(
        question="а по районам?",
        schema="Таблица pss_departures",
        examples=[],
        history=[Turn(question="Сколько выездов на ДТП?", sql="SELECT COUNT(*) FROM pss_departures")],
    )
    joined = "\n".join(m.content for m in messages)
    assert "Сколько выездов на ДТП?" in joined
    assert "SELECT COUNT(*) FROM pss_departures" in joined
    assert messages[-1].content == "а по районам?"


def test_examples_are_injected_into_system_prompt():
    messages = _build_sql_messages(
        question="вопрос",
        schema="Таблица pss_departures",
        examples=[Example("Сколько выездов?", "SELECT COUNT(*) FROM pss_departures")],
        history=[],
    )
    assert "Сколько выездов?" in messages[0].content


def test_schema_is_injected_into_system_prompt():
    messages = _build_sql_messages("вопрос", "УНИКАЛЬНАЯ_СХЕМА_123", [], [])
    assert "УНИКАЛЬНАЯ_СХЕМА_123" in messages[0].content
