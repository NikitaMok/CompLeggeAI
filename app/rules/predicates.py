"""Детерминированные проверки правил матрицы.

Каждая проверка зарегистрирована под именем, которое указывается в поле `check`
файла `config/rules.yaml`. Правила без зарегистрированной проверки остаются
на ручной оценке юриста.

Проверки сознательно строгие: формулировка, которая не читается однозначно,
считается отсутствующей. Ложное срабатывание стоит клиенту лишнего абзаца
в договоре, пропуск — отказа банка.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from app.rules.contract import ContractView

MANDATORY_CONTROL_THRESHOLD = Decimal(10_000_000)
BANK_REGISTRATION_THRESHOLD = Decimal(3_000_000)
TRAVEL_RULE_THRESHOLD = Decimal(60_000)


class Verdict(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"
    # проверка есть, но по тексту договора вывод сделать нельзя
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class Outcome:
    verdict: Verdict
    evidence: str = ""
    clauses: tuple[str, ...] = field(default_factory=tuple)


Predicate = Callable[[ContractView], Outcome]

_REGISTRY: dict[str, Predicate] = {}


def register(name: str) -> Callable[[Predicate], Predicate]:
    def decorator(function: Predicate) -> Predicate:
        if name in _REGISTRY:
            raise ValueError(f"проверка «{name}» уже зарегистрирована")
        _REGISTRY[name] = function
        return function

    return decorator


def get_predicate(name: str) -> Predicate | None:
    return _REGISTRY.get(name)


def registered_names() -> set[str]:
    return set(_REGISTRY)


def _passed(evidence: str, clauses: tuple[str, ...] = ()) -> Outcome:
    return Outcome(Verdict.PASSED, evidence, clauses)


def _failed(evidence: str, clauses: tuple[str, ...] = ()) -> Outcome:
    return Outcome(Verdict.FAILED, evidence, clauses)


def _skip(evidence: str) -> Outcome:
    return Outcome(Verdict.NOT_APPLICABLE, evidence)


def _unresolved(evidence: str, clauses: tuple[str, ...] = ()) -> Outcome:
    return Outcome(Verdict.UNRESOLVED, evidence, clauses)


def _clause_numbers(contract: ContractView, *patterns: str) -> tuple[str, ...]:
    return tuple(clause.number for clause in contract.matching_clauses(*patterns))


def _amount(value: Decimal) -> str:
    """Сумма с пробелами между разрядами. Знак рубля не входит в cp1251."""
    return f"{value:,.0f}".replace(",", " ") + " руб."


# --- A. Квалификация договора ---


@register("foreign_trade_qualified")
def foreign_trade_qualified(contract: ContractView) -> Outcome:
    facts = contract.facts
    missing = []
    if not facts.mentions_foreign_trade:
        missing.append("договор не назван внешнеторговым")
    if not facts.mentions_resident:
        missing.append("не указано, что сторона является резидентом")
    if not facts.mentions_non_resident:
        missing.append("не указано, что сторона является нерезидентом")

    if missing:
        return _failed("; ".join(missing))

    return _passed(
        "договор квалифицирован как внешнеторговый между резидентом и нерезидентом",
        _clause_numbers(contract, r"внешнеторгов"),
    )


@register("subject_matter_declared")
def subject_matter_declared(contract: ContractView) -> Outcome:
    patterns = (
        r"передач\w*\s+(товар|информаци|результат)",
        r"поставк\w*",
        r"куп\w*-?\s*продаж\w*",
        r"выполнени\w*\s+работ",
        r"оказани\w*\s+услуг",
    )
    if contract.has_any(*patterns):
        return _passed("предмет договора описан в терминах пункта 1 части 7 статьи 1")

    return _failed(
        "из договора не следует передача товаров, работ, услуг или "
        "результатов интеллектуальной деятельности"
    )


@register("parties_identified")
def parties_identified(contract: ContractView) -> Outcome:
    facts = contract.facts
    if not (facts.mentions_resident and facts.mentions_non_resident):
        return _failed("резидентство сторон из текста не определяется")

    jurisdiction = contract.has_any(
        r"зарегистрирован\w*", r"государств\w*\s+регистрации", r"Федерация", r"Республика"
    )
    identifier = bool(facts.inns) or contract.has_any(
        r"регистрационн\w+\s+номер", r"налогов\w+\s+номер"
    )

    if jurisdiction and identifier:
        return _passed("указаны юрисдикция и регистрационный либо налоговый номер сторон")

    absent = []
    if not jurisdiction:
        absent.append("государство регистрации")
    if not identifier:
        absent.append("регистрационный либо налоговый номер")
    return _failed("не указано: " + ", ".join(absent))


@register("cites_exemption")
def cites_exemption(contract: ContractView) -> Outcome:
    if contract.has_any(
        r"пункт\w*\s+1\s+част\w+\s+7\s+стать\w+\s+1",
        r"ст\.?\s*1\s*ч\.?\s*7\s*п\.?\s*1",
    ):
        return _passed("есть ссылка на пункт 1 части 7 статьи 1 № 282-ФЗ")

    return _failed("нет прямой ссылки на норму, разрешающую расчёты цифровой валютой")


@register("intermediary_capacity")
def intermediary_capacity(contract: ContractView) -> Outcome:
    if not contract.has_any(r"агент", r"комиссионер", r"поверенн"):
        return _skip("сторона не названа агентом, комиссионером или поверенным")

    if contract.has_any(r"в\s+интересах", r"по\s+договору"):
        return _passed(
            "указано, в чьих интересах действует посредник",
            _clause_numbers(contract, r"агент|комиссионер|поверенн"),
        )

    return _failed(
        "сторона названа посредником, но не указано, в чьих интересах "
        "и по какому договору"
    )


# --- B. Актив ---


@register("asset_identified")
def asset_identified(contract: ContractView) -> Outcome:
    facts = contract.facts
    absent = []
    if not facts.tickers:
        absent.append("тикер цифровой валюты")
    if not facts.networks:
        absent.append("сеть и стандарт токена")
    if not contract.has_any(r"эмитент"):
        absent.append("эмитент")

    if absent:
        return _failed("не указано: " + ", ".join(absent))

    return _passed(
        f"актив идентифицирован: {', '.join(facts.tickers)} в сети {', '.join(facts.networks)}"
    )


@register("payment_purpose_declared")
def payment_purpose_declared(contract: ContractView) -> Outcome:
    if contract.has_any(
        r"расчёт\w*\s+(производятся\s+)?цифров\w+\s+валют",
        r"оплата\s+производится\s+цифровой\s+валютой",
        r"в\s+качестве\s+средства\s+платежа",
        r"встречн\w+\s+предоставлени",
    ):
        return _passed("цифровая валюта заявлена как средство платежа по договору")

    return _failed(
        "не указано, что цифровая валюта используется как средство платежа "
        "по этому договору: депозитарию не на что опереться при зачислении"
    )


@register("depeg_and_delisting")
def depeg_and_delisting(contract: ContractView) -> Outcome:
    has_peg = contract.has_any(r"привязк", r"депег")
    has_event = contract.has_any(r"утрат", r"делистинг", r"исключ")
    if has_peg and has_event:
        return _passed("оговорены последствия утраты привязки и делистинга")

    return _failed(
        "не оговорены последствия утраты стейблкоином привязки к базовому активу "
        "и делистинга"
    )


@register("foreign_digital_instrument")
def foreign_digital_instrument(contract: ContractView) -> Outcome:
    named = contract.has_any(r"USDT", r"USDC", r"Tether", r"иностранн\w+\s+цифров")
    if not named:
        return _skip("иностранный цифровой инструмент в договоре не назван")

    if contract.has_any(
        r"иностранн\w+\s+цифров\w+\s+инструмент",
        r"применяются\s+положения,?\s+установленные\s+в\s+отношении\s+цифровых\s+валют",
    ):
        return _passed(
            "учтено, что к иностранному цифровому инструменту применяются "
            "правила о цифровых валютах"
        )

    return _failed(
        "актив является иностранным цифровым инструментом, но это в договоре "
        "не учтено"
    )


# --- C. Адреса и порядок расчётов ---


@register("identifier_address_used")
def identifier_address_used(contract: ContractView) -> Outcome:
    facts = contract.facts
    if facts.wallet_addresses:
        listed = ", ".join(address.value for address in facts.wallet_addresses)
        return _failed(
            f"в договоре указан адрес кошелька вместо адреса-идентификатора: {listed}",
            _clause_numbers(contract, r"T[1-9A-HJ-NP-Za-km-z]{33}|0x[a-fA-F0-9]{40}"),
        )

    if not facts.mentions_identifier_address:
        return _failed("адрес-идентификатор в договоре не упомянут")

    return _passed(
        "расчёты идут через адрес-идентификатор",
        _clause_numbers(contract, r"адрес\w*[\s-]*идентификатор"),
    )


@register("depositary_registry_reference")
def depositary_registry_reference(contract: ContractView) -> Outcome:
    if not contract.facts.mentions_depositary:
        return _failed("цифровой депозитарий не назван")

    if not contract.has_any(r"реестр\w*"):
        return _failed(
            "депозитарий назван, но нет реестровых данных для проверки его права "
            "на деятельность"
        )

    return _passed(
        "депозитарий назван с реестровыми данными. Публичный перечень цифровых "
        "депозитариев Банка России на дату проверки не опубликован "
        "(навигатор допуска — cbr.ru/admissionfinmarket/navigator/cd, "
        "обновление 28.08.2026; страница реестров cbr.ru/registries — 01.09.2026, "
        "списка цифровых депозитариев среди опубликованных нет; "
        "Положение Банка России от 27.08.2026 № 890-П и Указание от 27.08.2026 "
        "№ 7429-У — на регистрации в Минюсте; приём документов "
        "по процедуре «4010 Соискатели» — с даты вступления 890-П в силу; "
        "субъекты ЭПР вправе подать документы до 01.09.2027). "
        "Сверка записи в реестре не выполнялась",
        _clause_numbers(contract, r"депозитари", r"реестр"),
    )


@register("foreign_trade_settlement_basis")
def foreign_trade_settlement_basis(contract: ContractView) -> Outcome:
    if contract.has_any(r"лиц\w*\s+организующ\w+\s+обращен\w+\s+цифров"):
        return _passed(
            "расчёты через лицо, организующее обращение цифровых валют",
            _clause_numbers(contract, r"организующ\w+\s+обращен"),
        )

    if contract.facts.mentions_foreign_trade and contract.has_any(
        r"средств\w+\s+платеж",
        r"встречн\w+\s+предоставлен",
        r"оплат\w+\s+.{0,80}цифров\w+\s+валют",
        r"расчёты\s+цифровой\s+валютой",
    ):
        return _passed(
            "прямые расчёты: внешнеторговое основание зафиксировано "
            "(п. 2 ч. 1 ст. 30 № 282-ФЗ)",
            _clause_numbers(contract, r"внешнеторгов"),
        )

    return _failed(
        "расчёты не через лицо, организующее обращение, и нет явного "
        "внешнеторгового основания п. 2 ч. 1 ст. 30"
    )


@register("depositary_statement_right")
def depositary_statement_right(contract: ContractView) -> Outcome:
    if contract.has_all(r"выписк\w+", r"депозитари"):
        return _passed("предусмотрена выписка цифрового депозитария")

    return _failed("нет права требовать выписку цифрового депозитария")


@register("noncustodial_tax_reporting")
def noncustodial_tax_reporting(contract: ContractView) -> Outcome:
    if not contract.has_any(r"не\s+администриру\w*.{0,80}депозитари"):
        if contract.facts.mentions_depositary:
            return _skip(
                "расчёты через цифровой депозитарий: отчётность по статье 12.1 "
                "к этому адресу не привязана"
            )
        return _skip("недепозитарный адрес-идентификатор в договоре не назван")

    if contract.has_any(r"налогов", r"отч[её]т"):
        return _passed("оговорена отчётность в налоговые органы по недепозитарному адресу")

    return _failed(
        "используется адрес, не администрируемый депозитарием, без оговорки "
        "об отчётности в налоговые органы"
    )


# --- D. Курс и момент исполнения ---


@register("rate_source_fixed")
def rate_source_fixed(contract: ContractView) -> Outcome:
    has_source = contract.has_any(
        r"котировк\w*", r"Московск\w+\s+Бирж", r"Мосбирж", r"Bloomberg", r"курс\w*\s+Банка\s+России"
    )
    has_moment = contract.has_any(
        r"на\s+дату\s+списания", r"на\s+момент\s+списания", r"на\s+дату\s+платежа"
    )

    if has_source and has_moment:
        return _passed("зафиксированы источник котировки и момент её определения")

    absent = []
    if not has_source:
        absent.append("источник котировки")
    if not has_moment:
        absent.append("момент фиксации курса")
    return _failed("не зафиксировано: " + ", ".join(absent))


@register("record_moment_clause")
def record_moment_clause(contract: ContractView) -> Outcome:
    if not contract.facts.mentions_record_moment:
        return _failed(
            "момент исполнения не привязан к внесению записи в информационную "
            "систему: возможен спор при незавершённой транзакции"
        )

    return _passed(
        "исполнение привязано к внесению записи в информационную систему",
        _clause_numbers(contract, r"внесени\w*\s+запис"),
    )


@register("network_fees_allocated")
def network_fees_allocated(contract: ContractView) -> Outcome:
    if contract.has_all(r"комисси\w*", r"(информационн\w+\s+систем|сет)"):
        return _passed("расходы на комиссии информационной системы распределены")

    return _failed("не распределены расходы на комиссии информационной системы")


@register("rate_deviation_limit")
def rate_deviation_limit(contract: ContractView) -> Outcome:
    if contract.has_all(r"отклонен", r"курс") and contract.has_any(
        r"доплат", r"возврат", r"разниц"
    ):
        return _passed("установлен предел отклонения курса и порядок доплаты либо возврата")

    return _failed(
        "не установлен допустимый предел отклонения курса и порядок доплаты "
        "либо возврата разницы"
    )


# --- E. Реквизиты сторон ---


@register("party_details_present")
def party_details_present(contract: ContractView) -> Outcome:
    facts = contract.facts
    absent = []
    if not facts.inns and not contract.has_any(r"налогов\w+\s+номер"):
        absent.append("ИНН либо налоговый номер")
    if not contract.has_any(r"город|\bг\.\s*[А-ЯЁ]"):
        absent.append("город местонахождения")
    if not contract.has_any(r"государств\w*|Федерация|Республика"):
        absent.append("государство местонахождения")

    if absent:
        return _failed(
            "для сопровождения операции не хватает: " + ", ".join(absent)
        )

    return _passed("реквизиты сторон приведены в объёме статьи 7.2-1 № 115-ФЗ")


@register("party_details_above_travel_rule_threshold")
def party_details_above_travel_rule_threshold(contract: ContractView) -> Outcome:
    gate = _threshold_gate(contract, "60 000 руб.")
    if gate is not None:
        return gate
    amount = contract.contract_amount()
    if amount <= TRAVEL_RULE_THRESHOLD:
        return _skip(f"сумма {_amount(amount)} не превышает порог 60 000 руб.")

    return party_details_present(contract)


@register("details_update_obligation")
def details_update_obligation(contract: ContractView) -> Outcome:
    if contract.has_all(r"актуализ", r"реквизит"):
        return _passed("обязанность актуализировать реквизиты закреплена")

    return _failed(
        "нет обязанности актуализировать реквизиты и последствий "
        "непредоставления сведений"
    )


def _money(amount) -> str:
    """Сумма в иностранной валюте: «300 000 USD»."""
    value = f"{amount.value:,.0f}".replace(",", "\u00a0")
    return f"{value} {amount.currency}"


def _threshold_gate(contract: ContractView, threshold_name: str) -> Outcome | None:
    """Что делать пороговому правилу, когда рублёвой суммы в тексте нет.

    Договор ВЭД обычно номинирован в валюте. Раньше такое правило отвечало
    «сумма из текста не определена» и уходило в «не применимо», а юрист читал
    это как «порог не достигнут». Теперь правило называет валютную сумму
    и отправляет вопрос на оценку: пересчёт по курсу — не дело программы.
    """
    if contract.contract_amount() is not None:
        return None
    foreign = contract.foreign_contract_amount()
    if foreign is None:
        return _skip("сумма договора из текста не определена")
    return _unresolved(
        f"сумма договора выражена в иностранной валюте ({_money(foreign)}); "
        f"порог {threshold_name} применяется к рублёвому эквиваленту — "
        "оценить по курсу на дату платежа"
    )


# --- F. Пороги и контроль ---


@register("mandatory_control_readiness")
def mandatory_control_readiness(contract: ContractView) -> Outcome:
    gate = _threshold_gate(contract, "обязательного контроля 10 млн руб.")
    if gate is not None:
        return gate
    amount = contract.contract_amount()
    if amount < MANDATORY_CONTROL_THRESHOLD:
        return _skip(f"сумма {_amount(amount)} ниже порога обязательного контроля")

    if contract.has_any(r"подтверждающ\w+\s+документ"):
        return _passed(
            f"сумма {_amount(amount)} подпадает под обязательный контроль; "
            "обязанность предоставлять подтверждающие документы закреплена"
        )

    return _failed(
        f"сумма {_amount(amount)} подпадает под обязательный контроль по пункту 1.12 "
        "статьи 6 № 115-ФЗ, но обязанность предоставлять подтверждающие документы "
        "в договоре не закреплена"
    )


@register("bank_registration_clause")
def bank_registration_clause(contract: ContractView) -> Outcome:
    gate = _threshold_gate(contract, "постановки на учёт 3 млн руб.")
    if gate is not None:
        return gate
    amount = contract.contract_amount()
    if amount < BANK_REGISTRATION_THRESHOLD:
        return _skip(
            f"сумма {_amount(amount)} ниже порога постановки на учёт "
            "импортного контракта (п. 4.3 Инструкции № 181-И — 3 млн руб.)"
        )

    if contract.has_any(r"постановк\w*\s+на\s+учёт", r"на\s+учёт\s+в\s+уполномоченн\w+\s+банк"):
        return _passed(
            "постановка контракта на учёт в уполномоченном банке предусмотрена "
            "(п. 4.3, п. 5.1 Инструкции № 181-И)"
        )

    return _failed(
        f"сумма {_amount(amount)} не ниже порога постановки на учёт "
        "импортного контракта по пункту 4.3 Инструкции Банка России № 181-И, "
        "но в договоре это не оговорено"
    )


# Доля порога, ниже которой транш уже нельзя назвать «чуть ниже границы».
# Транш в 2,9 млн при пороге 3 млн — признак; транш в 200 тыс. — обычный график.
_SPLIT_NEAR = Decimal("0.75")


@register("no_payment_splitting")
def no_payment_splitting(contract: ContractView) -> Outcome:
    """Искусственное дробление платежей под порог.

    Считаются именно платежи, а не различные суммы: четыре транша по 2,9 млн
    при пороге 3 млн — классическая схема, и раньше она проходила мимо
    правила, потому что сумма транша одна и та же.

    Пороги проверяются от меньшего к большему: транш, подогнанный под 3 млн,
    говорит больше, чем факт «сумма ниже 10 млн».
    """
    amount = contract.contract_amount()
    payments = [
        item.value
        for item in contract.facts.amounts
        if item.currency == "RUB" and (amount is None or item.value != amount)
    ]
    has_schedule = contract.has_any(
        r"транш", r"поэтапн", r"график\s+платеж", r"платеж\w*\s+частями", r"частями"
    )

    if amount is None:
        foreign = contract.foreign_contract_amount()
        if foreign is not None:
            return _unresolved(
                f"сумма договора выражена в иностранной валюте ({_money(foreign)}); "
                "пороги установлены в рублёвом эквиваленте — оценить график "
                "платежей по курсу на дату платежа"
            )
        if has_schedule:
            return _unresolved(
                "в договоре указан график платежей, но общая сумма из текста "
                "не определяется: вывод о дроблении сделать нельзя"
            )
        return _passed("признаков искусственного дробления платежей в тексте не видно")

    for threshold in (BANK_REGISTRATION_THRESHOLD, MANDATORY_CONTROL_THRESHOLD):
        if amount < threshold:
            continue
        below = [value for value in payments if value < threshold]
        near = [value for value in below if value >= threshold * _SPLIT_NEAR]
        if len(near) >= 2:
            largest = max(near)
            return _failed(
                f"платежей ниже порога {_amount(threshold)}: {len(near)}, "
                f"наибольший — {_amount(largest)} при общей сумме {_amount(amount)}"
            )
        if len(below) >= 2 and len({value for value in below}) >= 2:
            return _unresolved(
                f"в договоре несколько платежей ниже порога {_amount(threshold)} "
                f"при общей сумме {_amount(amount)}: оценить, не является ли "
                "график дроблением"
            )

    if has_schedule:
        return _unresolved(
            "в договоре указан график или оплата частями; суммы траншей "
            "из текста не выделены, вывод о дроблении сделать нельзя"
        )

    return _passed("признаков искусственного дробления платежей в тексте не видно")


# --- G. Риски и AML ---


@register("risk_allocation_clause")
def risk_allocation_clause(contract: ContractView) -> Outcome:
    if contract.has_all(r"риск", r"(заморозк|отклонени|приостановлени|блокировк)"):
        return _passed(
            "риски заморозки и отклонения операции распределены",
            _clause_numbers(contract, r"риск"),
        )

    return _failed("не распределены риски заморозки или отклонения операции")


@register("suspension_right")
def suspension_right(contract: ContractView) -> Outcome:
    if contract.has_any(r"приостанов\w+\s+исполнени", r"вправе\s+приостанов"):
        return _passed("предусмотрено право приостановить исполнение")

    return _failed("нет права приостановить исполнение при выявлении риска")


@register("origin_representation")
def origin_representation(contract: ContractView) -> Outcome:
    if contract.has_all(r"заверя\w*", r"(происхожден|не\s+связан)"):
        return _passed("контрагент заверяет о происхождении цифровой валюты")

    return _failed("нет заверения о происхождении цифровой валюты")


@register("fatf_jurisdiction_addressed")
def fatf_jurisdiction_addressed(contract: ContractView) -> Outcome:
    from app.aml.fatf import get_fatf_snapshot

    snapshot = get_fatf_snapshot()
    disclosure = contract.has_any(
        r"ФАТФ",
        r"не\s+выполня\w*\s+рекомендац",
        r"обязательн\w+\s+контрол\w+\s+независимо\s+от\s+сумм",
    )
    if disclosure:
        return _passed(
            "юрисдикция площадки и режим обязательного контроля по ФАТФ оговорены",
            _clause_numbers(contract, r"ФАТФ")
            or _clause_numbers(contract, r"не\s+выполня\w*\s+рекомендац"),
        )

    matched = snapshot.match_in(contract.text)
    if matched:
        names = ", ".join(item.names[0] for item in matched)
        in_order = [item for item in matched if item.in_rf_order_361]
        only_fatf = [item for item in matched if not item.in_rf_order_361]
        parts = [
            "в договоре названо государство из снимка перечня ФАТФ "
            f"({snapshot.as_of}): {names}"
        ]
        if in_order:
            parts.append(
                "совпадает с приказом Росфинмониторинга от 10.11.2011 № 361: "
                + ", ".join(item.names[0] for item in in_order)
            )
        if only_fatf:
            parts.append(
                "в приказе Росфинмониторинга № 361 отсутствует: "
                + ", ".join(item.names[0] for item in only_fatf)
            )
        parts.append("нет оговорки об обязательном контроле независимо от суммы")
        return _failed(
            "; ".join(parts),
            _clause_numbers(contract, re.escape(matched[0].names[0])),
        )

    return _unresolved(
        "юрисдикция организации, администрирующей адрес-идентификатор, "
        f"в договоре не названа. Снимок перечня ФАТФ от {snapshot.as_of} "
        "подменяет официальный перечень Правительства РФ, пока тот не опубликован. "
        "Приказ Росфинмониторинга от 10.11.2011 № 361 — Иран и КНДР, без Мьянмы"
    )


# --- H. Документооборот ---


@register("supporting_documents_obligation")
def supporting_documents_obligation(contract: ContractView) -> Outcome:
    if contract.has_any(r"подтверждающ\w+\s+документ"):
        return _passed("обязанность предоставлять подтверждающие документы закреплена")

    return _failed("нет обязанности предоставлять подтверждающие документы")


@register("retention_period")
def retention_period(contract: ContractView) -> Outcome:
    if contract.has_all(r"(пяти|5)\s+лет", r"хран"):
        return _passed("срок хранения документов — не менее пяти лет")

    return _failed("не установлен срок хранения документов по операциям")


@register("tax_report_assistance")
def tax_report_assistance(contract: ContractView) -> Outcome:
    if not contract.has_any(r"не\s+администриру\w*.{0,80}депозитари"):
        if contract.facts.mentions_depositary:
            return _skip(
                "расчёты через депозитарий: содействие в отчёте по статье 12.1 "
                "этим правилом не требуется"
            )
        return _skip("недепозитарный адрес в договоре не назван")

    if contract.has_any(r"отч[её]т\w*\s+в\s+налогов", r"налогов\w+\s+орган"):
        return _passed("предусмотрено содействие в подготовке отчёта в налоговые органы")

    return _failed(
        "при расчётах через недепозитарный адрес нет оговорки о содействии "
        "в отчёте в налоговые органы"
    )
