"""Коннекторы сверки контрагента.

Российская сторона (есть ИНН): сначала DaData и Контур.Фокус — они дают
правовой статус полем и подключаются ключом клиента (`app/counterparty/ru.py`).
Публичные страницы ЕГРЮЛ, rusprofile.ru и saby.ru остаются запасным
вариантом на случай, когда ключей нет: они подтверждают существование
карточки, но не правовой статус.

Иностранная сторона: GLEIF без ключа, OpenCorporates по бесплатному токену
(на живом прогоне 15.09.2026 без токена отвечает 401).

Любая сторона дополнительно проходит санкционный и PEP-скрининг
(`app/counterparty/sanctions.py`).

Наружу уходят ИНН и наименование, не текст договора.
"""

from __future__ import annotations

import re

import httpx

from app import __version__
from app.core import provider
from app.core.catalog import load_catalog
from app.core.net import source_unavailable
from app.counterparty import ru, sanctions
from app.counterparty.models import SourceHit, names_match, skipped
from app.rules.guardrail import assert_clean

# ЕГРЮЛ отвечает медленно: на живом прогоне 15.09.2026 он не уложился в 8 с.
# Читаем дольше, соединение устанавливаем по-прежнему быстро.
_READ_TIMEOUT_S = 20.0
_CONNECT_TIMEOUT_S = 5.0
_TIMEOUT = httpx.Timeout(_READ_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S)
_HEADERS = {
    "User-Agent": f"CompLeggeAI/{__version__} (local contract check)",
    "Accept": "application/json, text/html;q=0.8",
}

_OC_SEARCH = "https://api.opencorporates.com/v0.4/companies/search"
_GLEIF_SEARCH = "https://api.gleif.org/api/v1/lei-records"

_EGRUL_START = "https://egrul.nalog.gov.ru/"
_RUSPROFILE_SEARCH = "https://www.rusprofile.ru/search"
_SABY_CARD = "https://saby.ru/contragents/{inn}"

_BLOCK_MARKERS = (
    "smartcaptcha",
    "g-recaptcha",
    "hcaptcha",
    "cf-challenge",
    "captcha required",
    "доступ ограничен",
    "just a moment",
)


def _blocked_page(html: str, inn: str) -> bool:
    """Капча и заглушка CDN — не карточка организации."""
    lowered = html.lower()
    if inn and inn in html and ("<h1" in lowered or "огрн" in lowered or "ogrn" in lowered):
        return False
    return any(marker in lowered for marker in _BLOCK_MARKERS)


def lookup_free_sources(
    inn: str | None,
    name: str,
    client: httpx.Client | None = None,
    *,
    foreign: bool = False,
    country: str | None = None,
) -> tuple[SourceHit, ...]:
    catalog = load_catalog()
    hits: list[SourceHit] = []
    own = client is None
    session = client or httpx.Client(timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True)
    try:
        if foreign:
            hits.extend(_lookup_foreign(name, session, catalog))
        else:
            hits.extend(_lookup_russian(inn, name, session, catalog))
        hits.extend(_screen(name, session, foreign=foreign, country=country))
    finally:
        if own:
            session.close()
    hits.extend(paid_party_notes())
    return tuple(hits)


def _screen(
    name: str, session: httpx.Client, *, foreign: bool, country: str | None = None
) -> list[SourceHit]:
    """Санкционный и PEP-скрининг: он одинаково нужен обеим сторонам."""
    if provider.status_of("sanctions", sanctions.SOURCE_ID).value == "disabled":
        return []
    return [sanctions.screen(name, session, foreign=foreign, country=country)]


def _lookup_russian(inn: str | None, name: str, session: httpx.Client, catalog) -> list[SourceHit]:
    if not inn:
        # Без ИНН российские карточки не ищутся ни по одному источнику, и об
        # этом уже сказано в итоговой строке по стороне. Раньше каждый источник
        # писал сюда своё «нет ИНН», и юрист читал три одинаковых сообщения.
        return []

    # Ключевые источники: правовой статус приходит полем, а не поиском слова
    # в HTML. Если хотя бы один отработал, публичные страницы не опрашиваем —
    # они добавят к заключению только строку «карточка существует».
    hits: list[SourceHit] = ru.lookup(inn, name, session)
    if any(hit.performed for hit in hits):
        return hits

    for source_id, fetch in (
        ("egrul", _egrul),
        ("rusprofile", _rusprofile),
        ("saby", _saby),
    ):
        if catalog.uses("counterparty", source_id):
            hits.append(fetch(inn, name, session))
    return hits


def _lookup_foreign(name: str, session: httpx.Client, catalog) -> list[SourceHit]:
    hits: list[SourceHit] = []
    query = " ".join(name.split())
    if len(query) < 4:
        if catalog.uses("counterparty", "opencorporates"):
            hits.append(skipped("opencorporates", "нет наименования иностранной стороны"))
        if catalog.uses("counterparty", "gleif"):
            hits.append(skipped("gleif", "нет наименования иностранной стороны"))
        return hits
    if catalog.uses("counterparty", "opencorporates"):
        hits.append(_opencorporates(query, session))
    if catalog.uses("counterparty", "gleif"):
        hits.append(_gleif(query, session))
    return hits


def paid_party_notes() -> tuple[SourceHit, ...]:
    """Источники, включённые в каталоге, но не отработавшие по устройству.

    Источник без ключа докладывает о себе сам — он знает, где взять ключ.
    Здесь остаётся один случай: запись включена, а коннектора к ней нет.
    """
    return tuple(
        skipped(item.id, item.detail)
        for item in provider.blocked("counterparty")
        if item.status is provider.ProviderStatus.NOT_IMPLEMENTED
    )


def _egrul(inn: str, name: str, session: httpx.Client) -> SourceHit:
    try:
        start = session.post(_EGRUL_START, data={"query": inn})
        start.raise_for_status()
        if _blocked_page(start.text, inn):
            return skipped("egrul", "ЕГРЮЛ не вернул результат поиска (капча или заглушка)")
        if "text/html" in start.headers.get("content-type", "") and "t" not in start.text[:200]:
            try:
                payload = start.json()
            except ValueError:
                return skipped("egrul", "ЕГРЮЛ вернул страницу вместо данных (часто капча)")
        else:
            try:
                payload = start.json()
            except ValueError:
                return skipped("egrul", "ЕГРЮЛ вернул не JSON")
        token = str(payload.get("t") or "")
        if payload.get("captchaRequired") or not token:
            return skipped("egrul", "ЕГРЮЛ не вернул результат поиска")
        result = session.get(f"{_EGRUL_START}search-result/{token}")
        result.raise_for_status()
        body = result.json()
        rows = body.get("rows") or body.get("items") or []
        if not isinstance(rows, list) or not rows:
            detail = "в ЕГРЮЛ запись по ИНН не найдена"
            assert_clean(detail)
            return SourceHit(
                source_id="egrul",
                performed=True,
                found=False,
                inn=inn,
                detail=detail,
            )
        row = next((item for item in rows if str(item.get("i") or "") == inn), rows[0])
        legal = str(row.get("n") or row.get("c") or "").strip()
        ogrn = str(row.get("o") or row.get("k") or "").strip() or None
        ended = str(row.get("e") or "").strip()
        status = "прекращена" if ended else "действует"
        match = names_match(name, legal) if name and legal else None
        detail = f"ЕГРЮЛ: {legal or 'наименование не разобрано'}, статус: {status}"
        assert_clean(detail)
        return SourceHit(
            source_id="egrul",
            performed=True,
            found=True,
            legal_name=legal or None,
            inn=str(row.get("i") or inn),
            ogrn=ogrn,
            status=status,
            name_match=match,
            detail=detail,
        )
    except httpx.ConnectTimeout:
        return skipped(
            "egrul",
            f"ЕГРЮЛ: соединение не установилось за {_CONNECT_TIMEOUT_S:.0f} с, "
            "сверка не выполнена",
        )
    except httpx.TimeoutException:
        return skipped(
            "egrul",
            f"ЕГРЮЛ не ответил за {_READ_TIMEOUT_S:.0f} с, сверка не выполнена",
        )
    except httpx.HTTPStatusError as error:
        return skipped(
            "egrul",
            f"ЕГРЮЛ ответил отказом (HTTP {error.response.status_code}), сверка не выполнена",
        )
    except httpx.HTTPError as error:
        return skipped("egrul", source_unavailable("ЕГРЮЛ", error))
    except ValueError:
        return skipped("egrul", "ЕГРЮЛ вернул неразборчивый ответ")


# Признаки организационно-правовой формы в наименовании. Нужны, чтобы отличить
# карточку организации от служебного заголовка сайта.
_LEGAL_FORM = re.compile(
    r"\b(ООО|АО|ПАО|ЗАО|ОАО|НАО|ИП|ГУП|МУП|ФГУП|АНО|НКО|Фонд|Ассоциация)\b",
    re.IGNORECASE,
)

HTML_SOURCE_LIMITS = (
    "карточка по ИНН открылась; правовой статус по ней не определяется, "
    "проверьте в ЕГРЮЛ"
)


def _html_title(html: str) -> str:
    match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
    raw = match.group(1) if match else ""
    if not raw:
        title = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        raw = title.group(1) if title else ""
    return re.sub(r"<[^>]+>", "", raw)


def _page_hit(source_id: str, inn: str, name: str, html: str) -> SourceHit:
    """Что можно утверждать по HTML-карточке агрегатора.

    Правовой статус поиском слова по всей странице определять нельзя.
    На прогоне 15.09.2026 карточка Сбербанка на saby.ru дала «ликвидирована»:
    слово стоит в стороннем блоке разметки — в списке связанных организаций.
    В заключение уходило «по реестру организация ликвидирована» про
    действующий банк.

    Поэтому от страницы берётся одно утверждение: карточка по этому ИНН
    открылась. Правовой статус даёт только ЕГРЮЛ, структурированным ответом.
    """
    if inn not in html:
        detail = f"{source_id}: страница не содержит запрошенный ИНН"
        assert_clean(detail)
        return SourceHit(
            source_id=source_id,
            performed=True,
            found=False,
            inn=inn,
            detail=detail,
        )

    title = " ".join(_html_title(html).split())
    candidate = title.split("—")[0].split("|")[0].strip() if title else ""
    # Наименование принимаем, только если оно похоже на наименование
    # организации: есть форма собственности либо оно совпало с запрошенным.
    plausible = bool(candidate) and (
        bool(_LEGAL_FORM.search(candidate))
        or (bool(name) and names_match(name, candidate))
    )
    legal = candidate if plausible else None
    match = names_match(name, legal) if name and legal else None

    bits = [source_id]
    if legal:
        bits.append(legal)
    detail = ": ".join(bits) if len(bits) > 1 else f"{source_id}: карточка по ИНН открыта"
    detail = f"{detail}. {HTML_SOURCE_LIMITS}"
    assert_clean(detail)
    return SourceHit(
        source_id=source_id,
        performed=True,
        found=True,
        legal_name=legal,
        inn=inn,
        # status не заполняется сознательно: см. докстроку
        status=None,
        name_match=match,
        detail=detail,
    )


def _rusprofile(inn: str, name: str, session: httpx.Client) -> SourceHit:
    try:
        response = session.get(_RUSPROFILE_SEARCH, params={"query": inn})
        response.raise_for_status()
        if _blocked_page(response.text, inn):
            return skipped("rusprofile", "Rusprofile вернул капчу или заглушку, сверка не выполнена")
        return _page_hit("rusprofile", inn, name, response.text)
    except httpx.ConnectTimeout:
        return skipped(
            "rusprofile",
            f"Rusprofile: соединение не установилось за {_CONNECT_TIMEOUT_S:.0f} с, "
            "сверка не выполнена",
        )
    except httpx.TimeoutException:
        return skipped(
            "rusprofile",
            f"Rusprofile не ответил за {_READ_TIMEOUT_S:.0f} с, сверка не выполнена",
        )
    except httpx.HTTPStatusError as error:
        code = error.response.status_code
        if code == 404:
            # 404 на поиске — это не «организации нет», это «нет такого адреса».
            # Выдавать это за отсутствие записи нельзя.
            return skipped(
                "rusprofile",
                "Rusprofile вернул 404 на адрес поиска: сверка не выполнена. "
                "Похоже, адрес поиска на сайте изменился",
            )
        return skipped(
            "rusprofile",
            f"Rusprofile ответил отказом (HTTP {code}), сверка не выполнена",
        )
    except httpx.HTTPError as error:
        return skipped("rusprofile", source_unavailable("Rusprofile", error))


def _saby(inn: str, name: str, session: httpx.Client) -> SourceHit:
    try:
        response = session.get(_SABY_CARD.format(inn=inn))
        response.raise_for_status()
        if _blocked_page(response.text, inn):
            return skipped("saby", "СБИС вернул капчу или заглушку, сверка не выполнена")
        return _page_hit("saby", inn, name, response.text)
    except httpx.ConnectTimeout:
        return skipped(
            "saby",
            f"СБИС: соединение не установилось за {_CONNECT_TIMEOUT_S:.0f} с, "
            "сверка не выполнена",
        )
    except httpx.TimeoutException:
        return skipped(
            "saby",
            f"СБИС не ответил за {_READ_TIMEOUT_S:.0f} с, сверка не выполнена",
        )
    except httpx.HTTPStatusError as error:
        return skipped(
            "saby",
            f"СБИС ответил отказом (HTTP {error.response.status_code}), сверка не выполнена",
        )
    except httpx.HTTPError as error:
        return skipped("saby", source_unavailable("СБИС", error))


OPENCORPORATES_NO_TOKEN = (
    "OpenCorporates без токена отвечает отказом: задайте бесплатный "
    "OPENCORPORATES_API_TOKEN в .env. Сверка не выполнена"
)


def _opencorporates(name: str, session: httpx.Client) -> SourceHit:
    catalog = load_catalog()
    source = catalog.source("counterparty", "opencorporates")
    token = catalog.secret(source) if source else ""
    if not token:
        # Анонимный запрос к v0.4 отвечает 401 (проверено 15.09.2026).
        # Тратить на него секунду каждого прогона незачем.
        return skipped("opencorporates", OPENCORPORATES_NO_TOKEN)
    params: dict[str, str | int] = {"q": name, "per_page": 5, "api_token": token}
    try:
        response = session.get(_OC_SEARCH, params=params)
        response.raise_for_status()
        payload = response.json()
        rows = ((payload.get("results") or {}).get("companies")) or []
        if not isinstance(rows, list) or not rows:
            detail = "OpenCorporates: по наименованию записей нет"
            assert_clean(detail)
            return SourceHit(
                source_id="opencorporates",
                performed=True,
                found=False,
                legal_name=None,
                detail=detail,
            )
        picked = _pick_company_row(name, rows, key="company")
        if picked is None:
            detail = "OpenCorporates: однозначной записи по наименованию нет"
            assert_clean(detail)
            return SourceHit(
                source_id="opencorporates",
                performed=True,
                found=False,
                detail=detail,
            )
        legal = str(picked.get("name") or "").strip()
        number = str(picked.get("company_number") or "").strip() or None
        jurisdiction = str(picked.get("jurisdiction_code") or "").strip() or None
        inactive = bool(picked.get("inactive"))
        status = str(picked.get("current_status") or "").strip() or (
            "inactive" if inactive else "active"
        )
        match = names_match(name, legal) if name and legal else None
        bits = ["OpenCorporates", legal or "наименование не разобрано"]
        if jurisdiction:
            bits.append(jurisdiction)
        if number:
            bits.append(f"номер {number}")
        bits.append(status)
        detail = ": ".join(bits[:2]) + (", " + ", ".join(bits[2:]) if len(bits) > 2 else "")
        assert_clean(detail)
        return SourceHit(
            source_id="opencorporates",
            performed=True,
            found=True,
            legal_name=legal or None,
            registration_number=number,
            jurisdiction=jurisdiction,
            status=status,
            name_match=match,
            detail=detail,
        )
    except httpx.TimeoutException:
        return skipped("opencorporates", "таймаут запроса, сверка не выполнена")
    except httpx.HTTPStatusError as error:
        code = error.response.status_code
        if code in (401, 403):
            return skipped("opencorporates", OPENCORPORATES_NO_TOKEN)
        return skipped(
            "opencorporates",
            f"OpenCorporates ответил отказом (HTTP {code}), сверка не выполнена",
        )
    except httpx.HTTPError as error:
        return skipped("opencorporates", source_unavailable("OpenCorporates", error))
    except (ValueError, TypeError):
        return skipped("opencorporates", "OpenCorporates вернул неразборчивый ответ")


def _gleif(name: str, session: httpx.Client) -> SourceHit:
    try:
        response = session.get(
            _GLEIF_SEARCH,
            params={"filter[entity.legalName]": name, "page[size]": 5},
            headers={"Accept": "application/vnd.api+json"},
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data") or []
        if not isinstance(rows, list) or not rows:
            detail = "GLEIF: по наименованию записей LEI нет"
            assert_clean(detail)
            return SourceHit(
                source_id="gleif",
                performed=True,
                found=False,
                detail=detail,
            )
        chosen: dict | None = None
        legal = ""
        for item in rows:
            if not isinstance(item, dict):
                continue
            entity = ((item.get("attributes") or {}).get("entity")) or {}
            name_block = entity.get("legalName") or {}
            candidate = str(
                name_block.get("name") if isinstance(name_block, dict) else name_block or ""
            ).strip()
            if candidate and names_match(name, candidate):
                chosen = item
                legal = candidate
                break
        if chosen is None:
            detail = "GLEIF: однозначной записи LEI по наименованию нет"
            assert_clean(detail)
            return SourceHit(
                source_id="gleif",
                performed=True,
                found=False,
                detail=detail,
            )
        entity = ((chosen.get("attributes") or {}).get("entity")) or {}
        lei = str((chosen.get("attributes") or {}).get("lei") or chosen.get("id") or "").strip()
        status = str(entity.get("status") or "").strip() or None
        address = entity.get("legalAddress") or {}
        country = ""
        if isinstance(address, dict):
            country = str(address.get("country") or "").strip()
        match = names_match(name, legal) if name and legal else None
        bits = ["GLEIF", legal or "наименование не разобрано"]
        if lei:
            bits.append(f"LEI {lei}")
        if country:
            bits.append(country)
        if status:
            bits.append(status)
        detail = ": ".join(bits[:2]) + (", " + ", ".join(bits[2:]) if len(bits) > 2 else "")
        assert_clean(detail)
        return SourceHit(
            source_id="gleif",
            performed=True,
            found=True,
            legal_name=legal or None,
            registration_number=lei or None,
            jurisdiction=country or None,
            status=status,
            name_match=match,
            detail=detail,
        )
    except httpx.TimeoutException:
        return skipped("gleif", "таймаут запроса, сверка не выполнена")
    except httpx.HTTPStatusError as error:
        return skipped(
            "gleif",
            f"GLEIF ответил отказом (HTTP {error.response.status_code}), сверка не выполнена",
        )
    except httpx.HTTPError as error:
        return skipped("gleif", source_unavailable("GLEIF", error))
    except (ValueError, TypeError):
        return skipped("gleif", "GLEIF вернул неразборчивый ответ")


def _pick_company_row(name: str, rows: list, *, key: str) -> dict | None:
    matched: list[dict] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        row = item.get(key) if key in item else item
        if not isinstance(row, dict):
            continue
        legal = str(row.get("name") or "").strip()
        if legal and names_match(name, legal):
            matched.append(row)
    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        return matched[0]
    return None

