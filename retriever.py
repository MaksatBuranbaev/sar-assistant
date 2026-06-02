"""
retriever.py — поиск похожих примеров «вопрос → SQL» для few-shot промпта.

Это retrieval-часть RAG: вместо того чтобы держать в системном промпте все
примеры сразу, по вопросу пользователя достаётся top-k наиболее похожих из
банка examples/few_shot.yaml, и только они уходят в модель. Промпт не растёт
с размером банка, а примеры всегда релевантны заданному вопросу.

Почему TF-IDF, а не эмбеддинги
------------------------------
Корпус — несколько десятков коротких однотипных вопросов на одну предметную
область. Лексического поиска здесь достаточно, а плотные эмбеддинги потянули
бы за собой модель на сотни мегабайт и сетевой вызов на каждый запрос.
Когда банк вырастет до сотен примеров и появятся перефразировки без общих
слов («сколько людей спасли» vs «количество эвакуированных»), лексический
поиск начнёт промахиваться — вот там нужны эмбеддинги и pgvector, благо
PostgreSQL в проекте уже есть.

Почему символьные n-граммы, а не слова
--------------------------------------
Русский язык флективный: «выезд», «выезда», «выездов», «выездам» — для
пословного TF-IDF это четыре разных токена, и совпадение теряется. Символьные
4-граммы делят между собой почти все подстроки, поэтому морфология перестаёт
мешать без подключения стеммера.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import yaml

EXAMPLES_PATH = Path(__file__).parent / "examples" / "few_shot.yaml"
NGRAM = 4


@dataclass(frozen=True)
class Example:
    question: str
    sql: str


def _normalize(text: str) -> str:
    """Приводит текст к виду, в котором считаются n-граммы."""
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Пробелы по краям, чтобы n-граммы начала и конца слов тоже учитывались.
    return f" {text} "


def _ngrams(text: str, n: int = NGRAM) -> Counter:
    normalized = _normalize(text)
    if len(normalized) < n:
        return Counter([normalized])
    return Counter(normalized[i:i + n] for i in range(len(normalized) - n + 1))


def _l2_normalize(vector: dict[str, float]) -> dict[str, float]:
    norm = math.sqrt(sum(v * v for v in vector.values()))
    if norm == 0:
        return vector
    return {k: v / norm for k, v in vector.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    # Оба вектора уже L2-нормированы, поэтому косинус — это просто скалярное
    # произведение. Итерируем по более короткому словарю.
    if len(a) > len(b):
        a, b = b, a
    return sum(weight * b.get(term, 0.0) for term, weight in a.items())


class FewShotRetriever:
    """TF-IDF индекс над банком примеров. Строится один раз при старте."""

    def __init__(self, examples: list[Example]):
        self.examples = examples
        self._doc_vectors: list[dict[str, float]] = []
        self._idf: dict[str, float] = {}
        self._build()

    def _build(self) -> None:
        if not self.examples:
            return

        doc_ngrams = [_ngrams(ex.question) for ex in self.examples]
        n_docs = len(doc_ngrams)

        document_frequency: Counter = Counter()
        for counts in doc_ngrams:
            document_frequency.update(counts.keys())

        # Сглаженный idf: +1 в числителе и знаменателе, чтобы не делить на ноль,
        # и +1 снаружи, чтобы вес никогда не обнулялся у термина из всех документов.
        self._idf = {
            term: math.log((n_docs + 1) / (df + 1)) + 1.0
            for term, df in document_frequency.items()
        }

        for counts in doc_ngrams:
            total = sum(counts.values()) or 1
            vector = {
                term: (count / total) * self._idf.get(term, 0.0)
                for term, count in counts.items()
            }
            self._doc_vectors.append(_l2_normalize(vector))

    def _vectorize_query(self, question: str) -> dict[str, float]:
        counts = _ngrams(question)
        total = sum(counts.values()) or 1
        vector = {
            term: (count / total) * self._idf[term]
            for term, count in counts.items()
            if term in self._idf  # термины, не встречавшиеся в банке, веса не имеют
        }
        return _l2_normalize(vector)

    def search(self, question: str, k: int = 4, min_score: float = 0.05) -> list[Example]:
        """Возвращает до k примеров, отсортированных по убыванию похожести."""
        if not self._doc_vectors:
            return []

        query_vector = self._vectorize_query(question)
        if not query_vector:
            return []

        scored = [
            (_cosine(query_vector, doc_vector), index)
            for index, doc_vector in enumerate(self._doc_vectors)
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)

        return [self.examples[index] for score, index in scored[:k] if score >= min_score]


def load_examples(path: Path = EXAMPLES_PATH) -> list[Example]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [
        Example(question=item["question"].strip(), sql=item["sql"].strip())
        for item in raw
        if item.get("question") and item.get("sql")
    ]


_retriever: FewShotRetriever | None = None


def get_retriever() -> FewShotRetriever:
    """Ленивый синглтон — индекс строится один раз на процесс."""
    global _retriever
    if _retriever is None:
        _retriever = FewShotRetriever(load_examples())
    return _retriever


def format_examples(examples: list[Example]) -> str:
    """Собирает найденные примеры в блок для системного промпта."""
    if not examples:
        return ""
    blocks = [
        f"Вопрос: {ex.question}\nSQL:\n```sql\n{ex.sql}\n```"
        for ex in examples
    ]
    return "\n\n".join(blocks)
