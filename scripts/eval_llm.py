"""Приёмочный прогон локальной модели.

Раздел «оговорки» в заключении заполняет локальная модель, и полнота этого
раздела зависит от того, какую модель поднял пользователь. Скрипт прогоняет
любую модель по одному набору задач и выдаёт балл, по которому видно, годится
она для работы или теряет условия договора.

Что измеряется на каждом договоре:

  json          доля окон, из которых удалось разобрать JSON;
  parties       найдены ли стороны, которые в договоре действительно есть;
  wallets       найден ли адрес кошелька, который в договоре действительно есть;
  clause_recall найдены ли оговорки, которые в договоре есть;
  clause_noise  не выдуманы ли оговорки, которых в договоре нет;
  verbatim      доля найденных оговорок, чья цитата пережила сверку с текстом;
  clean         доля ответов без ссылок на статьи и без советов обойти закон.

Запуск на живой модели:

    python -m scripts.eval_llm --model llama3.3:70b
    python -m scripts.eval_llm --model qwen3:32b --base-url http://10.0.0.5:11434

Записать ответы модели, чтобы потом прогонять без неё:

    python -m scripts.eval_llm --model qwen3:32b --record data/eval/llm_runs/qwen3-32b.json

Прогнать записанное (модель не нужна, работает в CI):

    python -m scripts.eval_llm --replay data/eval/llm_runs/qwen3-32b.json

Результат пишется в data/eval/llm_gate.json. Оттуда его берёт app/llm/tiers.py
и печатает в заключении измеренный балл вместо оценки по числу параметров.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import PROJECT_ROOT
from app.llm.clauses import ClauseAnalysis, analyze_clauses
from app.llm.client import LlmReply
from app.llm.tiers import GATE_PASS_SCORE
from app.parsing.document import Document, SourceFormat
from app.rules.contract import ContractView

FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
GATE_PATH = PROJECT_ROOT / "data" / "eval" / "llm_gate.json"

# Вес показателя в итоговом балле. Полнота и отсутствие выдумок весят больше
# формальных: битый JSON сервис переживает, а пропущенная оговорка — это то,
# ради чего модель вообще подключена.
WEIGHTS = {
    "json": 0.10,
    "parties": 0.15,
    "wallets": 0.10,
    "clause_recall": 0.30,
    "clause_noise": 0.15,
    "verbatim": 0.15,
    "clean": 0.05,
}


@dataclass(frozen=True)
class Task:
    task_id: str
    title: str
    text: str
    expect_present: dict[str, str] = field(default_factory=dict)
    expect_absent: tuple[str, ...] = ()
    expect_parties: tuple[str, ...] = ()
    expect_wallets: tuple[str, ...] = ()


def _read_fixture(name: str) -> str:
    return ContractView.from_file(FIXTURES / name).text


def _view(text: str) -> ContractView:
    lines = [line for line in text.split("\n") if line.strip()]
    return ContractView.from_document(Document(SourceFormat.DOCX, lines))


# Маркеры — дословные куски образцового договора. Если договор поменяли,
# а маркер не поправили, сборка задач падает: молча мерить нечего.
_COMPLIANT_PRESENT = {
    "AST-003": "делистинг",
    "AST-004": "иностранным цифровым инструментом",
    "RTE-004": "Отклонение курса",
    "TRV-003": "актуализируют реквизиты",
}
_COMPLIANT_ABSENT = ("FTC-004", "THR-003")

_ANNEX = (
    "{number}. ПРИЛОЖЕНИЕ К СПЕЦИФИКАЦИИ\n"
    "{number}.1. Позиция {number}: станок металлообрабатывающий, "
    "комплектность по технической документации поставщика, срок поставки "
    "в течение девяноста календарных дней с даты подписания.\n"
    "{number}.2. Упаковка и маркировка выполняются по стандарту поставщика; "
    "риск случайной гибели переходит в момент передачи первому перевозчику.\n"
    "{number}.3. Гарантийный срок составляет двенадцать месяцев с даты "
    "ввода оборудования в эксплуатацию.\n"
)

# Оговорка, спрятанная в конце длинного договора. Это и есть измерение того,
# о чём спорят: видит ли модель условие на сороковой странице приложений.
_DEEP_CLAUSE = (
    "99. ДОПОЛНИТЕЛЬНЫЕ УСЛОВИЯ\n"
    "99.1. Если Покупатель действует как агент, комиссионер или поверенный "
    "иного лица, он указывает договор, в интересах которого действует, "
    "и представляет Поставщику его реквизиты.\n"
)


def build_tasks() -> list[Task]:
    compliant = _read_fixture("contract_compliant.docx")
    violating = _read_fixture("contract_violating.docx")

    for code, marker in _COMPLIANT_PRESENT.items():
        if marker not in compliant:
            raise SystemExit(
                f"маркер оговорки {code} «{marker}» не найден в образцовом договоре: "
                "поправьте scripts/eval_llm.py или tests/fixtures"
            )

    annex = "\n".join(_ANNEX.format(number=number) for number in range(10, 90))
    long_text = compliant + "\n" + annex + "\n" + _DEEP_CLAUSE

    return [
        Task(
            task_id="compliant",
            title="образцовый договор, четыре страницы",
            text=compliant,
            expect_present=dict(_COMPLIANT_PRESENT),
            expect_absent=_COMPLIANT_ABSENT,
            expect_parties=("Уралимпорт", "Shenzhen Precision Machinery"),
        ),
        Task(
            task_id="violating",
            title="нарушающий договор, полстраницы",
            text=violating,
            expect_present={},
            expect_absent=("FTC-004", "AST-003", "RTE-004", "TRV-003"),
            expect_wallets=("TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE",),
        ),
        Task(
            task_id="long_annexes",
            title="договор с приложениями, оговорка в конце",
            text=long_text,
            expect_present={**_COMPLIANT_PRESENT, "FTC-004": "комиссионер или поверенный"},
            expect_absent=("THR-003",),
            expect_parties=("Уралимпорт", "Shenzhen Precision Machinery"),
        ),
    ]


@dataclass
class TaskScore:
    task_id: str
    title: str
    metrics: dict[str, float]
    found: list[str]
    missed: list[str]
    invented: list[str]
    windows: int
    windows_answered: int
    detail: str

    def to_dict(self) -> dict:
        return {
            "task": self.task_id,
            "title": self.title,
            "metrics": {name: round(value, 4) for name, value in self.metrics.items()},
            "found": self.found,
            "missed": self.missed,
            "invented": self.invented,
            "windows": self.windows,
            "windows_answered": self.windows_answered,
            "detail": self.detail,
        }


def _ratio(hit: int, total: int) -> float:
    return 1.0 if total == 0 else hit / total


def score_task(task: Task, result: ClauseAnalysis, raw_windows: list[str]) -> TaskScore:
    notes = {note.code: note for note in result.notes}

    present_codes = set(task.expect_present)
    found = sorted(code for code in present_codes if notes.get(code) and notes[code].present)
    missed = sorted(present_codes - set(found))
    invented = sorted(
        code for code in task.expect_absent if notes.get(code) and notes[code].present
    )

    verbatim_total = len(found)
    verbatim_hit = sum(1 for code in found if notes[code].quote)

    party_hit = sum(
        1
        for expected in task.expect_parties
        if any(expected.lower() in party.name.lower() for party in result.parties)
    )
    wallet_hit = sum(
        1
        for expected in task.expect_wallets
        if any(expected == wallet.value for wallet in result.wallets)
    )

    json_ok = _ratio(result.coverage.windows_answered, max(1, result.coverage.windows))

    # «Чисто» — модель не пыталась ссылаться на статьи и не советовала обход.
    # Такие ответы сервис вырезает, поэтому считаем по сырым окнам.
    dirty = sum(
        1
        for window in raw_windows
        if any(
            token in window.lower()
            for token in ("-фз", "статья", "ст.", "раздроб", "обойти", "чтобы банк")
        )
    )
    clean = 1.0 - _ratio(dirty, max(1, len(raw_windows)))

    metrics = {
        "json": json_ok,
        "parties": _ratio(party_hit, len(task.expect_parties)),
        "wallets": _ratio(wallet_hit, len(task.expect_wallets)),
        "clause_recall": _ratio(len(found), len(present_codes)),
        "clause_noise": 1.0 - _ratio(len(invented), max(1, len(task.expect_absent))),
        "verbatim": _ratio(verbatim_hit, verbatim_total),
        "clean": clean,
    }

    return TaskScore(
        task_id=task.task_id,
        title=task.title,
        metrics=metrics,
        found=found,
        missed=missed,
        invented=invented,
        windows=result.coverage.windows,
        windows_answered=result.coverage.windows_answered,
        detail=result.detail,
    )


def overall(scores: list[TaskScore]) -> dict[str, float]:
    if not scores:
        return {name: 0.0 for name in WEIGHTS} | {"score": 0.0}
    averaged = {
        name: sum(item.metrics[name] for item in scores) / len(scores) for name in WEIGHTS
    }
    averaged["score"] = sum(averaged[name] * weight for name, weight in WEIGHTS.items())
    return averaged


class Recorder:
    """Оборачивает обращение к модели, чтобы ответы можно было переиграть."""

    def __init__(self, inner: Callable[[str], LlmReply], model: str) -> None:
        self._inner = inner
        self._model = model
        self.answers: dict[str, str] = {}
        self.task_id = ""
        self.window = 0

    def __call__(self, prompt: str) -> LlmReply:
        reply = self._inner(prompt)
        self.answers[f"{self.task_id}#{self.window}"] = reply.text
        self.window += 1
        return reply

    def start(self, task_id: str) -> None:
        self.task_id = task_id
        self.window = 0


class Replay:
    """Отдаёт записанные ответы вместо обращения к модели."""

    def __init__(self, answers: dict[str, str], model: str) -> None:
        self._answers = answers
        self._model = model
        self.task_id = ""
        self.window = 0

    def __call__(self, _prompt: str) -> LlmReply:
        key = f"{self.task_id}#{self.window}"
        self.window += 1
        if key not in self._answers:
            return LlmReply(text="", model=self._model, error="в записи нет этого окна")
        return LlmReply(text=self._answers[key], model=self._model)

    def start(self, task_id: str) -> None:
        self.task_id = task_id
        self.window = 0


def run(tasks: list[Task], caller) -> tuple[list[TaskScore], dict[str, list[str]]]:
    scores: list[TaskScore] = []
    raw: dict[str, list[str]] = {}
    for task in tasks:
        caller.start(task.task_id)
        seen: list[str] = []

        def capture(prompt: str, _caller=caller, _seen=seen) -> LlmReply:
            reply = _caller(prompt)
            _seen.append(reply.text or "")
            return reply

        result = analyze_clauses(_view(task.text), complete_fn=capture)
        raw[task.task_id] = seen
        scores.append(score_task(task, result, seen))
    return scores, raw


def _print_table(scores: list[TaskScore], summary: dict[str, float], model: str) -> None:
    columns = list(WEIGHTS)
    header = f"{'задача':<26}" + "".join(f"{name:>15}" for name in columns)
    print(f"\nМодель: {model}")
    print(header)
    print("-" * len(header))
    for item in scores:
        row = f"{item.task_id:<26}" + "".join(
            f"{item.metrics[name]:>15.2f}" for name in columns
        )
        print(row)
        if item.missed:
            print(f"{'':<26}не найдено: {', '.join(item.missed)}")
        if item.invented:
            print(f"{'':<26}выдумано: {', '.join(item.invented)}")
        if item.windows_answered < item.windows:
            print(
                f"{'':<26}окон отвечено {item.windows_answered} из {item.windows}"
            )
    print("-" * len(header))
    print(f"{'среднее':<26}" + "".join(f"{summary[name]:>15.2f}" for name in columns))
    verdict = "проходит" if summary["score"] >= GATE_PASS_SCORE else "НЕ проходит"
    print(f"\nИтоговый балл: {summary['score']:.3f} при пороге {GATE_PASS_SCORE:.2f} — {verdict}")


def _write_gate(path: Path, model: str, summary: dict[str, float], scores: list[TaskScore]) -> None:
    payload = {"models": {}}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and isinstance(existing.get("models"), dict):
                payload = existing
        except ValueError:
            pass
    payload.setdefault("models", {})[model] = {
        "score": round(summary["score"], 4),
        "passed": summary["score"] >= GATE_PASS_SCORE,
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "metrics": {name: round(summary[name], 4) for name in WEIGHTS},
        "tasks": [item.to_dict() for item in scores],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Приёмочный прогон локальной модели на разборе оговорок"
    )
    parser.add_argument("--model", default="", help="имя модели в Ollama")
    parser.add_argument("--base-url", default="", help="адрес Ollama, если не из .env")
    parser.add_argument("--record", type=Path, help="куда записать ответы модели")
    parser.add_argument("--replay", type=Path, help="прогнать записанные ответы")
    parser.add_argument("--gate", type=Path, default=GATE_PATH, help="куда писать итог")
    parser.add_argument(
        "--no-gate", action="store_true", help="не записывать результат в data/eval"
    )
    args = parser.parse_args(argv)

    tasks = build_tasks()

    if args.replay:
        recorded = json.loads(args.replay.read_text(encoding="utf-8"))
        model = args.model or str(recorded.get("model") or "записанная модель")
        caller = Replay(recorded.get("answers") or {}, model)
    else:
        from app.core.catalog import load_catalog
        from app.core.config import get_settings
        from app.llm.client import complete, ollama_reachable

        settings = get_settings()
        catalog = load_catalog()
        if args.base_url:
            settings.ollama_base_url = args.base_url
            catalog.llm.base_url = args.base_url
        if args.model:
            settings.ollama_model = args.model
            catalog.llm.model = args.model
        model = catalog.llm.model
        if not ollama_reachable(catalog.llm.base_url):
            print(
                f"Ollama не отвечает по адресу {catalog.llm.base_url}. "
                "Поднимите её или прогоните запись: --replay",
                file=sys.stderr,
            )
            return 2
        caller = Recorder(complete, model)

    scores, _raw = run(tasks, caller)
    summary = overall(scores)
    _print_table(scores, summary, model)

    if args.record and isinstance(caller, Recorder):
        args.record.parent.mkdir(parents=True, exist_ok=True)
        args.record.write_text(
            json.dumps(
                {"model": model, "answers": caller.answers}, ensure_ascii=False, indent=2
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"запись ответов: {args.record}")

    if not args.no_gate:
        _write_gate(args.gate, model, summary, scores)
        print(f"итог: {args.gate}")

    return 0 if summary["score"] >= GATE_PASS_SCORE else 1


if __name__ == "__main__":
    raise SystemExit(main())
