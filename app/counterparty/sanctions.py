"""Санкционный и PEP-скрининг через OpenSanctions.

Опорный режим — свой сервер `yente` рядом с сервисом: наименования
контрагентов не уходят даже в OpenSanctions. Тот же коннектор работает
с облачным api.opensanctions.org, если клиенту так удобнее: адрес и ключ
берутся из каталога и `.env`.

Скрининг не выносит вердикт по договору. Он добавляет в заключение факт:
сторона похожа на запись из санкционного списка либо на публичное
должностное лицо. Решение принимает юрист.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app import __version__
from app.core import provider
from app.core.config import get_settings
from app.core.net import source_unavailable
from app.counterparty.models import SourceHit, skipped
from app.rules.guardrail import assert_clean

SOURCE_ID = "opensanctions"
_BASE = "http://127.0.0.1:8001"
_DATASET = "default"

_HEADERS = {
    "User-Agent": f"CompLeggeAI/{__version__} (local contract check)",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

NOT_CONFIGURED = (
    "OpenSanctions: сервер скрининга не отвечает по адресу из конфигурации. "
    "Поднимите yente рядом с сервисом либо укажите адрес облачного API. "
    "Скрининг не выполнен"
)

# Как OpenSanctions называет темы риска. Переводим только те, что встречаются
# в заключении; незнакомую тему пишем как есть, не выдумывая перевод.
_TOPICS = {
    "sanction": "санкционный список",
    "sanction.linked": "связь с санкционным лицом",
    "role.pep": "публичное должностное лицо",
    "role.rca": "родственник или близкий публичного должностного лица",
    "crime": "уголовное преследование",
    "crime.fin": "финансовые преступления",
    "export.control": "экспортный контроль",
    "debarment": "отстранение от закупок",
    "wanted": "в розыске",
}


@dataclass(frozen=True)
class ScreeningMatch:
    entity_id: str
    caption: str
    score: float
    topics: tuple[str, ...]
    datasets: tuple[str, ...]

    @property
    def label(self) -> str:
        topics = ", ".join(_TOPICS.get(item, item) for item in self.topics) or "запись реестра"
        return f"{self.caption} — {topics} (совпадение {self.score:.2f})"


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str) and item.strip())
    return ()


def _results(payload: Any) -> list[dict]:
    """yente отвечает картой `responses`, облачный API — списком `results`."""
    if not isinstance(payload, dict):
        return []
    responses = payload.get("responses")
    if isinstance(responses, dict):
        for item in responses.values():
            if isinstance(item, dict) and isinstance(item.get("results"), list):
                return [row for row in item["results"] if isinstance(row, dict)]
        return []
    rows = payload.get("results")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    return []


def _match(row: dict, threshold: float) -> ScreeningMatch | None:
    raw_score = row.get("score")
    score = float(raw_score) if isinstance(raw_score, (int, float)) else 0.0
    decided = row.get("match")
    if decided is False:
        return None
    if decided is not True and score < threshold:
        return None
    caption = row.get("caption")
    if not isinstance(caption, str) or not caption.strip():
        names = _strings((row.get("properties") or {}).get("name"))
        caption = names[0] if names else str(row.get("id") or "запись без наименования")
    return ScreeningMatch(
        entity_id=str(row.get("id") or ""),
        caption=caption.strip(),
        score=score,
        topics=_strings((row.get("properties") or {}).get("topics")),
        datasets=_strings(row.get("datasets")),
    )


def _query(name: str, *, foreign: bool, country: str | None) -> dict:
    properties: dict[str, list[str]] = {"name": [name]}
    if country:
        properties["country"] = [country]
    return {
        "queries": {
            "party": {
                # Юрлицо. Физлицо-контрагент во внешнеторговом договоре — редкость,
                # а Company в yente сопоставляется и с Organization.
                "schema": "Company",
                "properties": properties,
            }
        }
    }


def screen(
    name: str,
    session: httpx.Client | None = None,
    *,
    foreign: bool = True,
    country: str | None = None,
    dataset: str = _DATASET,
) -> SourceHit:
    """Скрининг наименования по санкционным спискам и PEP."""
    status = provider.status_of("sanctions", SOURCE_ID)
    if status.value == "disabled":
        return skipped(SOURCE_ID, "скрининг выключен в конфигурации")
    query = " ".join((name or "").split())
    if len(query) < 3:
        return skipped(SOURCE_ID, "нет наименования стороны, скрининг не выполнен")

    settings = get_settings()
    url = f"{provider.base_url('sanctions', SOURCE_ID, _BASE)}/match/{dataset}"
    headers = dict(_HEADERS)
    key = provider.key("sanctions", SOURCE_ID)
    if key:
        headers["Authorization"] = f"ApiKey {key}"

    own = session is None
    client = session or httpx.Client(
        timeout=httpx.Timeout(settings.sanctions_timeout_s, connect=5.0)
    )
    try:
        response = client.post(url, json=_query(query, foreign=foreign, country=country), headers=headers)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as error:
        code = error.response.status_code
        if code in (401, 403):
            return skipped(SOURCE_ID, "OpenSanctions отклонил ключ, скрининг не выполнен")
        if code == 429:
            return skipped(SOURCE_ID, "OpenSanctions: превышен лимит запросов, скрининг не выполнен")
        return skipped(
            SOURCE_ID, f"OpenSanctions ответил отказом (HTTP {code}), скрининг не выполнен"
        )
    except httpx.ConnectError:
        return skipped(SOURCE_ID, NOT_CONFIGURED)
    except httpx.HTTPError as error:
        return skipped(SOURCE_ID, source_unavailable("OpenSanctions", error))
    except ValueError:
        return skipped(SOURCE_ID, "OpenSanctions вернул неразбираемый ответ, скрининг не выполнен")
    finally:
        if own:
            client.close()

    threshold = settings.sanctions_match_threshold
    matches = [item for item in (_match(row, threshold) for row in _results(payload)) if item]
    if not matches:
        detail = "OpenSanctions: совпадений в санкционных списках и перечнях PEP нет"
        assert_clean(detail)
        return SourceHit(source_id=SOURCE_ID, performed=True, found=False, detail=detail)

    matches.sort(key=lambda item: item.score, reverse=True)
    markers = tuple(item.label for item in matches[:5])
    detail = f"OpenSanctions: совпадений {len(matches)}"
    assert_clean(detail)
    return SourceHit(
        source_id=SOURCE_ID,
        performed=True,
        found=True,
        status="совпадение с санкционным или PEP-перечнем",
        detail=detail,
        markers=markers,
    )
