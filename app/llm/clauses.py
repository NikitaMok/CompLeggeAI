"""Разбор смысла оговорок, которые регулярки не ловят.

Вердикт «соответствует / нет» ставит матрица правил, не модель.
Модель выписывает из текста факты: стороны, агентский договор, депег,
недепозитарный адрес, дробление платежей. Номера статей не называет.
Цитата обязана быть фрагментом договора, иначе отбрасывается.

Длинный договор в одно обращение к модели не помещается. Раньше текст молча
обрезался на 24 000 знаках: модель видела начало договора, а отчёт выглядел
так, будто она прочитала весь. Теперь договор проходит окнами с перекрытием,
а покрытие (сколько знаков модель фактически видела) попадает в отчёт: юрист
должен знать, читала модель приложения или нет.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.core.config import get_settings
from app.llm.client import LlmReply, complete
from app.llm.tiers import ModelTier, classify
from app.parsing.extract import WalletAddress, extract_wallet_addresses
from app.rules.contract import ContractView
from app.rules.guardrail import CircumventionAttempt, assert_clean, inspect

# Оговорки с нестандартной формулировкой. Вердикт ставит предикат матрицы;
# модель только выписывает факт, если цитата есть в тексте договора.
_CLAUSE_TOPICS = (
    ("FTC-004", "агент, комиссионер, поверенный"),
    ("AST-003", "утрата привязки стейблкоина, делистинг"),
    ("AST-004", "иностранный цифровой инструмент"),
    ("ADR-004", "адрес не администрируемый депозитарием и отчётность в налоговую"),
    ("RTE-004", "предел отклонения курса и доплата"),
    ("TRV-003", "обязанность актуализировать реквизиты"),
    ("THR-003", "график платежей, который выглядит как дробление под порог"),
    ("RPT-004", "содействие в отчёте в налоговые органы"),
)

_MAX_CHARS = 24_000
# Перекрытие окон: оговорка не должна пропасть из-за того, что попала на стык.
_WINDOW_OVERLAP = 1_500
_ARTICLE_HINT = re.compile(
    r"(?:\d{2,3}-ФЗ|стать[яи]\s+\d+|ст\.\s*\d+\s*ч\.)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PartyNote:
    name: str
    inn: str | None
    role: str
    country: str | None = None
    registration_number: str | None = None


@dataclass(frozen=True)
class ClauseNote:
    code: str
    quote: str
    reading: str
    present: bool | None


@dataclass(frozen=True)
class Coverage:
    """Сколько текста договора модель фактически прочитала."""

    chars_total: int = 0
    chars_seen: int = 0
    windows: int = 0
    windows_answered: int = 0

    @property
    def complete(self) -> bool:
        return self.chars_total > 0 and self.chars_seen >= self.chars_total

    @property
    def share(self) -> float:
        if self.chars_total <= 0:
            return 0.0
        return min(1.0, self.chars_seen / self.chars_total)

    def summary(self) -> str:
        if self.chars_total <= 0:
            return ""
        if self.complete and self.windows_answered == self.windows:
            if self.windows > 1:
                return f"модель прочитала договор целиком, {self.windows} фрагментами"
            return "модель прочитала договор целиком"
        percent = int(self.share * 100)
        return (
            f"модель прочитала {percent}% текста договора "
            f"({self.chars_seen} из {self.chars_total} знаков, "
            f"фрагментов отвечено {self.windows_answered} из {self.windows}). "
            "Незаполненная оговорка в непрочитанной части ничего не доказывает"
        )

    def to_dict(self) -> dict:
        return {
            "chars_total": self.chars_total,
            "chars_seen": self.chars_seen,
            "windows": self.windows,
            "windows_answered": self.windows_answered,
            "complete": self.complete,
            "share": round(self.share, 4),
            "summary": self.summary(),
        }


@dataclass(frozen=True)
class ClauseAnalysis:
    available: bool
    model: str
    detail: str
    parties: tuple[PartyNote, ...] = ()
    notes: tuple[ClauseNote, ...] = ()
    wallets: tuple[WalletAddress, ...] = ()
    coverage: Coverage = field(default_factory=Coverage)
    tier: ModelTier | None = None

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "model": self.model,
            "detail": self.detail,
            "parties": [
                {
                    "name": item.name,
                    "inn": item.inn,
                    "role": item.role,
                    "country": item.country,
                    "registration_number": item.registration_number,
                }
                for item in self.parties
            ],
            "notes": [
                {
                    "code": item.code,
                    "quote": item.quote,
                    "reading": item.reading,
                    "present": item.present,
                }
                for item in self.notes
            ],
            "wallets": [
                {"value": item.value, "network": item.network} for item in self.wallets
            ],
            "coverage": self.coverage.to_dict(),
            "tier": self.tier.to_dict() if self.tier is not None else None,
        }


def _unavailable(detail: str, model: str = "", coverage: Coverage | None = None) -> ClauseAnalysis:
    assert_clean(detail)
    return ClauseAnalysis(
        available=False,
        model=model,
        detail=detail,
        coverage=coverage or Coverage(),
        tier=classify(model) if model else None,
    )


def _windows(text: str) -> list[str]:
    """Договор кусками по _MAX_CHARS с перекрытием.

    Число кусков ограничено настройкой: иначе договор на тысячу страниц
    превратит один прогон в часы работы модели.
    """
    if len(text) <= _MAX_CHARS:
        return [text]

    limit = max(1, get_settings().llm_max_windows)
    step = _MAX_CHARS - _WINDOW_OVERLAP
    chunks: list[str] = []
    start = 0
    while start < len(text) and len(chunks) < limit:
        chunks.append(text[start : start + _MAX_CHARS])
        start += step
    return chunks


def _covered_chars(text: str, windows: list[str]) -> int:
    if not windows:
        return 0
    if len(windows) == 1:
        return min(len(text), len(windows[0]))
    step = _MAX_CHARS - _WINDOW_OVERLAP
    last_start = step * (len(windows) - 1)
    return min(len(text), last_start + len(windows[-1]))


def _prompt(text: str, *, part: int = 1, parts: int = 1) -> str:
    topics = "\n".join(f"- {code}: {title}" for code, title in _CLAUSE_TOPICS)
    body = text if len(text) <= _MAX_CHARS else text[:_MAX_CHARS]
    part_line = (
        ""
        if parts <= 1
        else (
            f"Это фрагмент {part} из {parts}. Выписывай только то, "
            "что есть в этом фрагменте.\n"
        )
    )
    return (
        "Ниже текст внешнеторгового договора. Выпиши только то, что в нём есть.\n"
        f"{part_line}"
        "Не оценивай соответствие закону. Не называй статьи, части и пункты.\n"
        "Не предлагай формулировки договора и способы обойти требование.\n"
        "Если явления в тексте нет — present: false, quote и reading пустые.\n"
        "quote — дословный фрагмент договора, не пересказ.\n\n"
        "Верни JSON вида:\n"
        '{"parties":[{"name":"...","inn":null,"country":null,'
        '"registration_number":null,"role":"покупатель|поставщик|иное"}],'
        '"wallets":[{"value":"T... или 0x...","network":"TRON|EVM"}],'
        '"notes":[{"code":"FTC-004","present":true,"quote":"...","reading":"..."}]}\n\n'
        "Коды notes — только из списка:\n"
        f"{topics}\n\n"
        "Для parties: иностранного поставщика выпиши как в договоре "
        "(наименование, страна, регистрационный номер, если они есть в тексте). "
        "inn — только российский ИНН из текста, иначе null. "
        "Для wallets — только адреса, которые буквально есть в договоре.\n\n"
        "Текст договора:\n"
        f"{body}"
    )


def _grounded_quote(quote: str, contract: str) -> str:
    compact = " ".join(quote.split())
    if len(compact) < 8:
        return ""
    haystack = " ".join(contract.split())
    if compact in haystack:
        return compact
    return ""


def _clean_reading(reading: str) -> str:
    text = " ".join(reading.split())
    if not text:
        return ""
    if _ARTICLE_HINT.search(text):
        return ""
    if inspect(text):
        return ""
    assert_clean(text)
    return text[:500]


def _parse_parties(raw: object, contract: str) -> tuple[PartyNote, ...]:
    if not isinstance(raw, list):
        return ()
    haystack = contract.lower()
    parties: list[PartyNote] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name") or "").split())
        if len(name) < 3 or name.lower() not in haystack:
            continue
        inn_raw = str(item.get("inn") or "").strip()
        inn = inn_raw if inn_raw.isdigit() and inn_raw in contract else None
        role = str(item.get("role") or "иное").strip()[:40]
        country_raw = " ".join(str(item.get("country") or "").split())
        country = country_raw[:80] if country_raw and country_raw.lower() in haystack else None
        reg_raw = " ".join(str(item.get("registration_number") or "").split())
        registration_number = (
            reg_raw[:80] if reg_raw and reg_raw in contract else None
        )
        try:
            assert_clean(name)
            assert_clean(role)
            if country:
                assert_clean(country)
            if registration_number:
                assert_clean(registration_number)
        except CircumventionAttempt:
            continue
        parties.append(
            PartyNote(
                name=name,
                inn=inn,
                role=role,
                country=country,
                registration_number=registration_number,
            )
        )
    return tuple(parties)


def _parse_wallets(raw: object, contract: str) -> tuple[WalletAddress, ...]:
    if not isinstance(raw, list):
        return ()
    known = extract_wallet_addresses(contract)
    allowed = {item.value: item for item in known}
    wallets: list[WalletAddress] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            value = str(item.get("value") or "").strip()
        else:
            value = str(item or "").strip()
        if value not in allowed or value in seen:
            continue
        seen.add(value)
        wallets.append(allowed[value])
    return tuple(wallets)


def _parse_notes(raw: object, contract: str) -> tuple[ClauseNote, ...]:
    allowed = {code for code, _title in _CLAUSE_TOPICS}
    if not isinstance(raw, list):
        return ()
    notes: list[ClauseNote] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if code not in allowed or code in seen:
            continue
        seen.add(code)
        present_raw = item.get("present")
        present: bool | None
        if present_raw is True:
            present = True
        elif present_raw is False:
            present = False
        else:
            present = None
        quote = _grounded_quote(str(item.get("quote") or ""), contract)
        reading = _clean_reading(str(item.get("reading") or ""))
        if present is False:
            quote, reading = "", ""
        notes.append(ClauseNote(code=code, quote=quote, reading=reading, present=present))
    return tuple(notes)


def _parse_payload(reply: LlmReply, contract: str) -> dict | None:
    """JSON одного окна. None — окно не пригодилось."""
    if not reply.ok:
        return None
    try:
        payload = json.loads(reply.text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _note_weight(note: ClauseNote) -> tuple[int, int, int]:
    """Чем полнее запись, тем она предпочтительнее при склейке окон."""
    present_rank = {True: 2, None: 1, False: 0}[note.present]
    return (present_rank, 1 if note.quote else 0, 1 if note.reading else 0)


def _merge_notes(batches: list[tuple[ClauseNote, ...]]) -> tuple[ClauseNote, ...]:
    best: dict[str, ClauseNote] = {}
    order: list[str] = []
    for batch in batches:
        for note in batch:
            if note.code not in best:
                best[note.code] = note
                order.append(note.code)
                continue
            if _note_weight(note) > _note_weight(best[note.code]):
                best[note.code] = note
    return tuple(best[code] for code in order)


def _merge_parties(batches: list[tuple[PartyNote, ...]]) -> tuple[PartyNote, ...]:
    best: dict[str, PartyNote] = {}
    order: list[str] = []
    for batch in batches:
        for party in batch:
            key = party.name.casefold()
            current = best.get(key)
            if current is None:
                best[key] = party
                order.append(key)
                continue
            # Из двух записей об одной стороне берём ту, где больше реквизитов.
            filled = sum(
                1 for value in (party.inn, party.country, party.registration_number) if value
            )
            filled_current = sum(
                1
                for value in (current.inn, current.country, current.registration_number)
                if value
            )
            if filled > filled_current:
                best[key] = party
    return tuple(best[key] for key in order)


def _merge_wallets(batches: list[tuple[WalletAddress, ...]]) -> tuple[WalletAddress, ...]:
    seen: set[str] = set()
    merged: list[WalletAddress] = []
    for batch in batches:
        for wallet in batch:
            if wallet.value in seen:
                continue
            seen.add(wallet.value)
            merged.append(wallet)
    return tuple(merged)


def analyze_clauses(
    contract: ContractView,
    *,
    complete_fn=complete,
) -> ClauseAnalysis:
    text = contract.text
    windows = _windows(text)
    coverage_total = len(text)

    parties_batches: list[tuple[PartyNote, ...]] = []
    notes_batches: list[tuple[ClauseNote, ...]] = []
    wallets_batches: list[tuple[WalletAddress, ...]] = []

    answered: list[str] = []
    model = ""
    first_error = ""
    seen_windows: list[str] = []

    for number, window in enumerate(windows, start=1):
        reply = complete_fn(_prompt(window, part=number, parts=len(windows)))
        if not isinstance(reply, LlmReply):
            first_error = first_error or "локальная модель не ответила"
            continue
        model = model or reply.model
        if not reply.ok:
            first_error = first_error or (reply.error or "локальная модель не ответила")
            continue

        try:
            payload = _parse_payload(reply, window)
        except CircumventionAttempt:
            first_error = first_error or (
                "ответ модели отклонён: формулировка не проходит проверку "
                "части 2 статьи 30"
            )
            continue
        if payload is None:
            first_error = first_error or "модель вернула не JSON"
            continue

        try:
            parties_batches.append(_parse_parties(payload.get("parties"), text))
            notes_batches.append(_parse_notes(payload.get("notes"), text))
            wallets_batches.append(_parse_wallets(payload.get("wallets"), text))
        except CircumventionAttempt:
            first_error = first_error or (
                "ответ модели отклонён: формулировка не проходит проверку "
                "части 2 статьи 30"
            )
            continue

        answered.append(window)
        seen_windows.append(window)

    coverage = Coverage(
        chars_total=coverage_total,
        chars_seen=_covered_chars(text, seen_windows) if seen_windows else 0,
        windows=len(windows),
        windows_answered=len(answered),
    )

    if not answered:
        return _unavailable(first_error or "локальная модель не ответила", model, coverage)

    detail = "локальная модель разобрала оговорки, которые не ловятся регулярками"
    if not coverage.complete:
        detail = f"{detail}; {coverage.summary()}"
    assert_clean(detail)

    return ClauseAnalysis(
        available=True,
        model=model,
        detail=detail,
        parties=_merge_parties(parties_batches),
        notes=_merge_notes(notes_batches),
        wallets=_merge_wallets(wallets_batches),
        coverage=coverage,
        tier=classify(model),
    )
