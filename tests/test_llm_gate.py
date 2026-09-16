"""Проверка самого приёмочного прогона.

Здесь не измеряются настоящие модели: без видеокарты их негде поднять, а
выдавать синтетику за измерение нельзя. Проверяется инструмент — что
`scripts/eval_llm.py` отличает полный разбор от неполного и от сломанного,
и что отчёт подхватывает записанный балл вместо оценки по имени модели.

Синтетические «ответчики» ниже имитируют три поведения: модель, которая
выписывает всё дословно; модель, которая пересказывает и теряет оговорки в
конце длинного договора; модель, которая ломает JSON.
"""

from __future__ import annotations

import json
import re

import pytest

from app.llm.client import LlmReply
from app.llm.tiers import GATE_PASS_SCORE, Tier, classify
from scripts.eval_llm import Replay, build_tasks, overall, run
from tests.synthetic_models import BrokenModel, PerfectModel, SloppyModel

pytestmark = pytest.mark.no_pipeline_stub

def _score(caller) -> dict[str, float]:
    scores, _raw = run(build_tasks(), caller)
    return overall(scores)


class TestGateDiscriminates:
    def test_full_extraction_passes(self):
        summary = _score(PerfectModel())

        assert summary["clause_recall"] == pytest.approx(1.0)
        assert summary["verbatim"] == pytest.approx(1.0)
        assert summary["score"] >= GATE_PASS_SCORE

    def test_paraphrasing_and_losing_the_tail_fails(self):
        summary = _score(SloppyModel())

        assert summary["score"] < GATE_PASS_SCORE
        assert summary["verbatim"] < 1.0
        assert summary["clause_recall"] < 1.0
        assert summary["clause_noise"] < 1.0  # выдумала THR-003

    def test_broken_json_fails(self):
        summary = _score(BrokenModel())

        assert summary["json"] == pytest.approx(0.0)
        assert summary["score"] < GATE_PASS_SCORE

    def test_deep_clause_is_what_separates_them(self):
        """Оговорка в конце длинного договора — та самая спорная точка."""
        good, _ = run(build_tasks(), PerfectModel())
        bad, _ = run(build_tasks(), SloppyModel())

        long_good = next(item for item in good if item.task_id == "long_annexes")
        long_bad = next(item for item in bad if item.task_id == "long_annexes")

        assert "FTC-004" in long_good.found
        assert "FTC-004" in long_bad.missed


class TestReplay:
    def test_recorded_answers_reproduce_the_score(self):
        caller = PerfectModel()
        tasks = build_tasks()
        answers: dict[str, str] = {}
        for task in tasks:
            caller.start(task.task_id)
            window = 0

            def capture(prompt: str, _task=task, _window=[0]) -> LlmReply:
                reply = caller(prompt)
                answers[f"{_task.task_id}#{_window[0]}"] = reply.text
                _window[0] += 1
                return reply

            from app.llm.clauses import analyze_clauses
            from app.parsing.document import Document, SourceFormat
            from app.rules.contract import ContractView

            lines = [line for line in task.text.split("\n") if line.strip()]
            analyze_clauses(
                ContractView.from_document(Document(SourceFormat.DOCX, lines)),
                complete_fn=capture,
            )
            del window

        replayed = _score(Replay(answers, "recorded"))
        assert replayed["score"] >= GATE_PASS_SCORE

    def test_missing_window_in_the_recording_is_not_silent(self):
        summary = _score(Replay({}, "recorded"))

        assert summary["json"] == pytest.approx(0.0)


class TestMeasuredScoreOverridesTheNameHeuristic:
    def test_small_model_that_passed_the_gate_is_not_called_unsupported(self, tmp_path):
        gate = tmp_path / "llm_gate.json"
        gate.write_text(
            json.dumps(
                {
                    "models": {
                        "qwen3:14b": {"score": 0.94, "ran_at": "2026-09-15T00:00:00+00:00"}
                    }
                }
            ),
            encoding="utf-8",
        )

        tier = classify("qwen3:14b", gate_path=gate)

        assert tier.measured
        assert tier.tier is Tier.SUPPORTED
        assert "Приёмочный прогон" in tier.note

    def test_big_model_that_failed_the_gate_is_downgraded(self, tmp_path):
        gate = tmp_path / "llm_gate.json"
        gate.write_text(
            json.dumps({"models": {"llama3.3:70b": {"score": 0.71}}}), encoding="utf-8"
        )

        tier = classify("llama3.3:70b", gate_path=gate)

        assert tier.tier is Tier.LIMITED

    def test_without_a_run_the_name_is_only_an_estimate(self, tmp_path):
        tier = classify("qwen3:14b", gate_path=tmp_path / "absent.json")

        assert not tier.measured
        assert tier.tier is Tier.LIMITED


class TestNameParsing:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("llama3.3:70b", 70.0),
            ("qwen3:32b-instruct-q4_K_M", 32.0),
            ("gemma3:27b", 27.0),
            ("mixtral:8x7b", 56.0),
            ("llama3.2:3b", 3.0),
            ("phi4:14b-q8_0", 14.0),
            ("my-private-model:latest", None),
        ],
    )
    def test_parameters_are_read_from_the_name(self, name, expected):
        from app.llm.tiers import parameters_from_name

        assert parameters_from_name(name) == expected

    def test_coarse_quantisation_lowers_the_class(self):
        assert classify("llama3.3:70b").tier is Tier.REFERENCE
        assert classify("llama3.3:70b-instruct-q2_K").tier is Tier.SUPPORTED

    def test_unknown_name_is_not_silently_trusted(self):
        tier = classify("my-private-model:latest")

        assert tier.tier is Tier.UNKNOWN
        assert re.search(r"не определя", tier.note)
