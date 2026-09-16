"""Класс локальной модели и его отражение в заключении.

Опорная конфигурация продукта — модель класса 70B. Разбор оговорок на договорах
крипто-ВЭД требует удержания длинного контекста и дословного цитирования;
модели меньшего класса теряют условия в приложениях. Класс модели пишется
в заключении, чтобы юрист видел, на чём построен раздел оговорок.

Вердикт по договору при этом ставит матрица правил (`app/rules/engine.py`)
и от модели не зависит. Ответ модели проходит сверку с текстом договора
в `app/llm/clauses.py`, непрошедшее выбрасывается.

Класс определяется по имени модели в Ollama — это оценка, а не измерение.
Если рядом лежит результат приёмочного прогона (`data/eval/llm_gate.json`,
пишет `scripts/eval_llm.py`), в заключение идёт измеренный балл.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from app.core.config import PROJECT_ROOT

GATE_PATH = PROJECT_ROOT / "data" / "eval" / "llm_gate.json"

# Опорная модель, на которой описан режим по умолчанию.
REFERENCE_MODEL = "llama3.3:70b"

# Границы классов в миллиардах параметров. Это не измерение качества, а рамка
# по умолчанию до приёмочного прогона: проверяется scripts/eval_llm.py.
REFERENCE_FLOOR = 65.0
SUPPORTED_FLOOR = 24.0
LIMITED_FLOOR = 12.0

# Балл приёмочного прогона, начиная с которого модель считается пригодной.
GATE_PASS_SCORE = 0.90

_SIZE = re.compile(r"(?<![a-z0-9.])(\d+(?:[.,]\d+)?)\s*b(?![a-z0-9])", re.IGNORECASE)
_MOE = re.compile(r"(?<![a-z0-9.])(\d+)\s*x\s*(\d+(?:[.,]\d+)?)\s*b(?![a-z0-9])", re.IGNORECASE)
_QUANT = re.compile(r"(q\d[_a-z0-9]*|f?p?16|bf16|f32)", re.IGNORECASE)
_COARSE_QUANT = re.compile(r"^q[123](?![0-9])", re.IGNORECASE)


class Tier(str, Enum):
    REFERENCE = "reference"
    SUPPORTED = "supported"
    LIMITED = "limited"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


_LABEL = {
    Tier.REFERENCE: "опорный класс",
    Tier.SUPPORTED: "поддерживаемый класс",
    Tier.LIMITED: "ограниченный класс",
    Tier.UNSUPPORTED: "класс ниже поддерживаемого",
    Tier.UNKNOWN: "класс не определён по имени модели",
}

_NOTE = {
    Tier.REFERENCE: (
        "Разбор оговорок выполнен на модели опорного класса. "
        "Вердикт по договору в любом случае ставит матрица правил."
    ),
    Tier.SUPPORTED: (
        "Разбор оговорок выполнен на модели поддерживаемого класса. "
        "Вердикт по договору ставит матрица правил; раздел оговорок "
        "перечитать юристу."
    ),
    Tier.LIMITED: (
        "Модель этого класса на длинном договоре пропускает часть оговорок. "
        "Незаполненный раздел оговорок не означает, что оговорки в договоре нет. "
        "Вердикт по договору ставит матрица правил и от модели не зависит."
    ),
    Tier.UNSUPPORTED: (
        "Модель ниже поддерживаемого класса. Раздел оговорок носит справочный "
        "характер: пропуски вероятны, отсутствие записи ничего не доказывает. "
        "Вердикт по договору ставит матрица правил и от модели не зависит."
    ),
    Tier.UNKNOWN: (
        "Класс модели по её имени не определяется. Полноту раздела оговорок "
        "следует считать непроверенной до приёмочного прогона "
        "(python -m scripts.eval_llm). Вердикт по договору ставит матрица правил."
    ),
}


@dataclass(frozen=True)
class ModelTier:
    model: str
    tier: Tier
    parameters_b: float | None
    quantization: str | None
    label: str
    note: str
    measured_score: float | None = None
    measured_on: str | None = None

    @property
    def below_supported(self) -> bool:
        return self.tier in (Tier.LIMITED, Tier.UNSUPPORTED)

    @property
    def measured(self) -> bool:
        return self.measured_score is not None

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "tier": self.tier.value,
            "parameters_b": self.parameters_b,
            "quantization": self.quantization,
            "label": self.label,
            "note": self.note,
            "measured_score": self.measured_score,
            "measured_on": self.measured_on,
        }


def parameters_from_name(model: str) -> float | None:
    """Число параметров в миллиардах по имени модели в Ollama.

    Поддерживает «70b», «32b-instruct-q4_K_M» и запись MoE «8x7b»
    (для смеси экспертов берётся общее число параметров, а не активных:
    в память загружаются все).
    """
    if not model:
        return None
    moe = _MOE.search(model)
    if moe:
        experts = int(moe.group(1))
        each = float(moe.group(2).replace(",", "."))
        return round(experts * each, 1)
    match = _SIZE.search(model)
    if not match:
        return None
    return float(match.group(1).replace(",", "."))


def quantization_from_name(model: str) -> str | None:
    if not model:
        return None
    tail = model.split(":", 1)[1] if ":" in model else model
    for part in re.split(r"[-_]", tail):
        if _QUANT.fullmatch(part):
            return part
    match = _QUANT.search(tail)
    return match.group(1) if match else None


def _tier_from_size(parameters_b: float | None) -> Tier:
    if parameters_b is None:
        return Tier.UNKNOWN
    if parameters_b >= REFERENCE_FLOOR:
        return Tier.REFERENCE
    if parameters_b >= SUPPORTED_FLOOR:
        return Tier.SUPPORTED
    if parameters_b >= LIMITED_FLOOR:
        return Tier.LIMITED
    return Tier.UNSUPPORTED


def _load_gate(path: Path | None = None) -> dict:
    source = path or GATE_PATH
    try:
        with source.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def classify(model: str, *, gate_path: Path | None = None) -> ModelTier:
    """Класс модели: измеренный приёмочным прогоном либо оценённый по имени."""
    name = (model or "").strip()
    parameters_b = parameters_from_name(name)
    quantization = quantization_from_name(name)
    tier = _tier_from_size(parameters_b)

    # Грубое квантование съедает качество сильнее, чем пара миллиардов
    # параметров, поэтому опускаем модель на класс ниже.
    if quantization and _COARSE_QUANT.match(quantization) and tier is Tier.REFERENCE:
        tier = Tier.SUPPORTED
    elif quantization and _COARSE_QUANT.match(quantization) and tier is Tier.SUPPORTED:
        tier = Tier.LIMITED

    measured_score: float | None = None
    measured_on: str | None = None
    gate = _load_gate(gate_path)
    entry = (gate.get("models") or {}).get(name) if isinstance(gate.get("models"), dict) else None
    if isinstance(entry, dict):
        raw_score = entry.get("score")
        if isinstance(raw_score, (int, float)):
            measured_score = float(raw_score)
            measured_on = str(entry.get("ran_at") or "") or None
            tier = Tier.SUPPORTED if measured_score >= GATE_PASS_SCORE else Tier.LIMITED
            if measured_score >= GATE_PASS_SCORE and (parameters_b or 0) >= REFERENCE_FLOOR:
                tier = Tier.REFERENCE

    note = _NOTE[tier]
    if measured_score is not None:
        note = (
            f"Приёмочный прогон: {measured_score:.2f} "
            f"при пороге {GATE_PASS_SCORE:.2f}. " + note
        )

    return ModelTier(
        model=name,
        tier=tier,
        parameters_b=parameters_b,
        quantization=quantization,
        label=_LABEL[tier],
        note=note,
        measured_score=measured_score,
        measured_on=measured_on,
    )
