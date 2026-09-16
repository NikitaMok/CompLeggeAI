from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from app.parsing.document import Document, load_document
from app.parsing.extract import ExtractedFacts, extract_facts
from app.parsing.structure import Clause, split_clauses


@dataclass(frozen=True)
class ContractView:
    """Контракт в виде, пригодном для проверки правилами."""

    text: str
    clauses: list[Clause]
    facts: ExtractedFacts

    @classmethod
    def from_document(cls, document: Document) -> ContractView:
        return cls(
            text=document.text,
            clauses=split_clauses(document),
            facts=extract_facts(document.text),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> ContractView:
        return cls.from_document(load_document(path))

    def matching_clauses(self, *patterns: str) -> list[Clause]:
        """Пункты, текст которых удовлетворяет всем шаблонам сразу."""
        compiled = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
        return [
            clause
            for clause in self.clauses
            if all(regex.search(clause.text) for regex in compiled)
        ]

    def has_all(self, *patterns: str) -> bool:
        return all(re.search(pattern, self.text, re.IGNORECASE) for pattern in patterns)

    def has_any(self, *patterns: str) -> bool:
        return any(re.search(pattern, self.text, re.IGNORECASE) for pattern in patterns)

    def contract_amount(self) -> Decimal | None:
        return self.facts.max_amount("RUB")

    def foreign_contract_amount(self):
        """Сумма договора в иностранной валюте, если рублёвой в тексте нет."""
        return self.facts.max_foreign_amount()
