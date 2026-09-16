"""Российское юрлицо: DaData и Контур.Фокус.

Прямой запрос к egrul.nalog.gov.ru с рабочей машины часто не проходит:
на живом прогоне 15.09.2026 соединение не установилось за пять секунд.
Поэтому основной источник правового статуса — DaData: те же сведения ЕГРЮЛ,
отданные по нормальному REST с токеном, статус отдельным полем.

Контур.Фокус подключается ключом клиента и добавляет то, чего в ЕГРЮЛ нет:
красные и жёлтые факты экспресс-отчёта — признаки прекращения деятельности,
недостоверности сведений, массовости адреса.

Наружу уходит ИНН, не текст договора.
"""

from __future__ import annotations

from typing import Any

import httpx

from app import __version__
from app.core import provider
from app.core.net import source_unavailable
from app.counterparty.models import SourceHit, names_match, skipped
from app.rules.guardrail import assert_clean

_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_HEADERS = {
    "User-Agent": f"CompLeggeAI/{__version__} (local contract check)",
    "Accept": "application/json",
}

_DADATA_PATH = "/suggestions/api/4_1/rs/findById/party"
_DADATA_BASE = "https://suggestions.dadata.ru"
_KONTUR_BASE = "https://focus-api.kontur.ru"

DADATA_NO_KEY = (
    "DaData: ключ не задан. Бесплатный ключ выдаётся на dadata.ru, "
    "10 000 запросов в сутки. Сверка не выполнена"
)
KONTUR_NO_KEY = "Контур.Фокус: ключ не задан, сверка не выполнена"

# Как DaData называет состояние записи в ЕГРЮЛ.
_DADATA_STATUS = {
    "ACTIVE": "действующая",
    "LIQUIDATING": "в процессе ликвидации",
    "LIQUIDATED": "ликвидирована",
    "BANKRUPT": "банкротство",
    "REORGANIZING": "в процессе реорганизации",
}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _dig(payload: Any, *path: str) -> Any:
    current = payload
    for step in path:
        if not isinstance(current, dict):
            return None
        current = current.get(step)
    return current


# --------------------------------------------------------------------- DaData


def dadata(inn: str, name: str, session: httpx.Client) -> SourceHit:
    """Карточка ЕГРЮЛ по ИНН. Статус берётся полем, а не поиском слова в тексте."""
    token = provider.key("counterparty", "dadata")
    if not token:
        return skipped("dadata", DADATA_NO_KEY)

    url = provider.base_url("counterparty", "dadata", _DADATA_BASE) + _DADATA_PATH
    try:
        response = session.post(
            url,
            json={"query": inn, "count": 1},
            headers={"Authorization": f"Token {token}", "Content-Type": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as error:
        code = error.response.status_code
        if code in (401, 403):
            return skipped("dadata", "DaData отклонила ключ, сверка не выполнена")
        if code == 429:
            return skipped("dadata", "DaData: превышен лимит запросов, сверка не выполнена")
        return skipped("dadata", f"DaData ответила отказом (HTTP {code}), сверка не выполнена")
    except httpx.HTTPError as error:
        return skipped("dadata", source_unavailable("DaData", error))
    except ValueError:
        return skipped("dadata", "DaData вернула неразбираемый ответ, сверка не выполнена")

    rows = payload.get("suggestions") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        detail = "DaData: записи по этому ИНН в ЕГРЮЛ нет"
        assert_clean(detail)
        return SourceHit(source_id="dadata", performed=True, found=False, inn=inn, detail=detail)

    data = rows[0].get("data") if isinstance(rows[0], dict) else None
    if not isinstance(data, dict):
        return skipped("dadata", "DaData вернула запись без сведений, сверка не выполнена")

    legal_name = (
        _text(_dig(data, "name", "full_with_opf"))
        or _text(_dig(data, "name", "short_with_opf"))
        or _text(rows[0].get("value") if isinstance(rows[0], dict) else "")
        or None
    )
    raw_status = _text(_dig(data, "state", "status")).upper()
    status = _DADATA_STATUS.get(raw_status) or (raw_status.lower() or None)

    markers: list[str] = []
    if raw_status and raw_status != "ACTIVE":
        markers.append(f"ЕГРЮЛ: {status}")

    parts: list[str] = []
    head = _text(_dig(data, "management", "name"))
    post = _text(_dig(data, "management", "post"))
    if head:
        parts.append(f"руководитель: {head}" + (f", {post.lower()}" if post else ""))
    okved = _text(data.get("okved"))
    if okved:
        parts.append(f"ОКВЭД {okved}")
    address = _text(_dig(data, "address", "value"))
    if address:
        parts.append(f"адрес: {address}")
    detail = "DaData (ЕГРЮЛ): " + ("; ".join(parts) if parts else "карточка найдена")
    assert_clean(detail)

    return SourceHit(
        source_id="dadata",
        performed=True,
        found=True,
        legal_name=legal_name,
        inn=_text(data.get("inn")) or inn,
        ogrn=_text(data.get("ogrn")) or None,
        status=status,
        name_match=names_match(name, legal_name) if (name and legal_name) else None,
        detail=detail,
        markers=tuple(markers),
    )


# --------------------------------------------------------------- Контур.Фокус


def _kontur_get(session: httpx.Client, path: str, key: str, inn: str) -> Any:
    url = provider.base_url("counterparty", "kontur_focus", _KONTUR_BASE) + path
    response = session.get(url, params={"key": key, "inn": inn})
    response.raise_for_status()
    return response.json()


def _kontur_first(payload: Any) -> dict | None:
    """Фокус отвечает массивом записей — берём запись по запрошенному ИНН."""
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    if isinstance(payload, dict):
        return payload
    return None


def _kontur_name(entry: dict) -> str | None:
    for path in (("UL", "legalName", "full"), ("UL", "legalName", "short")):
        value = _text(_dig(entry, *path))
        if value:
            return value
    fio = _text(_dig(entry, "IP", "fio"))
    return fio or None


def _statements(entry: dict, field: str) -> tuple[str, ...]:
    """Факты экспресс-отчёта. Текст берётся у источника, не переписывается."""
    rows = entry.get(field)
    if not isinstance(rows, list):
        return ()
    found: list[str] = []
    for row in rows:
        if isinstance(row, str):
            text = _text(row)
        elif isinstance(row, dict):
            text = _text(row.get("text")) or _text(row.get("description")) or _text(row.get("title"))
        else:
            continue
        if text:
            found.append(text)
    return tuple(found)


def kontur_focus(inn: str, name: str, session: httpx.Client) -> SourceHit:
    """Реквизиты и статус из `/api3/req` плюс красные факты `/api3/briefReport`."""
    key = provider.key("counterparty", "kontur_focus")
    if not key:
        return skipped("kontur_focus", KONTUR_NO_KEY)

    try:
        entry = _kontur_first(_kontur_get(session, "/api3/req", key, inn))
    except httpx.HTTPStatusError as error:
        code = error.response.status_code
        if code in (401, 403):
            return skipped("kontur_focus", "Контур.Фокус отклонил ключ, сверка не выполнена")
        if code == 429:
            return skipped(
                "kontur_focus", "Контур.Фокус: превышен лимит запросов, сверка не выполнена"
            )
        return skipped(
            "kontur_focus", f"Контур.Фокус ответил отказом (HTTP {code}), сверка не выполнена"
        )
    except httpx.HTTPError as error:
        return skipped("kontur_focus", source_unavailable("Контур.Фокус", error))
    except ValueError:
        return skipped("kontur_focus", "Контур.Фокус вернул неразбираемый ответ, сверка не выполнена")

    if entry is None:
        detail = "Контур.Фокус: записи по этому ИНН нет"
        assert_clean(detail)
        return SourceHit(
            source_id="kontur_focus", performed=True, found=False, inn=inn, detail=detail
        )

    legal_name = _kontur_name(entry)
    status = _text(_dig(entry, "UL", "status", "statusString")) or _text(
        _dig(entry, "IP", "status", "statusString")
    )

    markers: list[str] = []
    red: tuple[str, ...] = ()
    yellow: tuple[str, ...] = ()
    try:
        report = _kontur_first(_kontur_get(session, "/api3/briefReport", key, inn))
    except (httpx.HTTPError, ValueError):
        # Реквизиты уже получены: сверка выполнена, экспресс-отчёта нет.
        report = None
    if report is not None:
        red = _statements(report, "redStatements")
        yellow = _statements(report, "yellowStatements")
        markers.extend(f"красный факт: {item}" for item in red)
        markers.extend(f"жёлтый факт: {item}" for item in yellow)
    if status and status.lower() not in ("действующее", "действующая", "действующий"):
        markers.insert(0, f"Контур.Фокус: {status}")

    parts: list[str] = []
    if red:
        parts.append(f"красных фактов {len(red)}")
    if yellow:
        parts.append(f"жёлтых фактов {len(yellow)}")
    if report is None:
        parts.append("экспресс-отчёт не получен")
    detail = "Контур.Фокус: " + ("; ".join(parts) if parts else "карточка найдена")
    assert_clean(detail)

    return SourceHit(
        source_id="kontur_focus",
        performed=True,
        found=True,
        legal_name=legal_name,
        inn=_text(entry.get("inn")) or inn,
        ogrn=_text(entry.get("ogrn")) or None,
        status=status or None,
        name_match=names_match(name, legal_name) if (name and legal_name) else None,
        detail=detail,
        markers=tuple(markers),
        link=_text(entry.get("focusHref")) or None,
    )


def lookup(inn: str, name: str, session: httpx.Client) -> list[SourceHit]:
    """Оба источника по российскому ИНН, каждый в меру своей готовности."""
    hits: list[SourceHit] = []
    if provider.status_of("counterparty", "dadata").value != "disabled":
        hits.append(dadata(inn, name, session))
    if provider.status_of("counterparty", "kontur_focus").value != "disabled":
        hits.append(kontur_focus(inn, name, session))
    return hits
