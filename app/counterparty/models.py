"""Сверка контрагента по открытым источникам.

В сеть уходят ИНН и наименование, не текст договора.
Нет ответа источника — в отчёте «сверка не выполнена», не «всё в порядке».
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.rules.guardrail import assert_clean

NOT_PERFORMED = "сверка не выполнена"
NO_RUSSIAN_INN = (
    "российского ИНН в тексте нет; сверка по реестрам юридических лиц не выполнялась"
)
FOREIGN_NOT_PERFORMED = "иностранный контрагент: сверка не выполнена"
FOREIGN_NOT_FOUND = (
    "в открытых иностранных реестрах запись по наименованию не найдена"
)
FOREIGN_FOUND = (
    "запись в открытом иностранном реестре найдена; это не подтверждение правоспособности"
)
FOREIGN_NAME_MISMATCH = (
    "запись в иностранном реестре найдена, наименование в договоре с ней не совпадает"
)
FOREIGN_INACTIVE = "по открытому реестру организация недействующая или исключена"
SANCTIONED = (
    "сторона совпала с записью санкционного либо PEP-перечня: "
    "совпадение проверить юристу до подписания"
)


@dataclass(frozen=True)
class SourceHit:
    source_id: str
    performed: bool
    found: bool
    legal_name: str | None = None
    inn: str | None = None
    ogrn: str | None = None
    registration_number: str | None = None
    jurisdiction: str | None = None
    status: str | None = None
    name_match: bool | None = None
    detail: str = ""
    # Тревожные признаки, названные самим источником: красные факты
    # Контур.Фокуса, попадание в санкционный список, статус ликвидации.
    markers: tuple[str, ...] = ()
    # Ссылка на карточку у источника, если он её вернул.
    link: str | None = None

    def to_dict(self) -> dict:
        return {
            "source": self.source_id,
            "performed": self.performed,
            "found": self.found,
            "legal_name": self.legal_name,
            "inn": self.inn,
            "ogrn": self.ogrn,
            "registration_number": self.registration_number,
            "jurisdiction": self.jurisdiction,
            "status": self.status,
            "name_match": self.name_match,
            "detail": self.detail,
            "markers": list(self.markers),
            "link": self.link,
        }


@dataclass(frozen=True)
class PartyCheck:
    name: str
    inn: str | None
    foreign: bool
    hits: tuple[SourceHit, ...]
    summary: str
    # Кем лицо названо в договоре: сторона, цифровой депозитарий, эмитент.
    # Пусто, если из текста роль не следует.
    role: str | None = None
    # Страна из текста договора: сужает санкционный скрининг.
    country: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "inn": self.inn,
            "foreign": self.foreign,
            "role": self.role,
            "country": self.country,
            "hits": [hit.to_dict() for hit in self.hits],
            "summary": self.summary,
        }


def _norm_name(value: str) -> str:
    text = value.lower().replace("ё", "е")
    text = re.sub(r"[«»\"'`]", "", text)
    text = re.sub(
        r"\b(ооо|ао|пао|зао|оао|нао|ип|ltd|limited|inc|gmbh|llc|co)\b\.?",
        "",
        text,
    )
    return re.sub(r"[^a-zа-я0-9]+", "", text)


def names_match(left: str, right: str) -> bool:
    a, b = _norm_name(left), _norm_name(right)
    if not a or not b:
        return False
    return a in b or b in a


def skipped(source_id: str, reason: str) -> SourceHit:
    assert_clean(reason)
    return SourceHit(source_id=source_id, performed=False, found=False, detail=reason)


def _inactive(status: str | None) -> bool:
    if not status:
        return False
    lowered = status.lower()
    markers = (
        "ликвидир",
        # DaData отдаёт LIQUIDATING как «в процессе ликвидации»: корень другой,
        # а смысл для юриста тот же — с такой стороной договор не подписывают.
        "ликвидац",
        "исключ",
        "прекращ",
        "банкрот",
        "недейств",
        "inactive",
        "dissolved",
        "struck",
        "revoked",
        "retired",
    )
    return any(marker in lowered for marker in markers)


def summarize(hits: tuple[SourceHit, ...], *, foreign: bool, has_inn: bool) -> str:
    # Санкционное совпадение решает раньше всего остального: оно не зависит
    # ни от наличия ИНН, ни от того, нашлась ли карточка в реестре.
    if _sanctioned([hit for hit in hits if hit.performed and hit.found]):
        assert_clean(SANCTIONED)
        return SANCTIONED
    if foreign:
        return _summarize_foreign(hits)
    if not has_inn:
        assert_clean(NO_RUSSIAN_INN)
        return NO_RUSSIAN_INN
    performed = [hit for hit in hits if hit.performed]
    if not performed:
        assert_clean(NOT_PERFORMED)
        return NOT_PERFORMED
    found = [hit for hit in performed if hit.found]
    if not found:
        text = "в открытых источниках запись по ИНН не найдена"
        assert_clean(text)
        return text
    mismatch = [hit for hit in found if hit.name_match is False]
    liquidated = [hit for hit in found if _inactive(hit.status)]
    if liquidated:
        text = "по реестру организация ликвидирована либо исключена из ЕГРЮЛ"
        assert_clean(text)
        return text
    if _sanctioned(found):
        assert_clean(SANCTIONED)
        return SANCTIONED
    flagged = _markers(found)
    if flagged:
        text = "источник назвал тревожные признаки: " + "; ".join(flagged[:3])
        assert_clean(text)
        return text
    if mismatch:
        text = "запись в реестре найдена, наименование в договоре с ней не совпадает"
        assert_clean(text)
        return text
    text = "запись в открытом реестре найдена; это не подтверждение правоспособности"
    assert_clean(text)
    return text


def _sanctioned(hits: list[SourceHit]) -> bool:
    return any(hit.source_id == "opensanctions" and hit.found for hit in hits)


def _markers(hits: list[SourceHit]) -> list[str]:
    """Тревожные признаки, названные источниками, без повторов."""
    seen: list[str] = []
    for hit in hits:
        if hit.source_id == "opensanctions":
            continue
        for marker in hit.markers:
            if marker not in seen:
                seen.append(marker)
    return seen


def _summarize_foreign(hits: tuple[SourceHit, ...]) -> str:
    performed = [hit for hit in hits if hit.performed]
    if not performed:
        assert_clean(FOREIGN_NOT_PERFORMED)
        return FOREIGN_NOT_PERFORMED
    found = [hit for hit in performed if hit.found]
    if _sanctioned(found):
        # Санкционное совпадение важнее того, нашлась ли карточка компании.
        assert_clean(SANCTIONED)
        return SANCTIONED
    if not found:
        assert_clean(FOREIGN_NOT_FOUND)
        return FOREIGN_NOT_FOUND
    if any(_inactive(hit.status) for hit in found):
        assert_clean(FOREIGN_INACTIVE)
        return FOREIGN_INACTIVE
    if any(hit.name_match is False for hit in found):
        assert_clean(FOREIGN_NAME_MISMATCH)
        return FOREIGN_NAME_MISMATCH
    assert_clean(FOREIGN_FOUND)
    return FOREIGN_FOUND
