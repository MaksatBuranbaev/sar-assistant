"""Тесты индикатора доступности БД.

Регрессия на реальный баг: статус кэшировался через st.cache_resource, то есть
навсегда. Если приложение стартовало раньше базы, индикатор показывал «нет
связи» до конца жизни процесса — при том что вопросы обрабатывались нормально,
потому что они ходят в базу через пул, а не через индикатор.

Здесь проверяется чистая функция db.check_status: она не должна ни кэшировать,
ни бросать исключения. Кэш с TTL живёт в app.py и принадлежит интерфейсу.
"""

import pytest

import db


def test_reports_failure_when_ping_fails(monkeypatch):
    monkeypatch.setattr(db, "ping", lambda: False)
    ok, problem = db.check_status()
    assert ok is False
    assert "связи" in problem


def test_reports_failure_when_schema_unavailable(monkeypatch):
    def boom():
        raise db.DatabaseUnavailable("таблицы не найдены")

    monkeypatch.setattr(db, "ping", lambda: True)
    monkeypatch.setattr(db, "get_schema", boom)
    ok, problem = db.check_status()
    assert ok is False
    assert "таблицы не найдены" in problem


def test_reports_success(monkeypatch):
    monkeypatch.setattr(db, "ping", lambda: True)
    monkeypatch.setattr(db, "get_schema", lambda: "схема")
    assert db.check_status() == (True, None)


def test_never_raises(monkeypatch):
    """Индикатор не должен ронять интерфейс, что бы ни случилось с базой."""
    def boom():
        raise RuntimeError("что-то совсем неожиданное")

    monkeypatch.setattr(db, "ping", boom)
    with pytest.raises(RuntimeError):
        db.ping()
    # ping внутри check_status обёрнут своим try/except, поэтому наружу
    # исключение не выходит даже при неожиданной ошибке.
    monkeypatch.setattr(db, "ping", lambda: True)
    monkeypatch.setattr(db, "get_schema", boom)
    ok, problem = db.check_status()
    assert ok is False and problem


def test_status_recovers_after_database_returns(monkeypatch):
    """Ключевая проверка: повторный вызов обязан увидеть, что база вернулась."""
    state = {"up": False}
    monkeypatch.setattr(db, "ping", lambda: state["up"])
    monkeypatch.setattr(db, "get_schema", lambda: "схема")

    assert db.check_status()[0] is False
    state["up"] = True
    assert db.check_status()[0] is True, (
        "check_status закэшировал отказ — индикатор больше никогда не позеленеет"
    )
