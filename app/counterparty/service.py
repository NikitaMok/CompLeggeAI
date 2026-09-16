"""Сборка сверки контрагента из фактов договора."""

from __future__ import annotations

from collections.abc import Callable

from app.counterparty.models import PartyCheck, summarize
from app.counterparty.providers import lookup_free_sources
from app.llm.clauses import PartyNote
from app.parsing.extract import PartyMention
from app.rules.contract import ContractView
from app.rules.guardrail import assert_clean

Lookup = Callable[..., tuple]


def _foreign_name(name: str) -> bool:
    lowered = name.lower()
    return any(
        token in lowered
        for token in (
            "ltd",
            "limited",
            "inc",
            "gmbh",
            "llc",
            "co.",
            "pte",
            "corp",
            "b.v.",
            "s.a.",
            "n.v.",
            "plc",
        )
    )


Row = tuple[str, str | None, str | None, str | None]


def _fuller(current: str, candidate: str) -> bool:
    """Модель часто выписывает наименование целиком там, где разбор по тексту
    взял его кусок. Более полное принимается, только если короткое в него
    входит: иначе это две разные стороны, а не одна."""
    if not candidate or candidate == current:
        return False
    if not current:
        return True
    return current in candidate and len(candidate) > len(current)


def _merge_parties(
    mentions: list[PartyMention],
    llm_parties: tuple[PartyNote, ...],
) -> list[Row]:
    by_inn: dict[str, tuple[str, str | None, str | None]] = {}
    nameless: list[tuple[str, str | None, str | None]] = []
    seen_names: set[str] = set()
    for item in mentions:
        if item.inn:
            by_inn.setdefault(item.inn, (item.name, item.role, None))
        elif item.name:
            nameless.append((item.name, item.role, None))
            seen_names.add(item.name)
    for item in llm_parties:
        country = item.country or None
        if item.inn:
            known = by_inn.get(item.inn)
            if known is None:
                by_inn[item.inn] = (item.name, item.role or None, country)
            else:
                name, role, known_country = known
                by_inn[item.inn] = (
                    item.name if _fuller(name, item.name) else name,
                    role or (item.role or None),
                    known_country or country,
                )
        elif item.name:
            # То же лицо модель могла выписать полнее, чем разбор по тексту,
            # и вдобавок назвать страну и роль. Совпавшую запись дополняем,
            # новую добавляем.
            merged = False
            for index, (name, role, known_country) in enumerate(nameless):
                if name == item.name or _fuller(name, item.name):
                    nameless[index] = (
                        item.name if _fuller(name, item.name) else name,
                        role or (item.role or None),
                        known_country or country,
                    )
                    seen_names.add(item.name)
                    merged = True
                    break
            if merged:
                continue
            if item.name in seen_names:
                continue
            if all(item.name != name for name, _role, _c in by_inn.values()):
                nameless.append((item.name, item.role or None, country))
                seen_names.add(item.name)

    rows: list[Row] = [
        (name or f"ИНН {inn}", inn, role, country)
        for inn, (name, role, country) in by_inn.items()
    ]
    rows.extend((name, None, role, country) for name, role, country in nameless)
    return rows


def review_counterparties(
    contract: ContractView,
    *,
    llm_parties: tuple[PartyNote, ...] = (),
    lookup: Lookup | None = None,
) -> list[PartyCheck]:
    rows = _merge_parties(contract.facts.parties, llm_parties)
    if not rows:
        summary = "в тексте нет ИНН и наименования стороны; сверка не выполнена"
        assert_clean(summary)
        return [
            PartyCheck(name="", inn=None, foreign=False, hits=(), summary=summary)
        ]

    fetch = lookup or lookup_free_sources
    checks: list[PartyCheck] = []
    for name, inn, role, country in rows:
        foreign = (not inn) and (_foreign_name(name) or bool(country))
        # Страна из договора сужает санкционный скрининг: одноимённых компаний
        # в мировых реестрах много, и без страны совпадение мало что значит.
        hits = tuple(fetch(inn, name, foreign=foreign, country=country))
        summary = summarize(hits, foreign=foreign, has_inn=bool(inn))
        checks.append(
            PartyCheck(
                name=name,
                inn=inn,
                foreign=foreign,
                hits=hits,
                summary=summary,
                role=role,
                country=country,
            )
        )
    return checks
