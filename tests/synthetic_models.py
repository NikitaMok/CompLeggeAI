"""Синтетические «ответчики» вместо настоящей модели.

Нужны, чтобы проверять приёмочный прогон (`scripts/eval_llm.py`) там, где
поднять модель негде: в CI и на машине без видеокарты. Это проверка
инструмента, а не измерение чьего-либо качества — записи, собранные отсюда,
помечены полем `synthetic` и в отчёт о моделях не попадают.

Три поведения: модель выписывает всё дословно; модель пересказывает и теряет
оговорки в конце длинного договора; модель ломает JSON.
"""

from __future__ import annotations

import json

from app.llm.client import LlmReply

PARTIES = [
    {"name": "ООО «Уралимпорт»", "inn": "6659123456", "role": "покупатель"},
    {
        "name": "Shenzhen Precision Machinery Co., Ltd",
        "inn": None,
        "country": "Китайской Народной Республике",
        "registration_number": "91440300MA5EX12345",
        "role": "поставщик",
    },
]

MARKERS = {
    "AST-003": "делистинг",
    "AST-004": "иностранным цифровым инструментом",
    "RTE-004": "Отклонение курса",
    "TRV-003": "актуализируют реквизиты",
    "FTC-004": "комиссионер или поверенный",
}

WALLET = "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE"


def _contract_body(prompt: str) -> str:
    _, _, body = prompt.partition("Текст договора:\n")
    return body


def _sentence_around(text: str, marker: str) -> str:
    index = text.find(marker)
    if index < 0:
        return ""
    start = max(text.rfind(".", 0, index) + 1, 0)
    end = text.find(".", index + len(marker))
    end = len(text) if end < 0 else end + 1
    return " ".join(text[start:end].split())


class PerfectModel:
    """Выписывает дословно всё, что есть в окне."""

    def __init__(self) -> None:
        self.task_id = ""
        self.window = 0

    def start(self, task_id: str) -> None:
        self.task_id = task_id
        self.window = 0

    def _notes(self, body: str) -> list[dict]:
        notes = []
        for code, marker in MARKERS.items():
            if marker in body:
                notes.append(
                    {
                        "code": code,
                        "present": True,
                        "quote": _sentence_around(body, marker),
                        "reading": "условие присутствует в тексте",
                    }
                )
        return notes

    def __call__(self, prompt: str) -> LlmReply:
        self.window += 1
        body = _contract_body(prompt)
        answer = {
            "parties": [item for item in PARTIES if item["name"].split("«")[-1].strip("»") in body
                        or item["name"] in body],
            "wallets": [{"value": WALLET, "network": "TRON"}] if WALLET in body else [],
            "notes": self._notes(body),
        }
        return LlmReply(text=json.dumps(answer, ensure_ascii=False), model="perfect")


class SloppyModel:
    """Пересказывает вместо цитаты, теряет конец договора, добавляет лишнее."""

    def __init__(self) -> None:
        self.task_id = ""
        self.window = 0

    def start(self, task_id: str) -> None:
        self.task_id = task_id
        self.window = 0

    def __call__(self, prompt: str) -> LlmReply:
        self.window += 1
        body = _contract_body(prompt)
        notes = []
        if self.window == 1:
            for code, marker in list(MARKERS.items())[:2]:
                if marker in body:
                    notes.append(
                        {
                            "code": code,
                            "present": True,
                            "quote": f"в договоре сказано про {marker}",
                            "reading": "по смыслу статьи 12 закона 282-ФЗ",
                        }
                    )
            notes.append(
                {
                    "code": "THR-003",
                    "present": True,
                    "quote": "график платежей разбит на части",
                    "reading": "похоже на дробление",
                }
            )
        answer = {"parties": PARTIES[:1], "wallets": [], "notes": notes}
        return LlmReply(text=json.dumps(answer, ensure_ascii=False), model="sloppy")


class BrokenModel:
    def start(self, task_id: str) -> None:
        return

    def __call__(self, _prompt: str) -> LlmReply:
        return LlmReply(text='{"notes": [ {"code": "FTC-004",', model="broken")
