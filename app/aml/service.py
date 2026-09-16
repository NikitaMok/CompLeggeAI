"""Скоринг адресов, извлечённых из контракта.

Два слоя. Первый — факты по цепочке из публичных узлов: возраст адреса,
активность, баланс, открытые метки. Второй — коммерческий скоринг по ключам
клиента (`app/aml/kyt.py`): он видит то, чего в публичной истории нет.

Если куплен хотя бы один скоринг, полосу риска определяет он. Если ни одного
не подключено, в заключении прямо сказано, что оценка построена только
на открытых данных.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from app.aml import kyt
from app.aml.providers import fetch_snapshots, paid_wallet_notes
from app.aml.score import AddressScore, AddressSnapshot, score_snapshot
from app.core.config import get_settings
from app.rules.contract import ContractView
from app.rules.guardrail import assert_clean

Lookup = Callable[..., list[AddressSnapshot]]
Screen = Callable[..., tuple]


def score_contract_addresses(
    contract: ContractView,
    *,
    lookup: Lookup | None = None,
    screen: Screen | None = None,
    threshold: int | None = None,
) -> list[AddressScore]:
    addresses = contract.facts.wallet_addresses
    if not addresses:
        return []

    snapshots = (lookup or fetch_snapshots)(addresses)
    limit = threshold if threshold is not None else get_settings().aml_risk_threshold
    extras = paid_wallet_notes()
    for fragment in extras:
        assert_clean(fragment)

    run_screen = screen or kyt.screen
    scores: list[AddressScore] = []
    for snapshot in snapshots:
        verdicts = tuple(run_screen(snapshot.address, snapshot.network))
        score = score_snapshot(snapshot, threshold=limit, kyt=verdicts)
        if extras:
            score = replace(score, source_notes=extras)
        scores.append(score)
    return scores
