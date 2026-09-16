"""Кого сервис считает лицом, названным в договоре.

Здесь закрыт баг, из-за которого из проверки пропадал иностранный поставщик.
В типовой шапке «ООО «Х», ИНН …, и компанией Y Co., Ltd» один ИНН попадал
в окно обоих наименований, обе стороны склеивались по этому ИНН, и вторая
исчезала — то есть исчезал ровно тот контрагент, ради которого сверка нужна.
"""

from __future__ import annotations

import pytest

from app.counterparty.service import review_counterparties
from app.parsing.extract import extract_party_mentions
from app.rules.contract import ContractView

HEADER = (
    "1.1. Настоящий Договор заключён между ООО «Уралимпорт» (Покупатель), "
    "резидентом Российской Федерации, ИНН 6659123456, и компанией "
    "Shenzhen Precision Machinery Co., Ltd (Поставщик), нерезидентом."
)


def _no_network(inn, name, client=None, **_kwargs):
    return ()


class TestInnLinking:
    def test_one_inn_is_linked_to_one_name(self):
        mentions = extract_party_mentions(HEADER)
        by_name = {item.name: item for item in mentions}

        assert by_name["ООО «Уралимпорт»"].inn == "6659123456"
        assert by_name["Shenzhen Precision Machinery Co., Ltd"].inn is None

    def test_foreign_supplier_is_not_swallowed(self):
        mentions = extract_party_mentions(HEADER)

        assert "Shenzhen Precision Machinery Co., Ltd" in {item.name for item in mentions}

    def test_inn_without_a_name_is_still_reported(self):
        mentions = extract_party_mentions("Реквизиты: ИНН 7701234567, расчётный счёт.")

        assert any(item.inn == "7701234567" and not item.name for item in mentions)

    def test_repeated_name_is_listed_once(self):
        text = HEADER + " " + HEADER
        names = [item.name for item in extract_party_mentions(text)]

        assert names.count("Shenzhen Precision Machinery Co., Ltd") == 1


class TestRoles:
    @pytest.mark.parametrize(
        ("fragment", "name", "role"),
        [
            ("Покупатель ООО «Уралимпорт» принимает товар", "ООО «Уралимпорт»", "покупатель"),
            (
                "цифровой депозитарий ООО «Депозитарий Урала» ведёт учёт",
                "ООО «Депозитарий Урала»",
                "цифровой депозитарий",
            ),
            ("эмитент Tether Limited выпускает токен", "Tether Limited", "эмитент цифровой валюты"),
            (
                "уполномоченный банк АО «Банк Расчётов» ставит контракт на учёт",
                "АО «Банк Расчётов»",
                "уполномоченный банк",
            ),
        ],
    )
    def test_role_is_read_from_the_surrounding_text(self, fragment, name, role):
        mentions = {item.name: item for item in extract_party_mentions(fragment)}

        assert name in mentions, list(mentions)
        assert mentions[name].role == role

    def test_unknown_role_is_not_invented(self):
        mentions = extract_party_mentions("Упоминается ООО «Ромашка» без указания роли.")

        assert mentions[0].role is None


class TestReviewOnTheFixtures:
    def test_supplier_is_checked_without_the_model(self, compliant_docx):
        contract = ContractView.from_file(compliant_docx)
        checks = review_counterparties(contract, lookup=_no_network)
        by_name = {item.name: item for item in checks}

        supplier = by_name["Shenzhen Precision Machinery Co., Ltd"]
        assert supplier.foreign is True
        assert supplier.role == "поставщик"

    def test_depositary_is_not_labelled_a_counterparty(self, compliant_docx):
        contract = ContractView.from_file(compliant_docx)
        checks = review_counterparties(contract, lookup=_no_network)
        by_name = {item.name: item for item in checks}

        assert by_name["ООО «Цифровой Депозитарий Урала»"].role == "цифровой депозитарий"
        assert by_name["Tether Limited"].role == "эмитент цифровой валюты"

    def test_party_without_inn_gets_one_explanation_not_three(self, compliant_docx):
        """Раньше ЕГРЮЛ, Rusprofile и СБИС писали по строке «нет ИНН» каждая."""
        contract = ContractView.from_file(compliant_docx)
        checks = review_counterparties(contract, lookup=_no_network)
        depositary = next(
            item for item in checks if item.name == "ООО «Цифровой Депозитарий Урала»"
        )

        assert depositary.inn is None
        assert [hit for hit in depositary.hits if not hit.performed] == []
        assert "российского ИНН" in depositary.summary

    def test_roles_reach_the_json(self, compliant_docx):
        contract = ContractView.from_file(compliant_docx)
        checks = review_counterparties(contract, lookup=_no_network)

        assert all("role" in item.to_dict() for item in checks)


class TestFormatDoesNotChangeWhoIsFound:
    """Один договор в DOCX и в PDF должен давать один и тот же состав лиц.

    Извлечение из PDF ставит перенос строки там, где в DOCX был пробел.
    Из-за этого «Tether Limited» находилось в DOCX и терялось в PDF того же
    договора: наименование разрывалось переносом, и регулярка его не видела.
    """

    def test_same_entities_from_docx_and_pdf(self, fixtures_dir):
        for base in ("contract_compliant", "contract_long"):
            from_docx = {
                item.name
                for item in extract_party_mentions(
                    ContractView.from_file(fixtures_dir / f"{base}.docx").text
                )
            }
            from_pdf = {
                item.name
                for item in extract_party_mentions(
                    ContractView.from_file(fixtures_dir / f"{base}.pdf").text
                )
            }
            assert from_docx == from_pdf, base

    def test_name_broken_by_a_line_break_is_still_found(self):
        text = "Оплата цифровой валютой, эмитент Tether\nLimited, в сети TRON."
        names = [item.name for item in extract_party_mentions(text)]

        assert "Tether Limited" in names

    def test_name_is_normalised_to_one_line(self):
        text = "Поставщик — Shenzhen Precision\nMachinery Co., Ltd (нерезидент)."
        names = [item.name for item in extract_party_mentions(text)]

        assert "Shenzhen Precision Machinery Co., Ltd" in names
        assert all("\n" not in name for name in names)


class TestPreambleLegalForms:
    """В преамбуле форму пишут словами, дальше по тексту — аббревиатурой.

    Если ловить только аббревиатуру, сторона уходит в заключение голым ИНН,
    и юрист не понимает, о ком речь.
    """

    def _names(self, text: str):
        from app.parsing.extract import extract_party_mentions

        return [(item.name, item.inn, item.role) for item in extract_party_mentions(text)]

    def test_spelled_out_limited_liability_company(self):
        text = (
            "Общество с ограниченной ответственностью «СпецИмпортСнабжейшен», "
            "ИНН 7701123456, именуемое в дальнейшем «Покупатель»"
        )

        rows = self._names(text)

        assert rows
        name, inn, role = rows[0]
        assert "СпецИмпортСнабжейшен" in name
        assert inn == "7701123456"
        assert role == "покупатель"

    def test_spelled_out_joint_stock_company(self):
        rows = self._names('Публичное акционерное общество «Ромашка», ИНН 7707083893')

        assert rows and "Ромашка" in rows[0][0]
        assert rows[0][1] == "7707083893"

    def test_abbreviation_still_works(self):
        rows = self._names('ООО «Уралимпорт», ИНН 6659123456')

        assert rows and rows[0][1] == "6659123456"
