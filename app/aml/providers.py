"""Запросы к публичным API блокчейна.

Какие источники включены — `config/providers.yaml`.
Без ключа TronGrid отвечает с лимитом; Etherscan без ключа не вызывается.
GoPlus отдаёт публичные метки риска. Платные коннекторы, если включены
в yaml без реализации, в отчёте дают «оценка не выполнена», а не «чисто».
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from app.aml.score import AddressSnapshot
from app.core import provider
from app.core.catalog import load_catalog
from app.core.config import get_settings
from app.core.net import source_unavailable
from app.parsing.extract import WalletAddress

_TIMEOUT = httpx.Timeout(8.0, connect=4.0)

_GOPLUS_FLAGS = {
    "phishing_activities": "фишинг",
    "blackmail_activities": "вымогательство",
    "stealing_attack": "хищение",
    "fake_kyc": "поддельный KYC",
    "malicious_mining_activities": "вредоносный майнинг",
    "darkweb_transactions": "даркнет",
    "cybercrime": "киберпреступление",
    "money_laundering": "отмывание",
    "financial_crime": "финансовое преступление",
    "mixer": "миксер",
    "sanctioned": "санкции",
    "honeypot_related_address": "honeypot",
    "gas_abuse": "злоупотребление gas",
    "reinit": "повторная инициализация контракта",
    "fake_token": "поддельный токен",
}

_CHAIN_ID = {"EVM": "1", "TRON": "tron"}


def fetch_snapshot(address: WalletAddress, client: httpx.Client | None = None) -> AddressSnapshot:
    catalog = load_catalog()
    if address.network == "TRON":
        if not catalog.uses("wallet", "trongrid"):
            snapshot = AddressSnapshot(
                address=address.value,
                network="TRON",
                error="TronGrid выключен в каталоге",
            )
        else:
            snapshot = _tron(address.value, client)
    elif address.network == "EVM":
        if not catalog.uses("wallet", "etherscan"):
            snapshot = AddressSnapshot(
                address=address.value,
                network="EVM",
                error="Etherscan выключен в каталоге",
            )
        else:
            snapshot = _evm(address.value, client)
    else:
        snapshot = AddressSnapshot(
            address=address.value,
            network=address.network,
            error=f"сеть {address.network} не поддерживается",
        )

    if catalog.uses("wallet", "goplus"):
        snapshot = _with_goplus(snapshot, address, client)
    return snapshot


def fetch_snapshots(
    addresses: list[WalletAddress],
    client: httpx.Client | None = None,
) -> list[AddressSnapshot]:
    own_client = client is None
    session = client or httpx.Client(timeout=_TIMEOUT)
    try:
        return [fetch_snapshot(address, session) for address in addresses]
    finally:
        if own_client:
            session.close()


def paid_wallet_notes() -> tuple[str, ...]:
    """Источники, включённые в каталоге, но не отработавшие по устройству.

    Про отсутствующий ключ докладывает сам коннектор: он знает, где ключ
    взять. Здесь остаётся случай, когда запись включена, а коннектора нет.
    """
    return tuple(
        f"{item.id}: {item.detail}"
        for item in provider.blocked("wallet")
        if item.status is provider.ProviderStatus.NOT_IMPLEMENTED
    )


def _with_goplus(
    snapshot: AddressSnapshot,
    address: WalletAddress,
    client: httpx.Client | None,
) -> AddressSnapshot:
    labels, error = _goplus(address, client)
    if error and snapshot.error:
        combined = f"{snapshot.error}; {error}"
        return replace(snapshot, error=combined, risk_labels=labels)
    if error and not snapshot.risk_labels and snapshot.error is None:
        # Цепочка ответила, метки нет — это не ломает возраст и баланс.
        return replace(snapshot, risk_labels=labels)
    return replace(snapshot, risk_labels=labels)


def _goplus(
    address: WalletAddress,
    client: httpx.Client | None,
) -> tuple[tuple[str, ...], str | None]:
    chain = _CHAIN_ID.get(address.network)
    if chain is None:
        return (), f"GoPlus: сеть {address.network} не поддерживается"
    session = client or httpx.Client(timeout=_TIMEOUT)
    try:
        response = session.get(
            f"https://api.gopluslabs.io/api/v1/address_security/{chain}",
            params={"address": address.value},
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result")
        if not isinstance(result, dict):
            message = str(payload.get("message") or "нет данных")
            return (), f"GoPlus: {message[:200]}"
        labels = tuple(
            title
            for key, title in _GOPLUS_FLAGS.items()
            if str(result.get(key) or "") == "1"
        )
        return labels, None
    except httpx.HTTPError as error:
        return (), source_unavailable("GoPlus", error)
    finally:
        if client is None:
            session.close()


def _tron(address: str, client: httpx.Client | None) -> AddressSnapshot:
    settings = get_settings()
    headers = {}
    if settings.trongrid_api_key:
        headers["TRON-PRO-API-KEY"] = settings.trongrid_api_key
    session = client or httpx.Client(timeout=_TIMEOUT)
    try:
        account = session.get(
            f"https://api.trongrid.io/v1/accounts/{address}",
            headers=headers,
        )
        account.raise_for_status()
        payload = account.json()
        rows = payload.get("data") or []
        if not rows:
            # Пустая выдача у TronGrid означает неактивированный адрес: записи
            # о нём в сети нет. Раньше это записывалось как tx_count=0 —
            # «операций не найдено», то есть измерение вместо факта отсутствия.
            return AddressSnapshot(
                address=address,
                network="TRON",
                account_found=False,
                has_activity=False,
            )

        row = rows[0]
        usdt = _tron_usdt(row)
        activity = _tron_activity(session, address, headers)
        # `create_time` в карточке TronGrid есть не всегда: на живом ответе
        # 15.09.2026 его не было. Возраст берём по первой операции, иначе — None.
        created_at = _timestamp(row.get("create_time")) or activity.first_at
        return AddressSnapshot(
            address=address,
            network="TRON",
            created_at=created_at,
            tx_count=activity.count,
            tx_count_is_floor=activity.count_is_floor,
            has_activity=activity.has_activity,
            last_activity_at=activity.last_at,
            usdt_balance=usdt,
            account_found=True,
        )
    except httpx.HTTPError as error:
        return AddressSnapshot(
            address=address,
            network="TRON",
            error=source_unavailable("TronGrid", error),
        )
    finally:
        if client is None:
            session.close()


# Контракт USDT в сети TRON. TronGrid перечисляет балансы TRC-20 по адресу
# контракта, а не по тикеру: в ответе лежит {"TR7NHq…": "12500000"}. Прежний код
# искал в ключе подстроку «USDT» и не находил её никогда — баланс USDT не читался
# ни у одного адреса, а функция возвращала ноль как измеренное значение.
USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
# У USDT TRC-20 шесть знаков после запятой.
_USDT_DECIMALS = Decimal(1_000_000)


def _tron_usdt(row: dict) -> Decimal | None:
    """Баланс USDT по карточке адреса.

    None — источник не описал балансы TRC-20, и сказать о них нечего.
    Ноль возвращается только когда список балансов пришёл и USDT в нём нет:
    это измерение, а не отсутствие данных. Разница попадает в отчёт баллами
    риска, поэтому смешивать их нельзя.
    """
    if "trc20" not in row:
        return None
    tokens = row.get("trc20")
    if not isinstance(tokens, list):
        return None
    for item in tokens:
        if not isinstance(item, dict):
            continue
        for key, amount in item.items():
            token = str(key)
            if token == USDT_TRC20_CONTRACT or "USDT" in token.upper():
                try:
                    return Decimal(str(amount)) / _USDT_DECIMALS
                except (ArithmeticError, ValueError):
                    return None
    return Decimal(0)


# Сколько операций запрашиваем одной страницей. Точное число нужно там, где
# оно и важно: почти пустой адрес. У загруженного адреса берём нижнюю границу
# и так её и называем, вместо того чтобы молчать.
_TRON_PAGE = 200


@dataclass(frozen=True)
class TronActivity:
    """Что удалось узнать об операциях по адресу."""

    count: int | None = None
    count_is_floor: bool = False
    has_activity: bool | None = None
    first_at: datetime | None = None
    last_at: datetime | None = None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _tron_transactions(
    session: httpx.Client, address: str, headers: dict, params: dict
) -> list[dict] | None:
    try:
        response = session.get(
            f"https://api.trongrid.io/v1/accounts/{address}/transactions",
            params={"only_confirmed": "true", **params},
            headers=headers,
        )
        response.raise_for_status()
        data = response.json().get("data")
    except httpx.HTTPError:
        return None
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    return [row for row in data if isinstance(row, dict)]


def _tron_activity(session: httpx.Client, address: str, headers: dict) -> TronActivity:
    """Операции по адресу: сколько, когда первая и когда последняя.

    Прошлая версия запрашивала одну операцию и возвращала 0 либо None: у любого
    живого адреса число операций оказывалось «неизвестно», и адрес получал
    30 баллов за «нет сведений об истории».

    Возраст берётся отсюда же: в карточке TronGrid поля `create_time`
    на прогоне 15.09.2026 не оказалось. Дата первой операции для оценки риска
    подходит лучше — она показывает, когда адрес начал работать.
    """
    latest = _tron_transactions(session, address, headers, {"limit": _TRON_PAGE})
    if latest is None:
        return TronActivity()

    count = len(latest)
    if count == 0:
        return TronActivity(count=0, has_activity=False)

    last_at = _timestamp(latest[0].get("block_timestamp"))

    # Отдельный запрос за самой ранней операцией: страница выше отсортирована
    # от новых к старым, и по ней возраст адреса не виден.
    oldest = _tron_transactions(
        session, address, headers, {"limit": 1, "order_by": "block_timestamp,asc"}
    )
    first_at = _timestamp(oldest[0].get("block_timestamp")) if oldest else None

    return TronActivity(
        count=count,
        count_is_floor=count >= _TRON_PAGE,
        has_activity=True,
        first_at=first_at,
        last_at=last_at,
    )


def _evm(address: str, client: httpx.Client | None) -> AddressSnapshot:
    settings = get_settings()
    if not settings.etherscan_api_key:
        return AddressSnapshot(
            address=address,
            network="EVM",
            error="ключ Etherscan не задан",
        )
    session = client or httpx.Client(timeout=_TIMEOUT)
    try:
        response = session.get(
            "https://api.etherscan.io/api",
            params={
                "module": "account",
                "action": "txlist",
                "address": address,
                "startblock": 0,
                "endblock": 99999999,
                "page": 1,
                "offset": 1,
                "sort": "asc",
                "apikey": settings.etherscan_api_key,
            },
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result")
        if payload.get("status") != "1" or not isinstance(result, list):
            message = str(payload.get("message") or payload.get("result") or "нет данных")
            if "No transactions found" in message:
                return AddressSnapshot(address=address, network="EVM", tx_count=0)
            return AddressSnapshot(address=address, network="EVM", error=message[:200])

        first = result[0] if result else None
        created_at = None
        if first and first.get("timeStamp"):
            created_at = datetime.fromtimestamp(int(first["timeStamp"]), tz=timezone.utc)
        tx_count = len(result) if result else 0
        return AddressSnapshot(
            address=address,
            network="EVM",
            created_at=created_at,
            tx_count=tx_count,
        )
    except httpx.HTTPError as error:
        return AddressSnapshot(
            address=address,
            network="EVM",
            error=source_unavailable("Etherscan", error),
        )
    finally:
        if client is None:
            session.close()
