"""Корпус договоров: разные формулировки, валюты и структуры платежей.

Правила писались на паре образцов, и этого мало. Договоры ВЭД пишут
по-разному: организационно-правовую форму — словами и аббревиатурой, сумму —
в рублях, долларах и юанях, иностранную сторону — с Ltd, LLP, Pvt Ltd и GmbH.
Здесь по каждому варианту закреплено, что именно программа обязана извлечь
и как обязано повести себя пороговое правило.

Главное свойство: пороговое правило, которое не может сравнить сумму
с порогом, отправляет вопрос юристу, а не молчит. Молчание правила читается
как «порог не достигнут», и это худшая из возможных ошибок в таком отчёте.
"""

from __future__ import annotations

import pytest

from app.parsing.document import Document, SourceFormat
from app.rules.contract import ContractView
from app.rules.engine import FindingStatus, evaluate

THRESHOLD_RULES = ("THR-001", "THR-002", "TRV-002")


def view(lines: list[str]) -> ContractView:
    return ContractView.from_document(Document(SourceFormat.DOCX, lines))


def finding(lines: list[str], code: str):
    report = evaluate(view(lines), moment=None)
    return next(item for item in report.findings if item.code == code)


def parties(lines: list[str]):
    return [(item.name, item.inn, item.role) for item in view(lines).facts.parties]


HEAD_RU = (
    "Общество с ограниченной ответственностью «СпецИмпортСнабжейшен», "
    "ИНН 7701123456, именуемое в дальнейшем «Покупатель», и"
)


def usd_contract() -> list[str]:
    return [
        "ДОГОВОР ПОСТАВКИ № 21/26",
        HEAD_RU,
        "Shenzhen Silk Road Trading Co., Ltd, Китайская Народная Республика, "
        "именуемое в дальнейшем «Поставщик»,",
        "1.2. Общая сумма Договора составляет 300 000 долларов США.",
        "2.1. Оплата производится цифровой валютой USDT в сети TRON.",
        "2.2. Реквизиты: TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE.",
        "2.3. Оплата производится траншами по 32 000 долларов США согласно графику.",
    ]


class TestForeignCurrencyContract:
    """Договор в валюте — типовой случай ВЭД, а не исключение."""

    def test_rouble_amount_is_absent_but_foreign_is_known(self):
        contract = view(usd_contract())

        assert contract.contract_amount() is None
        foreign = contract.foreign_contract_amount()
        assert foreign is not None
        assert foreign.currency == "USD"
        assert foreign.value == 300_000

    @pytest.mark.parametrize("code", THRESHOLD_RULES)
    def test_threshold_rules_ask_the_lawyer_instead_of_going_silent(self, code):
        item = finding(usd_contract(), code)

        assert item.status is FindingStatus.NOT_AUTOMATED, (
            f"{code}: правило ушло в «{item.status.value}» — юрист прочитает это "
            "как «порог не достигнут»"
        )
        assert "иностранной валюте" in item.evidence
        assert "USD" in item.evidence

    def test_splitting_rule_also_names_the_currency(self):
        item = finding(usd_contract(), "THR-003")

        assert item.status is FindingStatus.NOT_AUTOMATED
        assert "USD" in item.evidence

    def test_yuan_contract_behaves_the_same(self):
        lines = [
            "Общество с ограниченной ответственностью «ВостокТрейд», ИНН 7702234567,",
            "1.2. Общая сумма Договора составляет 2 400 000 юаней.",
            "2.1. Оплата производится цифровой валютой USDT в сети TRON.",
        ]

        item = finding(lines, "THR-001")

        assert item.status is FindingStatus.NOT_AUTOMATED
        assert "CNY" in item.evidence

    def test_rouble_contract_still_compares_with_the_threshold(self):
        lines = [
            "1.2. Общая сумма Договора составляет 24 000 000 рублей.",
            "2.1. Оплата производится цифровой валютой USDT в сети TRON.",
        ]

        item = finding(lines, "THR-001")

        assert item.status is FindingStatus.FAILED
        assert "24 000 000" in item.evidence

    def test_small_rouble_contract_is_correctly_not_applicable(self):
        lines = ["1.2. Стоимость услуг составляет 95 000 рублей."]

        assert finding(lines, "THR-001").status is FindingStatus.NOT_APPLICABLE
        assert finding(lines, "THR-002").status is FindingStatus.NOT_APPLICABLE


class TestForeignLegalForms:
    """Иностранная сторона — половина смысла проверки контрагента."""

    @pytest.mark.parametrize(
        "name",
        [
            "Shenzhen Silk Road Trading Co., Ltd",
            "Istanbul Textile Ltd",
            "Almaty Trade LLP",
            "Mumbai Chemicals Pvt Ltd",
            "Singapore Metals Pte Ltd",
            "Dubai General Trading LLC",
            "Berlin Machinen GmbH",
            "Freelance Studio Inc",
            "Amsterdam Logistics B.V.",
            "Milano Tessuti S.p.A.",
            "London Trade PLC",
        ],
    )
    def test_company_is_extracted(self, name):
        lines = [HEAD_RU, f"{name}, именуемое в дальнейшем «Поставщик»,"]

        names = [item[0] for item in parties(lines)]

        assert any(name.rstrip(".") in item for item in names), (
            f"{name} не извлечено: иностранная сторона выпадет из проверки"
        )

    def test_role_comes_from_the_text_after_the_name(self):
        lines = [
            "Публичное акционерное общество «Северный комбинат», ИНН 7703345678, "
            "именуемое в дальнейшем «Покупатель», и",
            "Berlin Machinen GmbH, Федеративная Республика Германия, «Поставщик»,",
        ]

        rows = {name: role for name, _inn, role in parties(lines)}

        assert rows["Berlin Machinen GmbH"] == "поставщик"
        assert rows["Публичное акционерное общество «Северный комбинат»"] == "покупатель"


class TestWalletExtractionAcrossNetworks:
    def test_evm_address_is_found(self):
        lines = [
            "2.1. Оплата производится цифровой валютой USDC в сети Ethereum.",
            "2.2. Реквизиты: 0x742d35Cc6634C0532925a3b844Bc454e4438f44e.",
        ]

        wallets = view(lines).facts.wallet_addresses

        assert [item.network for item in wallets] == ["EVM"]

    def test_two_networks_in_one_contract(self):
        lines = [
            "2.2. Адрес в сети TRON: TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE.",
            "2.3. Адрес в сети Ethereum: 0x742d35Cc6634C0532925a3b844Bc454e4438f44e.",
        ]

        wallets = view(lines).facts.wallet_addresses

        assert {item.network for item in wallets} == {"TRON", "EVM"}


class TestClauseAtTheEndOfALongContract:
    def test_last_clause_is_still_read(self):
        lines = [
            "Общество с ограниченной ответственностью «ДлинныйДоговор», ИНН 7706678901,",
            "1.2. Общая сумма Договора составляет 55 000 000 рублей.",
            *[
                f"{index}.1. Приложение № {index}: порядок приёмки партии оформляется актом."
                for index in range(3, 83)
            ],
            "83.1. Стороны актуализируют реквизиты не позднее трёх рабочих дней "
            "с момента их изменения.",
        ]

        assert finding(lines, "TRV-003").status is FindingStatus.PASSED


class TestThresholdRulesNeverGoSilentOnAKnownAmount:
    """Сводное свойство по всем пороговым правилам корпуса."""

    @pytest.mark.parametrize(
        "amount_line,currency",
        [
            ("1.2. Общая сумма Договора составляет 300 000 долларов США.", "USD"),
            ("1.2. Общая сумма Договора составляет 2 400 000 юаней.", "CNY"),
            ("1.2. Общая сумма Договора составляет 250 000 евро.", "EUR"),
            ("1.2. Общая сумма Договора составляет 300 000 USDT.", "USDT"),
        ],
    )
    def test_known_foreign_amount_never_reads_as_below_threshold(
        self, amount_line, currency
    ):
        lines = [HEAD_RU, amount_line, "2.1. Оплата цифровой валютой USDT в сети TRON."]

        for code in THRESHOLD_RULES:
            item = finding(lines, code)
            assert item.status is not FindingStatus.NOT_APPLICABLE, (
                f"{code} при сумме в {currency} ушло в «не применимо»"
            )
            assert currency in item.evidence
