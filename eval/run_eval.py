"""
run_eval.py — замер качества Text-to-SQL на размеченном наборе.

Запуск (нужны и БД, и доступ к GigaChat):
    python -m eval.run_eval
    python -m eval.run_eval --limit 10 --output eval/report.json

Что считается
-------------
execution accuracy — доля вопросов, где результат сгенерированного запроса
совпал с результатом эталонного. Сравниваются именно результаты, а не тексты:
один и тот же ответ даёт множество разных корректных запросов, поэтому
сравнение строк SQL меряло бы совпадение стиля, а не правильность.

Дополнительно:
  invalid_sql_rate — доля запросов, отклонённых валидатором или не разобранных;
  db_error_rate    — доля запросов, упавших уже в PostgreSQL;
  self_correction  — сколько ответов потребовали повторной генерации;
  latency p50/p95  — время полного цикла.

Зачем это нужно
---------------
Промпт — такой же изменяемый артефакт, как код, но без замера правка промпта
проверяется «на глаз»: чинишь один вопрос, молча ломаешь три других. Этот
скрипт — регрессионный тест для промпта, схемы и банка примеров.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
from assistant import generate_sql  # noqa: E402
from sql_guard import SqlValidationError, validate  # noqa: E402

DATASET_PATH = Path(__file__).parent / "dataset.yaml"
DEFAULT_REPORT = Path(__file__).parent / "report.json"

logger = logging.getLogger("eval")


@dataclass
class CaseResult:
    question: str
    difficulty: str
    gold_sql: str
    predicted_sql: str | None = None
    correct: bool = False
    failure: str | None = None  # invalid_sql | db_error | mismatch | llm_error
    detail: str | None = None
    gold_rows: int = 0
    predicted_rows: int = 0
    elapsed_ms: int = 0


@dataclass
class Report:
    total: int = 0
    correct: int = 0
    execution_accuracy: float = 0.0
    invalid_sql_rate: float = 0.0
    db_error_rate: float = 0.0
    mismatch_rate: float = 0.0
    latency_p50_ms: int = 0
    latency_p95_ms: int = 0
    by_difficulty: dict = field(default_factory=dict)
    cases: list = field(default_factory=list)
    generated_at: str = ""


def _normalize_value(value) -> str:
    """Приводит значение к строке, устойчивой к несущественным различиям типов.

    numeric из PostgreSQL приходит как Decimal, а COUNT(*) как int; AVG может
    отличаться в последнем знаке из-за порядка суммирования. Округление до
    двух знаков убирает шум, не пряча реальные расхождения.
    """
    if value is None:
        return "∅"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float, Decimal)):
        return f"{float(value):.2f}"
    if isinstance(value, (datetime, date, dtime, timedelta)):
        return str(value)
    return str(value).strip()


def _normalize_rows(rows: list[dict]) -> Counter:
    """Мультимножество строк без привязки к именам и порядку колонок.

    Компромисс осознанный: `SELECT district, COUNT(*)` и `SELECT COUNT(*), district`
    считаются одинаковыми, потому что модель вправе выбрать любой порядок и
    любые псевдонимы. Плата — теоретическая возможность засчитать запрос, где
    значения совпали случайно; на практике это редкость, а завышение метрики
    из-за требования дословного совпадения колонок было бы куда хуже.
    """
    return Counter(
        tuple(sorted(_normalize_value(v) for v in row.values()))
        for row in rows
    )


def _rows_equal(gold: list[dict], predicted: list[dict], ordered: bool) -> bool:
    if ordered:
        if len(gold) != len(predicted):
            return False
        return all(
            sorted(_normalize_value(v) for v in g.values())
            == sorted(_normalize_value(v) for v in p.values())
            for g, p in zip(gold, predicted, strict=True)
        )
    return _normalize_rows(gold) == _normalize_rows(predicted)


def _is_ordered(sql: str) -> bool:
    """Порядок строк важен, только если он явно задан в эталоне."""
    return "order by" in sql.lower()


def _percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(int(round(p * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def run_case(item: dict, schema: str) -> CaseResult:
    question = item["question"].strip()
    gold_sql = item["sql"].strip()
    case = CaseResult(
        question=question,
        difficulty=item.get("difficulty", "unknown"),
        gold_sql=gold_sql,
    )

    started = time.monotonic()
    try:
        predicted_sql, _ = generate_sql(question, schema=schema)
        case.predicted_sql = predicted_sql
    except Exception as e:  # noqa: BLE001
        case.failure, case.detail = "llm_error", str(e)
        case.elapsed_ms = int((time.monotonic() - started) * 1000)
        return case

    try:
        validate(predicted_sql)
    except SqlValidationError as e:
        case.failure, case.detail = "invalid_sql", str(e)
        case.elapsed_ms = int((time.monotonic() - started) * 1000)
        return case

    try:
        _, predicted_rows = db.execute_generated_sql(predicted_sql)
    except Exception as e:  # noqa: BLE001
        case.failure, case.detail = "db_error", str(e)
        case.elapsed_ms = int((time.monotonic() - started) * 1000)
        return case

    _, gold_rows = db.execute_generated_sql(gold_sql)
    case.elapsed_ms = int((time.monotonic() - started) * 1000)
    case.gold_rows = len(gold_rows)
    case.predicted_rows = len(predicted_rows)

    if _rows_equal(gold_rows, predicted_rows, ordered=_is_ordered(gold_sql)):
        case.correct = True
    else:
        case.failure = "mismatch"
        case.detail = f"эталон: {len(gold_rows)} строк, получено: {len(predicted_rows)}"

    return case


def build_report(cases: list[CaseResult]) -> Report:
    total = len(cases)
    report = Report(total=total, generated_at=datetime.now().isoformat(timespec="seconds"))
    if total == 0:
        return report

    report.correct = sum(1 for c in cases if c.correct)
    report.execution_accuracy = round(report.correct / total, 3)
    report.invalid_sql_rate = round(sum(1 for c in cases if c.failure == "invalid_sql") / total, 3)
    report.db_error_rate = round(sum(1 for c in cases if c.failure == "db_error") / total, 3)
    report.mismatch_rate = round(sum(1 for c in cases if c.failure == "mismatch") / total, 3)

    latencies = [c.elapsed_ms for c in cases]
    report.latency_p50_ms = int(statistics.median(latencies))
    report.latency_p95_ms = _percentile(latencies, 0.95)

    for difficulty in sorted({c.difficulty for c in cases}):
        subset = [c for c in cases if c.difficulty == difficulty]
        report.by_difficulty[difficulty] = {
            "total": len(subset),
            "correct": sum(1 for c in subset if c.correct),
            "accuracy": round(sum(1 for c in subset if c.correct) / len(subset), 3),
        }

    report.cases = [asdict(c) for c in cases]
    return report


def print_report(report: Report) -> None:
    print()
    print("=" * 72)
    print(f"  Execution accuracy : {report.execution_accuracy:.1%}  "
          f"({report.correct}/{report.total})")
    print(f"  Невалидный SQL     : {report.invalid_sql_rate:.1%}")
    print(f"  Ошибки БД          : {report.db_error_rate:.1%}")
    print(f"  Неверный результат : {report.mismatch_rate:.1%}")
    print(f"  Latency p50 / p95  : {report.latency_p50_ms} мс / {report.latency_p95_ms} мс")
    print("=" * 72)

    print("\n  По сложности:")
    for difficulty, stats in report.by_difficulty.items():
        print(f"    {difficulty:<8} {stats['accuracy']:.1%}  "
              f"({stats['correct']}/{stats['total']})")

    failures = [c for c in report.cases if not c["correct"]]
    if failures:
        print(f"\n  Не прошли ({len(failures)}):")
        for case in failures:
            print(f"    [{case['failure']}] {case['question']}")
            if case["detail"]:
                print(f"        {case['detail']}")
            if case["predicted_sql"]:
                compact = " ".join(case["predicted_sql"].split())
                print(f"        SQL: {compact[:150]}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Замер качества Text-to-SQL")
    parser.add_argument("--limit", type=int, help="прогнать только первые N примеров")
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    items = yaml.safe_load(args.dataset.read_text(encoding="utf-8")) or []
    if args.limit:
        items = items[:args.limit]

    # check_status, а не ping: она различает «нет связи» и «связь есть, но нет
    # нужных таблиц» — на старте прогона это экономит время на диагностику.
    db_ok, problem = db.check_status()
    if not db_ok:
        print(f"База данных недоступна: {problem}", file=sys.stderr)
        print("Проверьте настройки подключения в .env", file=sys.stderr)
        return 1

    schema = db.get_schema()
    print(f"Прогон {len(items)} примеров...\n")

    cases: list[CaseResult] = []
    for index, item in enumerate(items, start=1):
        case = run_case(item, schema)
        cases.append(case)
        mark = "✓" if case.correct else "✗"
        print(f"  {mark} [{index:>2}/{len(items)}] {case.question}")

    report = build_report(cases)
    print_report(report)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Отчёт сохранён: {args.output}")

    db.close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
