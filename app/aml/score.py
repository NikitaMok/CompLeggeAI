"""Предварительная оценка адреса по открытым данным.

Это не цифровой анализ в смысле статьи 35 № 282-ФЗ: нет присвоения уровня
риска сделке, нет включения в реестр поставщиков таких услуг. Оценка нужна
стороне внешнеторгового договора, чтобы увидеть, что публичный адрес
выглядит пустым, только что созданным или, наоборот, сверхнагруженным.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from app.aml.kyt import KytVerdict, worst
from app.rules.guardrail import assert_clean

DISCLAIMER = (
    "Предварительная оценка по открытым данным блокчейна. "
    "Не является цифровым анализом в смысле статьи 35 Федерального закона "
    "от 04.08.2026 № 282-ФЗ."
)

assert_clean(DISCLAIMER)

BAND_LOW = "низкий"
BAND_ELEVATED = "повышенный"
BAND_HIGH = "высокий"
BAND_CRITICAL = "критический"

# Как оценка коммерческого сервиса ложится на полосу заключения. Оценка
# по открытым данным — эвристика; купленный скоринг измеряет то же самое
# по закрытым данным, поэтому его вывод полосу и определяет.
_KYT_BAND = {
    "низкий": BAND_LOW,
    "умеренный": BAND_ELEVATED,
    "высокий": BAND_HIGH,
    "критический": BAND_CRITICAL,
}

# Полосу можно только поднять: купленный скоринг с «низким» не отменяет
# тревожных признаков, увиденных в публичной истории адреса.
_BAND_ORDER = {
    "нет данных": -1,
    BAND_LOW: 0,
    BAND_ELEVATED: 1,
    BAND_HIGH: 2,
    BAND_CRITICAL: 3,
}


@dataclass(frozen=True)
class AddressSnapshot:
    """Что известно об адресе. None означает «не знаем», а не «ноль».

    «Баланс USDT 0» — утверждение о состоянии адреса; «баланс неизвестен» —
    признание, что источник его не отдал. Раньше второе превращалось в первое
    и добавляло баллы риска за нулевой баланс, которого никто не измерял.
    """

    address: str
    network: str
    created_at: datetime | None = None
    tx_count: int | None = None
    usdt_balance: Decimal | None = None
    risk_labels: tuple[str, ...] = ()
    error: str | None = None
    # Найдена ли вообще запись об адресе в сети. False — адрес не активирован.
    account_found: bool | None = None
    # Есть ли подтверждённые операции. Отдельно от tx_count: источник может
    # подтвердить наличие операций, не назвав их числа.
    has_activity: bool | None = None
    # True, когда tx_count — нижняя граница (страница выдачи заполнена целиком).
    tx_count_is_floor: bool = False
    # Дата последней подтверждённой операции: спящий адрес — тоже сигнал.
    last_activity_at: datetime | None = None


@dataclass(frozen=True)
class AddressScore:
    address: str
    network: str
    score: int | None
    band: str
    factors: tuple[str, ...]
    labels: tuple[str, ...] = ()
    disclaimer: str = DISCLAIMER
    error: str | None = None
    source_notes: tuple[str, ...] = ()
    # Оценки коммерческих сервисов по ключам клиента. Пустой кортеж означает,
    # что ни один не подключён, а не что адрес чист.
    kyt: tuple[KytVerdict, ...] = ()
    # Факты по цепочке, на которых построена оценка. Нужны отчёту: юрист
    # читает не балл, а дату первой операции и число переводов.
    created_at: datetime | None = None
    tx_count: int | None = None
    tx_count_is_floor: bool = False
    last_activity_at: datetime | None = None
    usdt_balance: Decimal | None = None
    account_found: bool | None = None
    has_activity: bool | None = None

    @property
    def screened(self) -> bool:
        return any(item.performed for item in self.kyt)

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "network": self.network,
            "score": self.score,
            "band": self.band,
            "factors": list(self.factors),
            "labels": list(self.labels),
            "disclaimer": self.disclaimer,
            "error": self.error,
            "source_notes": list(self.source_notes),
            "screened": self.screened,
            "kyt": [item.to_dict() for item in self.kyt],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "tx_count": self.tx_count,
            "tx_count_is_floor": self.tx_count_is_floor,
            "last_activity_at": (
                self.last_activity_at.isoformat() if self.last_activity_at else None
            ),
            "usdt_balance": str(self.usdt_balance) if self.usdt_balance is not None else None,
            "account_found": self.account_found,
            "has_activity": self.has_activity,
        }


def _facts(snapshot: AddressSnapshot) -> dict:
    """Показатели цепочки, которые отчёт печатает рядом с оценкой."""
    return {
        "created_at": snapshot.created_at,
        "tx_count": snapshot.tx_count,
        "tx_count_is_floor": snapshot.tx_count_is_floor,
        "last_activity_at": snapshot.last_activity_at,
        "usdt_balance": snapshot.usdt_balance,
        "account_found": snapshot.account_found,
        "has_activity": snapshot.has_activity,
    }


def _age_days(created_at: datetime, now: datetime) -> int:
    start = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return max(0, (current - start).days)


def score_snapshot(
    snapshot: AddressSnapshot,
    *,
    threshold: int = 50,
    now: datetime | None = None,
    kyt: tuple[KytVerdict, ...] = (),
) -> AddressScore:
    if snapshot.error:
        return AddressScore(
            address=snapshot.address,
            network=snapshot.network,
            score=None,
            band="нет данных",
            factors=(),
            labels=snapshot.risk_labels,
            error=snapshot.error,
            kyt=kyt,
            **_facts(snapshot),
        )

    points = 0
    factors: list[str] = []
    moment = now or datetime.now(timezone.utc)

    if snapshot.account_found is False:
        # Адрес не активирован в сети: по нему не было ни одной операции.
        # Для получателя платежа по внешнеторговому договору это сильный сигнал.
        points += 35
        factors.append("адрес не активирован в сети: операций по нему не было")
    elif (
        snapshot.created_at is None
        and snapshot.tx_count is None
        and snapshot.has_activity is None
    ):
        points += 30
        factors.append("источник не вернул историю адреса: оценка неполная")

    if snapshot.created_at is not None:
        age = _age_days(snapshot.created_at, moment)
        if age < 30:
            points += 25
            factors.append(f"адрес создан {age} дн. назад")
        elif age < 90:
            points += 15
            factors.append(f"адрес создан {age} дн. назад")

    if snapshot.tx_count is not None:
        if snapshot.tx_count == 0:
            points += 20
            factors.append("подтверждённых операций не найдено")
        elif snapshot.tx_count_is_floor:
            factors.append(f"подтверждённых операций не менее {snapshot.tx_count}")
        elif snapshot.tx_count < 5:
            points += 15
            factors.append(f"подтверждённых операций: {snapshot.tx_count}")
        elif snapshot.tx_count > 10_000:
            points += 10
            factors.append(f"очень высокая активность: {snapshot.tx_count} операций")
    elif snapshot.has_activity is False and snapshot.account_found is not False:
        points += 20
        factors.append("подтверждённых операций не найдено")

    if snapshot.last_activity_at is not None:
        idle = _age_days(snapshot.last_activity_at, moment)
        if idle > 365:
            points += 10
            factors.append(f"последняя операция {idle} дн. назад")

    # Нулевой баланс — утверждение, и оно делается только когда баланс измерен.
    # Отсутствие сведений баллов не добавляет.
    if snapshot.usdt_balance is not None and snapshot.usdt_balance == 0:
        points += 10
        factors.append("нулевой баланс USDT")
    elif snapshot.usdt_balance is None and snapshot.account_found is not False:
        factors.append("баланс USDT источник не вернул")

    if snapshot.risk_labels:
        points += 40
        if any(
            label in snapshot.risk_labels
            for label in ("санкции", "миксер", "фишинг", "отмывание", "киберпреступление")
        ):
            points += 20
        factors.append("публичные метки риска: " + ", ".join(snapshot.risk_labels))

    score = min(100, points)
    if score >= threshold:
        band = BAND_HIGH
    elif score >= max(20, threshold * 3 // 5):
        band = BAND_ELEVATED
    else:
        band = BAND_LOW
        if not factors:
            factors.append("публичная история не показывает явных признаков риска")

    # Купленный скоринг перебивает эвристику: он видит то, чего в публичной
    # истории адреса нет. Полосу поднимаем, но не опускаем — если по открытым
    # данным адрес выглядит плохо, «низкий» от сервиса это не отменяет.
    verdict = worst(kyt)
    if verdict is not None:
        mapped = _KYT_BAND.get(verdict.risk_level or "", band)
        if _BAND_ORDER.get(mapped, 0) > _BAND_ORDER.get(band, 0):
            band = mapped
        head = f"{verdict.provider}: риск {verdict.risk_level}"
        if verdict.risk_score is not None:
            head += f", балл {verdict.risk_score:.0f}"
        factors.insert(0, head)
        factors.extend(f"{verdict.provider}: {signal}" for signal in verdict.signals[:4])
    elif kyt:
        factors.append(
            "коммерческий скоринг адреса не выполнен: оценка построена "
            "только на открытых данных"
        )

    for fragment in factors:
        assert_clean(fragment)

    return AddressScore(
        address=snapshot.address,
        network=snapshot.network,
        score=score,
        band=band,
        factors=tuple(factors),
        labels=snapshot.risk_labels,
        kyt=kyt,
        **_facts(snapshot),
    )
