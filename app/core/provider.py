"""Состояние источника: включён, есть ли ключ, написан ли коннектор.

Раньше эти три вопроса решались в каждом модуле по-своему, и включённый
платный источник без коннектора выглядел в отчёте так же, как источник,
который просто не ответил. Здесь состояние считается один раз и одинаково
для кошелька, контрагента и санкционного скрининга.

Правило прежнее: ни одно из состояний не превращается в «проверено».
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.core.catalog import Source, load_catalog

# Источники, к которым в проекте есть HTTP-коннектор. Запись в каталоге,
# которой здесь нет, в отчёте честно называется нереализованной.
IMPLEMENTED: dict[str, frozenset[str]] = {
    "wallet": frozenset({"trongrid", "etherscan", "goplus", "amlbot", "misttrack", "chainalysis"}),
    "counterparty": frozenset(
        {"egrul", "rusprofile", "saby", "opencorporates", "gleif", "dadata", "kontur_focus"}
    ),
    "sanctions": frozenset({"opensanctions"}),
}


class ProviderStatus(str, Enum):
    READY = "ready"
    NO_KEY = "no_key"
    NOT_IMPLEMENTED = "not_implemented"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


_DETAIL = {
    ProviderStatus.NO_KEY: "ключ не задан, сверка не выполнена",
    ProviderStatus.NOT_IMPLEMENTED: "коннектор не реализован, сверка не выполнена",
    ProviderStatus.DISABLED: "источник выключен в конфигурации",
    ProviderStatus.UNKNOWN: "источника нет в каталоге",
}


@dataclass(frozen=True)
class ProviderInfo:
    id: str
    group: str
    tier: str
    status: ProviderStatus
    what: str = ""
    env_key: str | None = None

    @property
    def ready(self) -> bool:
        return self.status is ProviderStatus.READY

    @property
    def detail(self) -> str:
        return _DETAIL.get(self.status, "")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "group": self.group,
            "tier": self.tier,
            "status": self.status.value,
            "what": self.what,
            "env_key": self.env_key,
            "detail": self.detail,
        }


def _needs_key(source: Source) -> bool:
    """Ключ обязателен платным источникам и тем бесплатным, где он указан
    как обязательный самим каталогом (`required_key: true`)."""
    if source.tier == "paid":
        return True
    return bool(source.required_key)


def status_of(group: str, source_id: str) -> ProviderStatus:
    catalog = load_catalog()
    source = catalog.source(group, source_id)
    if source is None:
        return ProviderStatus.UNKNOWN
    if not source.enabled:
        return ProviderStatus.DISABLED
    if source_id not in IMPLEMENTED.get(group, frozenset()):
        return ProviderStatus.NOT_IMPLEMENTED
    if _needs_key(source) and not catalog.secret(source):
        return ProviderStatus.NO_KEY
    return ProviderStatus.READY


def info(group: str, source_id: str) -> ProviderInfo:
    catalog = load_catalog()
    source = catalog.source(group, source_id)
    return ProviderInfo(
        id=source_id,
        group=group,
        tier=source.tier if source else "unknown",
        status=status_of(group, source_id),
        what=source.what if source else "",
        env_key=source.env_key if source else None,
    )


def inventory(group: str) -> tuple[ProviderInfo, ...]:
    """Все записи каталога группы с их состоянием — для отчёта и диагностики."""
    catalog = load_catalog()
    return tuple(info(group, source.id) for source in getattr(catalog, group, []))


def ready_ids(group: str) -> tuple[str, ...]:
    return tuple(item.id for item in inventory(group) if item.ready)


def uses(group: str, source_id: str) -> bool:
    return status_of(group, source_id) is ProviderStatus.READY


def blocked(group: str) -> tuple[ProviderInfo, ...]:
    """Включённые источники, которые не отработают, и причина каждого.

    Выключенные сюда не попадают: клиент выключил их сам, писать ему об этом
    в каждом заключении незачем.
    """
    return tuple(
        item
        for item in inventory(group)
        if item.status in (ProviderStatus.NO_KEY, ProviderStatus.NOT_IMPLEMENTED)
    )


def key(group: str, source_id: str) -> str:
    catalog = load_catalog()
    source = catalog.source(group, source_id)
    return catalog.secret(source) if source else ""


def base_url(group: str, source_id: str, fallback: str) -> str:
    catalog = load_catalog()
    source = catalog.source(group, source_id)
    if source and source.base_url:
        return source.base_url.rstrip("/")
    return fallback.rstrip("/")
