"""Разбор фактов по кошельку и по стороне договора.

Источники отдают показатели: возраст адреса, число операций, балл скоринга,
статус в реестре, красные факты. Юристу нужен не список показателей, а ответ
на три вопроса: что установлено, что настораживает и чего установить
не удалось. Здесь показатели сводятся к этим трём спискам и к одной строке
вывода.

Ничего не придумывается: каждая строка опирается на полученное значение.
Показатель, который источник не вернул, попадает в «не установлено»
и никогда — в «установлено».
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.rules.guardrail import assert_clean

# Полосы риска адреса — как их называет app/aml/score.py.
_BAND_HEADLINE = {
    "критический": "адрес отнесён к критическому уровню риска",
    "высокий": "адрес отнесён к высокому уровню риска",
    "повышенный": "адрес отнесён к повышенному уровню риска",
    "низкий": "признаков повышенного риска по адресу не выявлено",
    "нет данных": "оценка адреса не выполнена",
}

_ELEVATED = ("повышенный", "высокий", "критический")


@dataclass(frozen=True)
class Assessment:
    """Разбор одного объекта проверки: адреса либо стороны договора."""

    subject: str
    headline: str
    established: tuple[str, ...] = ()
    concerns: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    manual: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "subject": self.subject,
            "headline": self.headline,
            "established": list(self.established),
            "concerns": list(self.concerns),
            "gaps": list(self.gaps),
            "manual": list(self.manual),
        }


def _clean(*groups: list[str]) -> None:
    for group in groups:
        for line in group:
            assert_clean(line)


def _days(value: str | None, now: datetime) -> int | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0, (now - moment).days)


def _date(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(str(value)).date().isoformat()
    except ValueError:
        return str(value)


# ------------------------------------------------------------------- кошелёк


def assess_wallet(payload: dict, *, now: datetime | None = None) -> Assessment:
    """Разбор адреса: что о нём известно и что из этого следует для расчёта."""
    moment = now or datetime.now(timezone.utc)
    address = str(payload.get("address") or "адрес не назван")
    network = str(payload.get("network") or "")
    band = str(payload.get("band") or "нет данных")
    screened = bool(payload.get("screened"))

    established: list[str] = []
    concerns: list[str] = []
    gaps: list[str] = []
    manual: list[str] = []

    if network:
        established.append(f"Сеть расчёта: {network}.")

    account_found = payload.get("account_found")
    if account_found is False:
        concerns.append(
            "Адрес в сети не активирован: подтверждённых операций по нему нет. "
            "Платёж на такой адрес не имеет подтверждённой истории получателя."
        )
        manual.append(
            "Запросить у контрагента подтверждение владения адресом "
            "и его связи с расчётами по настоящему договору."
        )

    age = _days(payload.get("created_at"), moment)
    if age is not None:
        established.append(
            f"Первая операция по адресу: {_date(payload.get('created_at'))} "
            f"({age} дн. назад)."
        )
        if age < 30:
            concerns.append(
                "Адрес создан менее месяца назад: истории, пригодной для оценки, нет."
            )
        elif age < 90:
            concerns.append("Адрес создан менее трёх месяцев назад: история короткая.")
    elif account_found is not False:
        gaps.append("Дата первой операции источником не возвращена.")

    tx_count = payload.get("tx_count")
    if isinstance(tx_count, int):
        floor = "не менее " if payload.get("tx_count_is_floor") else ""
        established.append(f"Подтверждённых операций: {floor}{tx_count}.")
        if tx_count == 0:
            concerns.append("Подтверждённых операций по адресу не найдено.")
        elif tx_count > 10_000 and not payload.get("tx_count_is_floor"):
            concerns.append(
                "Обороты по адресу нехарактерны для расчётного счёта одной сделки: "
                "возможен адрес площадки или сервиса, а не контрагента."
            )
    elif payload.get("has_activity") is True:
        established.append("Операции по адресу есть; их число источником не названо.")
    elif account_found is not False:
        gaps.append("Число операций источником не возвращено.")

    idle = _days(payload.get("last_activity_at"), moment)
    if idle is not None:
        established.append(
            f"Последняя операция: {_date(payload.get('last_activity_at'))} "
            f"({idle} дн. назад)."
        )
        if idle > 365:
            concerns.append(
                "Более года без операций: адрес может быть недоступен получателю."
            )

    balance = payload.get("usdt_balance")
    if balance is not None:
        established.append(f"Баланс USDT на момент запроса: {balance}.")
    elif account_found is not False:
        gaps.append("Баланс источником не возвращён; нулевым он не считается.")

    labels = payload.get("labels") or []
    if labels:
        concerns.append("Публичные метки риска: " + ", ".join(str(x) for x in labels) + ".")

    # Коммерческий скоринг: он и определяет полосу, когда подключён.
    for verdict in payload.get("kyt") or []:
        name = str(verdict.get("provider") or "источник")
        if not verdict.get("performed"):
            gaps.append(f"{name}: {verdict.get('detail') or 'оценка не выполнена'}.")
            continue
        level = str(verdict.get("risk_level") or "")
        score = verdict.get("risk_score")
        line = f"{name}: уровень риска «{level}»" if level else f"{name}: оценка получена"
        if isinstance(score, (int, float)):
            line += f", балл {score:.0f}"
        established.append(line + ".")
        for signal in (verdict.get("signals") or [])[:5]:
            concerns.append(f"{name}: {signal}.")

    if not screened:
        gaps.append(
            "Коммерческий скоринг адреса не выполнялся. Признаки связи с нелегальным "
            "оборотом по открытым данным блокчейна не определяются."
        )
        manual.append(
            "До расчёта провести проверку адреса в специализированном сервисе "
            "либо получить такую проверку от контрагента."
        )

    if band in _ELEVATED:
        manual.append(
            "Вынести вопрос о расчёте на этот адрес на решение комплаенс-функции "
            "до подписания и до первого платежа."
        )

    headline = _BAND_HEADLINE.get(band, f"уровень риска: {band}")
    if band in _ELEVATED and screened:
        headline += " по данным подключённого сервиса"
    elif band in _ELEVATED:
        headline += " по открытым данным"
    if payload.get("error"):
        headline = "оценка адреса не выполнена"
        gaps.append(str(payload["error"]))

    if not concerns and band == "низкий":
        concerns.append(
            "Признаков, требующих отдельного решения, в полученных данных нет."
        )

    _clean(established, concerns, gaps, manual)
    return Assessment(
        subject=f"{address} ({network})" if network else address,
        headline=headline,
        established=tuple(established),
        concerns=tuple(concerns),
        gaps=tuple(gaps),
        manual=tuple(manual),
    )


# ----------------------------------------------------------------- контрагент


_SOURCE_TITLE = {
    "dadata": "ЕГРЮЛ (DaData)",
    "kontur_focus": "Контур.Фокус",
    "egrul": "ЕГРЮЛ",
    "rusprofile": "Rusprofile",
    "saby": "СБИС",
    "opencorporates": "OpenCorporates",
    "gleif": "Реестр LEI",
    "opensanctions": "Санкционный и PEP-скрининг",
}


def _title(source_id: str) -> str:
    return _SOURCE_TITLE.get(source_id, source_id)


def _prefixed(title: str, detail: str) -> str:
    """Не повторять имя источника, если он уже назвал себя сам.

    Коннектор пишет «DaData: ключ не задан», и строка «ЕГРЮЛ (DaData): DaData:
    ключ не задан» читается как ошибка вёрстки.
    """
    head = detail.split(":", 1)[0].strip().lower()
    if head and (head in title.lower() or title.lower() in head):
        return detail
    return f"{title}: {detail}"


def assess_party(payload: dict) -> Assessment:
    """Разбор стороны: правовой статус, тревожные признаки и пробелы сверки."""
    name = str(payload.get("name") or payload.get("inn") or "сторона не названа")
    role = str(payload.get("role") or "")
    foreign = bool(payload.get("foreign"))
    hits = payload.get("hits") or []

    established: list[str] = []
    concerns: list[str] = []
    gaps: list[str] = []
    manual: list[str] = []

    if role:
        established.append(f"Роль по договору: {role}.")
    if payload.get("inn"):
        established.append(f"ИНН в тексте договора: {payload['inn']}.")
    if payload.get("country"):
        established.append(f"Страна по договору: {payload['country']}.")

    performed = [hit for hit in hits if hit.get("performed")]
    found = [hit for hit in performed if hit.get("found")]
    sanctioned = any(hit.get("source") == "opensanctions" and hit.get("found") for hit in hits)
    screened = any(hit.get("source") == "opensanctions" and hit.get("performed") for hit in hits)

    legal_names: list[str] = []
    for hit in found:
        source = _title(str(hit.get("source")))
        if hit.get("source") == "opensanctions":
            continue
        legal_name = str(hit.get("legal_name") or "").strip()
        if legal_name and legal_name not in legal_names:
            legal_names.append(legal_name)
        status = str(hit.get("status") or "").strip()
        if status:
            established.append(f"{source}: правовой статус — {status}.")
        if hit.get("ogrn"):
            established.append(f"{source}: ОГРН {hit['ogrn']}.")
        if hit.get("registration_number"):
            established.append(
                f"{source}: регистрационный номер {hit['registration_number']}."
            )
        if hit.get("jurisdiction"):
            established.append(f"{source}: юрисдикция {hit['jurisdiction']}.")
        if hit.get("name_match") is False:
            concerns.append(
                f"{source}: наименование в реестре не совпадает с наименованием "
                "в договоре. Возможна ошибка в реквизитах либо другое лицо."
            )
        for marker in hit.get("markers") or []:
            concerns.append(f"{source}: {marker}.")

    if legal_names:
        established.insert(0, "Наименование по реестру: " + "; ".join(legal_names) + ".")

    for hit in hits:
        if hit.get("performed"):
            continue
        detail = str(hit.get("detail") or "сверка не выполнена")
        gaps.append(_prefixed(_title(str(hit.get("source"))), detail) + ".")

    if sanctioned:
        manual.insert(
            0,
            "До подписания получить правовую оценку допустимости сделки "
            "с учётом санкционного совпадения.",
        )
    elif not screened:
        gaps.append(
            "Санкционный и PEP-скрининг не выполнялся: совпадения с перечнями "
            "не проверялись."
        )
        manual.append("Провести санкционный скрининг стороны до подписания.")

    if performed and not found and not sanctioned:
        concerns.append(
            "Ни один из отработавших источников не нашёл запись по этой стороне."
        )

    if not performed:
        gaps.append("Ни один источник сверки не отработал.")

    if foreign:
        manual.append(
            "Запросить у иностранной стороны выписку из торгового реестра страны "
            "регистрации и документ, подтверждающий полномочия подписанта."
        )
    else:
        manual.append(
            "Сверить наименование, ИНН и ОГРН в преамбуле договора с реестровой записью."
        )

    summary = str(payload.get("summary") or "").strip()
    headline = summary or "сверка стороны не выполнена"

    _clean(established, concerns, gaps, manual)
    return Assessment(
        subject=f"{name} — {role}" if role else name,
        headline=headline,
        established=tuple(established),
        concerns=tuple(concerns),
        gaps=tuple(gaps),
        manual=tuple(manual),
    )
