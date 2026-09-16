"""Коммерческий скоринг криптоадреса по ключу клиента.

Три поставщика через один интерфейс: клиент покупает доступ к тому, к чему
может, и вписывает ключ в `.env`. Наружу уходит адрес кошелька, не договор.

  MistTrack    — `GET /v2/risk_score`, ключ параметром `api_key`;
                 поля ответа задокументированы публично.
  Chainalysis  — Address Screening API: регистрация адреса `POST /api/risk/v2/entities`,
                 затем `GET /api/risk/v2/entities/{address}`, ключ в заголовке `Token`.
  AMLBot       — путь запроса задаётся настройкой `AMLBOT_API_URL`: публичной
                 спецификации у сервиса нет, он выдаёт её вместе с доступом.
                 Шаблон подставляет `{address}` и `{asset}`.

Ответ ни одного из них не превращается в вердикт по договору. Он попадает
в заключение фактом: такой-то сервис оценил адрес так-то.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app import __version__
from app.core import provider
from app.core.config import get_settings
from app.core.net import describe
from app.rules.guardrail import assert_clean

_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
_HEADERS = {
    "User-Agent": f"CompLeggeAI/{__version__} (local contract check)",
    "Accept": "application/json",
}

_MISTTRACK_BASE = "https://openapi.misttrack.io"
_CHAINALYSIS_BASE = "https://api.chainalysis.com"
_AMLBOT_BASE = "https://extrnl.amlbot.com"

# Уровни риска: свой словарь на каждый сервис, потому что шкалы разные.
LOW = "низкий"
MODERATE = "умеренный"
HIGH = "высокий"
SEVERE = "критический"

_ORDER = {LOW: 0, MODERATE: 1, HIGH: 2, SEVERE: 3}

_CHAINALYSIS_RISK = {
    "low": LOW,
    "medium": MODERATE,
    "high": HIGH,
    "severe": SEVERE,
}

# Монета в терминах MistTrack. Сеть определяется при извлечении адреса.
_MISTTRACK_COIN = {
    "TRON": "USDT-TRC20",
    "TRC20": "USDT-TRC20",
    "EVM": "ETH",
    "ETH": "ETH",
    "ETHEREUM": "ETH",
}


@dataclass(frozen=True)
class KytVerdict:
    """Оценка одного сервиса. performed=False — оценки нет, а не «чисто»."""

    provider: str
    performed: bool
    risk_score: float | None = None
    risk_level: str | None = None
    signals: tuple[str, ...] = ()
    detail: str = ""
    report_url: str | None = None

    @property
    def severity(self) -> int:
        return _ORDER.get(self.risk_level or "", -1)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "performed": self.performed,
            "risk_score": self.risk_score,
            "risk_level": self.risk_level,
            "signals": list(self.signals),
            "detail": self.detail,
            "report_url": self.report_url,
        }


def _not_performed(name: str, reason: str) -> KytVerdict:
    assert_clean(reason)
    return KytVerdict(provider=name, performed=False, detail=reason)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _misttrack_level(score: float | None, named: str) -> str | None:
    """Шкала MistTrack: Low 0–30, Moderate 31–70, High 71–90, Severe 91–100."""
    lowered = named.lower()
    for key, value in (("severe", SEVERE), ("high", HIGH), ("moderate", MODERATE), ("low", LOW)):
        if key in lowered:
            return value
    if score is None:
        return None
    if score >= 91:
        return SEVERE
    if score >= 71:
        return HIGH
    if score >= 31:
        return MODERATE
    return LOW


def misttrack(address: str, network: str, session: httpx.Client) -> KytVerdict:
    key = provider.key("wallet", "misttrack")
    if not key:
        return _not_performed("misttrack", "MistTrack: ключ не задан, оценка не выполнена")
    coin = _MISTTRACK_COIN.get((network or "").upper())
    if not coin:
        return _not_performed(
            "misttrack", f"MistTrack: сеть {network or 'не определена'} не поддержана, оценка не выполнена"
        )

    url = provider.base_url("wallet", "misttrack", _MISTTRACK_BASE) + "/v2/risk_score"
    try:
        response = session.get(url, params={"api_key": key, "coin": coin, "address": address})
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as error:
        return _not_performed(
            "misttrack",
            f"MistTrack ответил отказом (HTTP {error.response.status_code}), оценка не выполнена",
        )
    except httpx.HTTPError as error:
        return _not_performed("misttrack", f"MistTrack: {describe(error)}, оценка не выполнена")
    except ValueError:
        return _not_performed("misttrack", "MistTrack вернул неразбираемый ответ, оценка не выполнена")

    if not isinstance(payload, dict) or not payload.get("success"):
        reason = _text(payload.get("msg")) if isinstance(payload, dict) else ""
        return _not_performed(
            "misttrack", f"MistTrack: {reason or 'запрос отклонён'}, оценка не выполнена"
        )

    data = payload.get("data")
    if not isinstance(data, dict):
        return _not_performed("misttrack", "MistTrack вернул ответ без данных, оценка не выполнена")

    raw_score = data.get("score")
    score = float(raw_score) if isinstance(raw_score, (int, float)) else None
    level = _misttrack_level(score, _text(data.get("risk_level")))

    signals: list[str] = []
    hacking = _text(data.get("hacking_event"))
    if hacking:
        signals.append(f"инцидент: {hacking}")
    details = data.get("detail_list")
    if isinstance(details, list):
        signals.extend(item.strip() for item in details if isinstance(item, str) and item.strip())
    risk_detail = data.get("risk_detail")
    if isinstance(risk_detail, list):
        for row in risk_detail[:5]:
            if not isinstance(row, dict):
                continue
            entity = _text(row.get("entity"))
            risk_type = _text(row.get("risk_type"))
            hops = row.get("hop_num")
            label = " / ".join(part for part in (risk_type, entity) if part)
            if label:
                signals.append(label + (f", {hops} переход(ов)" if isinstance(hops, int) else ""))

    detail = "MistTrack: оценка получена"
    assert_clean(detail)
    return KytVerdict(
        provider="misttrack",
        performed=True,
        risk_score=score,
        risk_level=level,
        signals=tuple(dict.fromkeys(signals))[:8],
        detail=detail,
        report_url=_text(data.get("risk_report_url")) or None,
    )


def chainalysis(address: str, network: str, session: httpx.Client) -> KytVerdict:
    key = provider.key("wallet", "chainalysis")
    if not key:
        return _not_performed("chainalysis", "Chainalysis: ключ не задан, оценка не выполнена")

    base = provider.base_url("wallet", "chainalysis", _CHAINALYSIS_BASE) + "/api/risk/v2/entities"
    headers = {**_HEADERS, "Token": key, "Content-Type": "application/json"}
    try:
        # Адрес сначала регистрируется, затем читается его оценка. Повторная
        # регистрация уже известного адреса отвечает 200 либо 409 — это не ошибка.
        registered = session.post(base, json={"address": address}, headers=headers)
        if registered.status_code not in (200, 201, 202, 409):
            registered.raise_for_status()
        response = session.get(f"{base}/{address}", headers=headers)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as error:
        code = error.response.status_code
        if code in (401, 403):
            return _not_performed("chainalysis", "Chainalysis отклонил ключ, оценка не выполнена")
        return _not_performed(
            "chainalysis", f"Chainalysis ответил отказом (HTTP {code}), оценка не выполнена"
        )
    except httpx.HTTPError as error:
        return _not_performed("chainalysis", f"Chainalysis: {describe(error)}, оценка не выполнена")
    except ValueError:
        return _not_performed(
            "chainalysis", "Chainalysis вернул неразбираемый ответ, оценка не выполнена"
        )

    if not isinstance(payload, dict):
        return _not_performed("chainalysis", "Chainalysis вернул ответ без данных, оценка не выполнена")

    level = _CHAINALYSIS_RISK.get(_text(payload.get("risk")).lower())
    signals: list[str] = []
    reason = _text(payload.get("riskReason"))
    if reason:
        signals.append(reason)
    cluster = payload.get("cluster")
    if isinstance(cluster, dict):
        name = _text(cluster.get("name"))
        category = _text(cluster.get("category"))
        label = " / ".join(part for part in (category, name) if part)
        if label:
            signals.append(f"кластер: {label}")
    identifications = payload.get("addressIdentifications")
    if isinstance(identifications, list):
        for row in identifications[:5]:
            if isinstance(row, dict):
                name = _text(row.get("name")) or _text(row.get("category"))
                if name:
                    signals.append(f"метка: {name}")

    detail = "Chainalysis: оценка получена"
    assert_clean(detail)
    return KytVerdict(
        provider="chainalysis",
        performed=True,
        risk_level=level,
        signals=tuple(dict.fromkeys(signals))[:8],
        detail=detail,
    )


def _amlbot_level(payload: dict) -> tuple[float | None, str | None]:
    """AMLBot отдаёт риск долей 0..1 либо процентом — принимаем оба."""
    for key in ("riskscore", "risk_score", "score", "risk"):
        raw = payload.get(key)
        if isinstance(raw, (int, float)):
            value = float(raw)
            percent = value * 100 if value <= 1 else value
            if percent >= 75:
                return percent, SEVERE
            if percent >= 50:
                return percent, HIGH
            if percent >= 25:
                return percent, MODERATE
            return percent, LOW
    named = _text(payload.get("risk_level")) or _text(payload.get("level"))
    lowered = named.lower()
    for key, value in (("severe", SEVERE), ("high", HIGH), ("medium", MODERATE), ("low", LOW)):
        if key in lowered:
            return None, value
    return None, None


def amlbot(address: str, network: str, session: httpx.Client) -> KytVerdict:
    key = provider.key("wallet", "amlbot")
    if not key:
        return _not_performed("amlbot", "AMLBot: ключ не задан, оценка не выполнена")

    settings = get_settings()
    template = (settings.amlbot_api_url or "").strip()
    if not template:
        return _not_performed(
            "amlbot",
            "AMLBot: не задан AMLBOT_API_URL. Путь запроса сервис выдаёт "
            "вместе с доступом; впишите его в .env. Оценка не выполнена",
        )
    asset = _MISTTRACK_COIN.get((network or "").upper(), network or "")
    url = template.replace("{address}", address).replace("{asset}", asset).replace("{key}", key)

    headers = {**_HEADERS, "Authorization": f"Bearer {key}", "X-Api-Key": key}
    try:
        response = session.get(url, headers=headers)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as error:
        code = error.response.status_code
        if code == 404:
            return _not_performed(
                "amlbot",
                "AMLBot: адрес запроса не найден (HTTP 404), проверьте AMLBOT_API_URL. "
                "Оценка не выполнена",
            )
        if code in (401, 403):
            return _not_performed("amlbot", "AMLBot отклонил ключ, оценка не выполнена")
        return _not_performed("amlbot", f"AMLBot ответил отказом (HTTP {code}), оценка не выполнена")
    except httpx.HTTPError as error:
        return _not_performed("amlbot", f"AMLBot: {describe(error)}, оценка не выполнена")
    except ValueError:
        return _not_performed("amlbot", "AMLBot вернул неразбираемый ответ, оценка не выполнена")

    if not isinstance(payload, dict):
        return _not_performed("amlbot", "AMLBot вернул ответ без данных, оценка не выполнена")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    score, level = _amlbot_level(data)
    signals: list[str] = []
    for key_name in ("signals", "riskySources", "risky_sources", "detail_list", "tags"):
        rows = data.get(key_name)
        if isinstance(rows, list):
            for row in rows[:8]:
                if isinstance(row, str) and row.strip():
                    signals.append(row.strip())
                elif isinstance(row, dict):
                    label = _text(row.get("name")) or _text(row.get("type")) or _text(row.get("title"))
                    share = row.get("share") or row.get("percent")
                    if label:
                        signals.append(
                            label + (f", доля {float(share):.0%}" if isinstance(share, (int, float)) and share <= 1 else "")
                        )

    detail = "AMLBot: оценка получена"
    assert_clean(detail)
    return KytVerdict(
        provider="amlbot",
        performed=True,
        risk_score=score,
        risk_level=level,
        signals=tuple(dict.fromkeys(signals))[:8],
        detail=detail,
        report_url=_text(data.get("report_url")) or None,
    )


_PROVIDERS = {
    "amlbot": amlbot,
    "misttrack": misttrack,
    "chainalysis": chainalysis,
}


def screen(address: str, network: str, session: httpx.Client | None = None) -> tuple[KytVerdict, ...]:
    """Все включённые коммерческие скоринги по одному адресу."""
    wanted = [
        source_id
        for source_id in _PROVIDERS
        if provider.status_of("wallet", source_id).value != "disabled"
    ]
    if not wanted:
        return ()

    own = session is None
    client = session or httpx.Client(timeout=_TIMEOUT, headers=_HEADERS)
    try:
        return tuple(_PROVIDERS[source_id](address, network, client) for source_id in wanted)
    finally:
        if own:
            client.close()


def worst(verdicts: tuple[KytVerdict, ...]) -> KytVerdict | None:
    """Самая строгая из полученных оценок. Неполученные не участвуют."""
    performed = [item for item in verdicts if item.performed and item.risk_level]
    if not performed:
        return None
    return max(performed, key=lambda item: item.severity)
