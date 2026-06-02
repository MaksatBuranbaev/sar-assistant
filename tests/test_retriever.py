"""Тесты поиска похожих примеров и целостности банка few-shot."""

import pytest

from retriever import Example, FewShotRetriever, format_examples, load_examples
from sql_guard import SqlValidationError, validate


@pytest.fixture(scope="module")
def bank():
    return load_examples()


def test_bank_is_not_empty(bank):
    assert len(bank) >= 10


def test_every_example_passes_the_guard(bank):
    """Банк примеров — это то, на что модель ориентируется. Если пример
    не проходит собственный валидатор, модель учится генерировать мусор."""
    for example in bank:
        try:
            validate(example.sql)
        except SqlValidationError as e:
            pytest.fail(f"пример «{example.question}» не проходит валидатор: {e}")


def test_no_duplicate_questions(bank):
    questions = [ex.question.lower().strip() for ex in bank]
    assert len(questions) == len(set(questions))


def test_bank_does_not_leak_into_eval_dataset(bank):
    """Пересечение банка и тестового набора завышает execution accuracy:
    модель получила бы эталонный ответ прямо в промпте."""
    yaml = pytest.importorskip("yaml")
    from pathlib import Path

    dataset_path = Path(__file__).parent.parent / "eval" / "dataset.yaml"
    if not dataset_path.exists():
        pytest.skip("eval/dataset.yaml отсутствует")

    eval_questions = {
        item["question"].lower().strip()
        for item in (yaml.safe_load(dataset_path.read_text(encoding="utf-8")) or [])
    }
    bank_questions = {ex.question.lower().strip() for ex in bank}

    overlap = bank_questions & eval_questions
    assert not overlap, f"пересечение банка и eval-набора: {overlap}"


def test_search_finds_relevant_example():
    retriever = FewShotRetriever([
        Example("Сколько выездов на ДТП по районам?", "SELECT 1"),
        Example("Какие нормативы отрабатывали на занятиях?", "SELECT 2"),
        Example("Сколько всего участников занятий?", "SELECT 3"),
    ])
    hits = retriever.search("количество выездов на дтп в разрезе районов", k=1)
    assert hits
    assert "ДТП" in hits[0].question


def test_search_is_robust_to_russian_morphology():
    """Символьные n-граммы должны сшивать «выездов» и «выезд» —
    ради этого они и выбраны вместо пословного TF-IDF."""
    retriever = FewShotRetriever([
        Example("Сколько было выездов?", "SELECT 1"),
        Example("Какие нормативы отрабатывали?", "SELECT 2"),
    ])
    hits = retriever.search("выезд", k=1)
    assert hits and "выездов" in hits[0].question


def test_search_returns_at_most_k():
    retriever = FewShotRetriever([Example(f"вопрос про выезды {i}", "SELECT 1") for i in range(10)])
    assert len(retriever.search("выезды", k=3)) <= 3


def test_empty_bank_does_not_crash():
    assert FewShotRetriever([]).search("что угодно", k=3) == []


def test_format_examples_produces_sql_blocks():
    block = format_examples([Example("Вопрос", "SELECT 1")])
    assert "```sql" in block and "SELECT 1" in block


def test_format_examples_empty():
    assert format_examples([]) == ""
