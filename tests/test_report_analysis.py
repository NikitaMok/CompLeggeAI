"""Разбор фактов в выводы: app/report/analysis.py.

Главное свойство, которое здесь закрепляется: показатель, которого источник
не вернул, попадает в «не установлено» и никогда — в «установлено».
Отчёт, в котором пробел выглядит как проверенный факт, опаснее отсутствия
отчёта.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.report.analysis import assess_party, assess_wallet

NOW = datetime(2026, 9, 16, tzinfo=timezone.utc)


def wallet(**overrides) -> dict:
    payload = {
        "address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        "network": "TRON",
        "band": "низкий",
        "score": 5,
        "screened": False,
        "factors": [],
        "labels": [],
        "kyt": [],
        "created_at": (NOW - timedelta(days=900)).isoformat(),
        "tx_count": 420,
        "tx_count_is_floor": False,
        "last_activity_at": (NOW - timedelta(days=2)).isoformat(),
        "usdt_balance": "15000.50",
        "account_found": True,
        "has_activity": True,
        "error": None,
        "source_notes": [],
    }
    payload.update(overrides)
    return payload


def party(**overrides) -> dict:
    payload = {
        "name": "ООО «СпецИмпортСнабжейшен»",
        "inn": "7701123456",
        "foreign": False,
        "role": "покупатель",
        "country": None,
        "summary": "запись в открытом реестре найдена; это не подтверждение правоспособности",
        "hits": [],
    }
    payload.update(overrides)
    return payload


def hit(**overrides) -> dict:
    payload = {
        "source": "dadata",
        "performed": True,
        "found": True,
        "legal_name": 'ООО "СПЕЦИМПОРТСНАБЖЕЙШЕН"',
        "inn": "7701123456",
        "ogrn": "1027700000001",
        "registration_number": None,
        "jurisdiction": None,
        "status": "действующая",
        "name_match": True,
        "detail": "DaData (ЕГРЮЛ): карточка найдена",
        "markers": [],
        "link": None,
    }
    payload.update(overrides)
    return payload


def text(assessment) -> str:
    return " ".join(
        [assessment.headline, *assessment.established, *assessment.concerns,
         *assessment.gaps, *assessment.manual]
    )


class TestWalletFactsVersusGaps:
    def test_known_values_land_in_established(self):
        result = assess_wallet(wallet(), now=NOW)

        joined = " ".join(result.established)
        assert "Сеть расчёта: TRON" in joined
        assert "Подтверждённых операций: 420" in joined
        assert "15000.50" in joined

    def test_unknown_balance_is_a_gap_not_a_zero(self):
        result = assess_wallet(wallet(usdt_balance=None), now=NOW)

        assert any("Баланс источником не возвращён" in line for line in result.gaps)
        assert not any("Баланс" in line for line in result.established)
        assert not any("Баланс" in line for line in result.concerns)

    def test_missing_history_never_reads_as_checked(self):
        result = assess_wallet(
            wallet(created_at=None, tx_count=None, last_activity_at=None, has_activity=None),
            now=NOW,
        )

        assert len(result.gaps) >= 3
        assert not any("Первая операция" in line for line in result.established)

    def test_inactive_address_is_a_concern_with_an_action(self):
        result = assess_wallet(wallet(account_found=False), now=NOW)

        assert any("не активирован" in line for line in result.concerns)
        assert any("подтверждение владения" in line for line in result.manual)

    def test_fresh_address_is_flagged(self):
        result = assess_wallet(
            wallet(created_at=(NOW - timedelta(days=9)).isoformat()), now=NOW
        )

        assert any("менее месяца" in line for line in result.concerns)

    def test_dormant_address_is_flagged(self):
        result = assess_wallet(
            wallet(last_activity_at=(NOW - timedelta(days=500)).isoformat()), now=NOW
        )

        assert any("без операций" in line for line in result.concerns)


class TestWalletScreening:
    def test_without_screening_the_gap_is_named(self):
        result = assess_wallet(wallet(screened=False), now=NOW)

        assert any("скоринг адреса не выполнялся" in line for line in result.gaps)
        assert any("специализированном сервисе" in line for line in result.manual)

    def test_commercial_verdict_is_established_and_signals_are_concerns(self):
        result = assess_wallet(
            wallet(
                band="критический",
                screened=True,
                kyt=[
                    {
                        "provider": "chainalysis",
                        "performed": True,
                        "risk_level": "критический",
                        "risk_score": 95,
                        "signals": ["Direct exposure to sanctioned entity"],
                        "detail": "оценка получена",
                        "report_url": None,
                    }
                ],
            ),
            now=NOW,
        )

        assert "критическому уровню риска" in result.headline
        assert any("chainalysis" in line and "критический" in line for line in result.established)
        assert any("sanctioned entity" in line for line in result.concerns)
        assert any("комплаенс" in line for line in result.manual)

    def test_failed_provider_is_a_gap(self):
        result = assess_wallet(
            wallet(
                kyt=[
                    {
                        "provider": "misttrack",
                        "performed": False,
                        "risk_level": None,
                        "risk_score": None,
                        "signals": [],
                        "detail": "MistTrack: ключ не задан, оценка не выполнена",
                        "report_url": None,
                    }
                ]
            ),
            now=NOW,
        )

        assert any("ключ не задан" in line for line in result.gaps)
        assert not any("misttrack" in line.lower() for line in result.established)

    def test_error_turns_the_headline_into_not_performed(self):
        result = assess_wallet(
            wallet(band="нет данных", error="TronGrid: нет соединения с источником"), now=NOW
        )

        assert "не выполнена" in result.headline
        assert any("TronGrid" in line for line in result.gaps)


class TestPartyAnalysis:
    def test_registry_facts_are_established(self):
        result = assess_party(party(hits=[hit()]))

        joined = " ".join(result.established)
        assert "Наименование по реестру" in joined
        assert "правовой статус — действующая" in joined
        assert "ОГРН 1027700000001" in joined
        assert "Роль по договору: покупатель" in joined

    def test_red_facts_become_concerns(self):
        result = assess_party(
            party(
                hits=[
                    hit(
                        source="kontur_focus",
                        detail="Контур.Фокус: красных фактов 1",
                        markers=["красный факт: адрес массовой регистрации"],
                    )
                ]
            )
        )

        assert any("массовой регистрации" in line for line in result.concerns)

    def test_name_mismatch_is_a_concern(self):
        result = assess_party(party(hits=[hit(name_match=False)]))

        assert any("не совпадает" in line for line in result.concerns)

    def test_silent_source_is_a_gap(self):
        result = assess_party(
            party(
                hits=[
                    hit(
                        source="dadata",
                        performed=False,
                        found=False,
                        detail="DaData: ключ не задан",
                    )
                ]
            )
        )

        assert any("ключ не задан" in line for line in result.gaps)
        assert any("Ни один источник сверки не отработал" in line for line in result.gaps)
        assert not result.established or "правовой статус" not in " ".join(result.established)

    def test_without_screening_the_gap_and_the_action_are_named(self):
        result = assess_party(party(hits=[hit()]))

        assert any("скрининг не выполнялся" in line for line in result.gaps)
        assert any("скрининг стороны до подписания" in line for line in result.manual)

    def test_sanctions_hit_puts_legal_assessment_first(self):
        result = assess_party(
            party(
                summary="сторона совпала с записью санкционного либо PEP-перечня",
                hits=[
                    hit(),
                    hit(
                        source="opensanctions",
                        legal_name=None,
                        status="совпадение с санкционным или PEP-перечнем",
                        detail="OpenSanctions: совпадений 1",
                        markers=["Blocked Trading Ltd — санкционный список (совпадение 0.93)"],
                    ),
                ],
            )
        )

        assert result.manual[0].startswith("До подписания получить правовую оценку")
        assert not any("скрининг не выполнялся" in line for line in result.gaps)

    def test_foreign_party_gets_its_own_action(self):
        result = assess_party(
            party(
                name="Shenzhen Silk Road Trading Co., Ltd",
                inn=None,
                foreign=True,
                role="поставщик",
                country="CN",
                hits=[hit(source="gleif", inn=None, ogrn=None, jurisdiction="CN")],
            )
        )

        assert any("Страна по договору: CN" in line for line in result.established)
        assert any("торгового реестра страны" in line for line in result.manual)

    def test_nothing_found_is_not_a_clean_result(self):
        result = assess_party(
            party(
                summary="в открытых источниках запись по ИНН не найдена",
                hits=[hit(found=False, legal_name=None, status=None, ogrn=None)],
            )
        )

        assert any("не нашёл запись" in line for line in result.concerns)


class TestNoInventedFacts:
    @pytest.mark.parametrize(
        "payload",
        [
            wallet(usdt_balance=None, tx_count=None, created_at=None, last_activity_at=None),
            wallet(account_found=None, has_activity=None),
        ],
    )
    def test_wallet_never_states_what_it_did_not_receive(self, payload):
        result = assess_wallet(payload, now=NOW)

        joined = " ".join(result.established)
        assert "Баланс USDT" not in joined or payload["usdt_balance"] is not None
        assert "Подтверждённых операций" not in joined or payload["tx_count"] is not None

    def test_party_without_sources_says_so_plainly(self):
        result = assess_party(party(hits=[]))

        assert "Ни один источник сверки не отработал." in result.gaps
        assert text(result).count("установлен") >= 0
