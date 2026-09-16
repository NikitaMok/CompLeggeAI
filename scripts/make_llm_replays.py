"""Собирает синтетические записи ответов для приёмочного прогона.

Запускается вручную при изменении задач или образцовых договоров; результат
коммитится, поэтому в CI модель не нужна:

    python -m scripts.make_llm_replays

Записи помечены полем `synthetic`. Это проверка самого прогона, а не измерение
настоящей модели: `app/llm/tiers.py` такие записи в отчёт не берёт.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.config import PROJECT_ROOT
from app.llm.clauses import analyze_clauses
from app.parsing.document import Document, SourceFormat
from app.rules.contract import ContractView
from scripts.eval_llm import build_tasks
from tests.synthetic_models import BrokenModel, PerfectModel, SloppyModel

OUT = PROJECT_ROOT / "data" / "eval" / "llm_runs"

NOTE = (
    "Синтетическая запись для самопроверки приёмочного прогона. "
    "Это не измерение настоящей модели."
)


def _view(text: str) -> ContractView:
    lines = [line for line in text.split("\n") if line.strip()]
    return ContractView.from_document(Document(SourceFormat.DOCX, lines))


def record(model, label: str) -> Path:
    answers: dict[str, str] = {}
    for task in build_tasks():
        if hasattr(model, "start"):
            model.start(task.task_id)
        counter = [0]

        def capture(prompt: str, _task=task, _counter=counter):
            reply = model(prompt)
            answers[f"{_task.task_id}#{_counter[0]}"] = reply.text
            _counter[0] += 1
            return reply

        analyze_clauses(_view(task.text), complete_fn=capture)

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{label}.json"
    path.write_text(
        json.dumps(
            {"model": label, "synthetic": True, "note": NOTE, "answers": answers},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def main() -> int:
    for model, label in (
        (PerfectModel(), "synthetic_full_extraction"),
        (SloppyModel(), "synthetic_lossy_extraction"),
        (BrokenModel(), "synthetic_broken_json"),
    ):
        path = record(model, label)
        print(f"записано: {path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
