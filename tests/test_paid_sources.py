"""Коннекторы по ключам клиента: DaData, Контур.Фокус, OpenSanctions,
MistTrack, Chainalysis, AMLBot.

Проверяется одно и то же для каждого: разбор нормального ответа, поведение
при отказе и главное свойство — источник, который не отработал, никогда
не превращается в «проверено» и не красит сторону или адрес зелёным.
"""

from __future__ import annotations

import httpx
import pytest

from app.aml import kyt
from app.aml.score import AddressSnapshot, score_snapshot
from app.counterparty import ru, sanctions
from app.counterparty.models import SANCTIONED, SourceHit, summarize
from app.core.config import get_settings


@pytest.fixture
def keys():
    """Ключи задаются на объекте настроек: он кэширован на весь прогон."""
    settings = get_settings()
    saved = {
        name: getattr(settings, name)
        for name in (
            "dadata_api_key",
            "kontur_focus_key",
            "opensanctions_api_key",
            "misttrack_api_key",
            "chainalysis_api_key",
            "amlbot_api_key",
            "amlbot_api_url",
        )
    }

    def apply(**values):
        for name, value in values.items():
            setattr(settings, name, value)

    yield apply
    for name, value in saved.items():
        setattr(settings, name, value)


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------- DaData


DADATA_OK = {
    "suggestions": [
        {
            "value": 'ООО "УРАЛИМПОРТ"',
            "data": {
                "inn": "6659123456",
                "ogrn": "1026600000001",
                "name": {"full_with_opf": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "УРАЛИМПОРТ"'},
                "state": {"status": "ACTIVE"},
                "management": {"name": "Иванов Иван Иванович", "post": "ДИРЕКТОР"},
                "okved": "46.90",
                "address": {"value": "620000, Свердловская обл, г Екатеринбург"},
            },
        }
    ]
}


class TestDaData:
    def test_without_key_says_where_to_get_it(self, keys):
        keys(dadata_api_key="")
        with client(lambda request: httpx.Response(500)) as session:
            hit = ru.dadata("6659123456", "ООО «Уралимпорт»", session)

        assert not hit.performed
        assert "dadata.ru" in hit.detail
        assert "не выполнена" in hit.detail

    def test_parses_active_card(self, keys):
        keys(dadata_api_key="test-token")
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization", "")
            seen["path"] = request.url.path
            return httpx.Response(200, json=DADATA_OK)

        with client(handler) as session:
            hit = ru.dadata("6659123456", "ООО «Уралимпорт»", session)

        assert seen["auth"] == "Token test-token"
        assert seen["path"].endswith("/findById/party")
        assert hit.performed and hit.found
        assert hit.status == "действующая"
        assert hit.name_match is True
        assert hit.ogrn == "1026600000001"
        assert "Иванов" in hit.detail
        assert hit.markers == ()

    def test_liquidating_is_a_marker_and_changes_the_summary(self, keys):
        keys(dadata_api_key="test-token")
        payload = {
            "suggestions": [
                {
                    "value": "ООО «Тень»",
                    "data": {
                        "inn": "6659123456",
                        "name": {"full_with_opf": "ООО «Тень»"},
                        "state": {"status": "LIQUIDATING"},
                    },
                }
            ]
        }
        with client(lambda request: httpx.Response(200, json=payload)) as session:
            hit = ru.dadata("6659123456", "ООО «Тень»", session)

        assert hit.status == "в процессе ликвидации"
        assert hit.markers
        text = summarize((hit,), foreign=False, has_inn=True)
        assert "ликвидирована либо исключена" in text

    def test_empty_answer_is_not_found_not_error(self, keys):
        keys(dadata_api_key="test-token")
        with client(lambda request: httpx.Response(200, json={"suggestions": []})) as session:
            hit = ru.dadata("6659123456", "ООО «Уралимпорт»", session)

        assert hit.performed
        assert not hit.found
        assert "записи по этому ИНН" in hit.detail

    @pytest.mark.parametrize(
        "code,marker",
        [(401, "отклонила ключ"), (429, "лимит"), (503, "отказом")],
    )
    def test_refusals_are_not_performed(self, keys, code, marker):
        keys(dadata_api_key="test-token")
        with client(lambda request: httpx.Response(code, text="no")) as session:
            hit = ru.dadata("6659123456", "ООО «Уралимпорт»", session)

        assert not hit.performed
        assert marker in hit.detail

    def test_broken_json_is_not_performed(self, keys):
        keys(dadata_api_key="test-token")
        with client(lambda request: httpx.Response(200, text="<html>")) as session:
            hit = ru.dadata("6659123456", "ООО «Уралимпорт»", session)

        assert not hit.performed
        assert "неразбираемый" in hit.detail


# ------------------------------------------------------------- Контур.Фокус


class TestKonturFocus:
    def test_without_key_is_not_performed(self, keys):
        keys(kontur_focus_key="")
        with client(lambda request: httpx.Response(500)) as session:
            hit = ru.kontur_focus("6659123456", "ООО «Уралимпорт»", session)

        assert not hit.performed
        assert "ключ не задан" in hit.detail

    def test_reads_requisites_and_red_facts(self, keys):
        keys(kontur_focus_key="focus-key")
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            assert request.url.params.get("key") == "focus-key"
            if request.url.path.endswith("/req"):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "inn": "6659123456",
                            "ogrn": "1026600000001",
                            "focusHref": "https://focus.kontur.ru/entity?query=6659123456",
                            "UL": {
                                "legalName": {"full": 'ООО "УРАЛИМПОРТ"'},
                                "status": {"statusString": "Действующее"},
                            },
                        }
                    ],
                )
            return httpx.Response(
                200,
                json=[
                    {
                        "redStatements": [{"text": "адрес массовой регистрации"}],
                        "yellowStatements": ["судебные дела как ответчик"],
                    }
                ],
            )

        with client(handler) as session:
            hit = ru.kontur_focus("6659123456", "ООО «Уралимпорт»", session)

        assert any(path.endswith("/req") for path in calls)
        assert any(path.endswith("/briefReport") for path in calls)
        assert hit.performed and hit.found
        assert hit.status == "Действующее"
        assert hit.link and "focus.kontur.ru" in hit.link
        assert any("массовой регистрации" in marker for marker in hit.markers)

        text = summarize((hit,), foreign=False, has_inn=True)
        assert "тревожные признаки" in text

    def test_brief_report_failure_keeps_requisites(self, keys):
        keys(kontur_focus_key="focus-key")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/req"):
                return httpx.Response(
                    200,
                    json=[{"inn": "6659123456", "UL": {"legalName": {"short": "ООО «Уралимпорт»"}}}],
                )
            return httpx.Response(500, text="boom")

        with client(handler) as session:
            hit = ru.kontur_focus("6659123456", "ООО «Уралимпорт»", session)

        assert hit.performed and hit.found
        assert "экспресс-отчёт не получен" in hit.detail

    def test_rejected_key_is_not_performed(self, keys):
        keys(kontur_focus_key="focus-key")
        with client(lambda request: httpx.Response(403, text="no")) as session:
            hit = ru.kontur_focus("6659123456", "ООО «Уралимпорт»", session)

        assert not hit.performed
        assert "отклонил ключ" in hit.detail


# ------------------------------------------------------------ OpenSanctions


def _yente(results: list[dict]) -> dict:
    return {"responses": {"party": {"status": 200, "results": results, "total": {"value": len(results)}}}}


class TestSanctionsScreening:
    def test_clean_name_is_screened_and_not_found(self):
        with client(lambda request: httpx.Response(200, json=_yente([]))) as session:
            hit = sanctions.screen("Shenzhen Trading Co., Ltd", session)

        assert hit.performed
        assert not hit.found
        assert "совпадений" in hit.detail

    def test_match_above_threshold_is_reported(self):
        payload = _yente(
            [
                {
                    "id": "NK-123",
                    "caption": "Blocked Trading Ltd",
                    "score": 0.93,
                    "match": True,
                    "datasets": ["us_ofac_sdn"],
                    "properties": {"topics": ["sanction"]},
                }
            ]
        )
        with client(lambda request: httpx.Response(200, json=payload)) as session:
            hit = sanctions.screen("Blocked Trading Ltd", session)

        assert hit.performed and hit.found
        assert any("санкционный список" in marker for marker in hit.markers)
        assert summarize((hit,), foreign=True, has_inn=False) == SANCTIONED

    def test_weak_match_is_not_a_hit(self):
        payload = _yente(
            [{"id": "NK-9", "caption": "Other Co", "score": 0.30, "properties": {}}]
        )
        with client(lambda request: httpx.Response(200, json=payload)) as session:
            hit = sanctions.screen("Shenzhen Trading Co., Ltd", session)

        assert hit.performed
        assert not hit.found

    def test_cloud_key_goes_into_the_header(self, keys):
        keys(opensanctions_api_key="os-key")
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, json=_yente([]))

        with client(handler) as session:
            sanctions.screen("Shenzhen Trading Co., Ltd", session)

        assert seen["auth"] == "ApiKey os-key"

    def test_server_down_is_named_plainly(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with client(handler) as session:
            hit = sanctions.screen("Shenzhen Trading Co., Ltd", session)

        assert not hit.performed
        assert "yente" in hit.detail
        assert "не выполнен" in hit.detail

    def test_short_name_is_skipped(self):
        with client(lambda request: httpx.Response(200, json=_yente([]))) as session:
            hit = sanctions.screen("АБ", session)

        assert not hit.performed


# ----------------------------------------------------------------- MistTrack


class TestMistTrack:
    def test_without_key_is_not_performed(self, keys):
        keys(misttrack_api_key="")
        with client(lambda request: httpx.Response(500)) as session:
            verdict = kyt.misttrack("TR7NHq", "TRON", session)

        assert not verdict.performed
        assert "ключ не задан" in verdict.detail

    def test_parses_score_and_signals(self, keys):
        keys(misttrack_api_key="mt-key")
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["coin"] = request.url.params.get("coin", "")
            seen["key"] = request.url.params.get("api_key", "")
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "score": 82,
                        "risk_level": "High",
                        "hacking_event": "Bybit Exploit",
                        "detail_list": ["Malicious Address"],
                        "risk_detail": [
                            {"entity": "Sanctioned Entity", "risk_type": "Sanctioned", "hop_num": 2}
                        ],
                        "risk_report_url": "https://misttrack.io/report/1",
                    },
                },
            )

        with client(handler) as session:
            verdict = kyt.misttrack("TR7NHq", "TRON", session)

        assert seen["coin"] == "USDT-TRC20"
        assert seen["key"] == "mt-key"
        assert verdict.performed
        assert verdict.risk_score == 82
        assert verdict.risk_level == kyt.HIGH
        assert any("Bybit" in signal for signal in verdict.signals)
        assert verdict.report_url

    def test_unsuccessful_answer_is_not_a_verdict(self, keys):
        keys(misttrack_api_key="mt-key")
        payload = {"success": False, "msg": "invalid api_key"}
        with client(lambda request: httpx.Response(200, json=payload)) as session:
            verdict = kyt.misttrack("TR7NHq", "TRON", session)

        assert not verdict.performed
        assert "invalid api_key" in verdict.detail

    def test_unknown_network_is_refused_before_the_request(self, keys):
        keys(misttrack_api_key="mt-key")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("запрос не должен уходить")

        with client(handler) as session:
            verdict = kyt.misttrack("addr", "SOLANA", session)

        assert not verdict.performed


# --------------------------------------------------------------- Chainalysis


class TestChainalysis:
    def test_registers_then_reads(self, keys):
        keys(chainalysis_api_key="ca-key")
        calls: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            assert request.headers.get("token") == "ca-key"
            if request.method == "POST":
                return httpx.Response(201, json={})
            return httpx.Response(
                200,
                json={
                    "address": "TR7NHq",
                    "risk": "Severe",
                    "riskReason": "Direct exposure to sanctioned entity",
                    "cluster": {"name": "Garantex", "category": "sanctioned entity"},
                    "addressIdentifications": [{"name": "OFAC SDN"}],
                },
            )

        with client(handler) as session:
            verdict = kyt.chainalysis("TR7NHq", "TRON", session)

        assert [method for method, _ in calls] == ["POST", "GET"]
        assert verdict.performed
        assert verdict.risk_level == kyt.SEVERE
        assert any("Garantex" in signal for signal in verdict.signals)

    def test_known_address_conflict_is_not_an_error(self, keys):
        keys(chainalysis_api_key="ca-key")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(409, json={"message": "already registered"})
            return httpx.Response(200, json={"risk": "Low"})

        with client(handler) as session:
            verdict = kyt.chainalysis("TR7NHq", "TRON", session)

        assert verdict.performed
        assert verdict.risk_level == kyt.LOW

    def test_rejected_key_is_not_performed(self, keys):
        keys(chainalysis_api_key="ca-key")
        with client(lambda request: httpx.Response(401, json={})) as session:
            verdict = kyt.chainalysis("TR7NHq", "TRON", session)

        assert not verdict.performed
        assert "отклонил ключ" in verdict.detail


# -------------------------------------------------------------------- AMLBot


class TestAmlBot:
    def test_key_without_url_says_what_is_missing(self, keys):
        keys(amlbot_api_key="amlbot-key", amlbot_api_url="")
        with client(lambda request: httpx.Response(500)) as session:
            verdict = kyt.amlbot("TR7NHq", "TRON", session)

        assert not verdict.performed
        assert "AMLBOT_API_URL" in verdict.detail

    def test_template_is_filled_and_score_parsed(self, keys):
        keys(
            amlbot_api_key="amlbot-key",
            amlbot_api_url="https://extrnl.amlbot.com/check?address={address}&asset={asset}",
        )
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(
                200,
                json={"data": {"riskscore": 0.81, "signals": [{"name": "darknet", "share": 0.4}]}},
            )

        with client(handler) as session:
            verdict = kyt.amlbot("TR7NHq", "TRON", session)

        assert "address=TR7NHq" in seen["url"]
        assert "asset=USDT-TRC20" in seen["url"]
        assert verdict.performed
        assert verdict.risk_level == kyt.SEVERE
        assert any("darknet" in signal for signal in verdict.signals)

    def test_wrong_path_points_at_the_setting(self, keys):
        keys(amlbot_api_key="amlbot-key", amlbot_api_url="https://extrnl.amlbot.com/nope")
        with client(lambda request: httpx.Response(404, text="")) as session:
            verdict = kyt.amlbot("TR7NHq", "TRON", session)

        assert not verdict.performed
        assert "AMLBOT_API_URL" in verdict.detail


# ------------------------------------------- как скоринг влияет на заключение


class TestScoreWithCommercialVerdicts:
    def _snapshot(self) -> AddressSnapshot:
        return AddressSnapshot(
            address="TR7NHq",
            network="TRON",
            tx_count=5000,
            account_found=True,
            has_activity=True,
        )

    def test_without_any_screening_the_report_says_so(self):
        score = score_snapshot(
            self._snapshot(),
            kyt=(kyt.KytVerdict(provider="misttrack", performed=False, detail="ключ не задан"),),
        )

        assert any("только на открытых данных" in factor for factor in score.factors)
        assert not score.screened

    def test_commercial_verdict_raises_the_band(self):
        score = score_snapshot(
            self._snapshot(),
            kyt=(
                kyt.KytVerdict(
                    provider="chainalysis",
                    performed=True,
                    risk_level=kyt.SEVERE,
                    signals=("Direct exposure to sanctioned entity",),
                ),
            ),
        )

        assert score.band == "критический"
        assert score.screened
        assert score.factors[0].startswith("chainalysis")

    def test_low_commercial_verdict_does_not_whitewash_open_data(self):
        snapshot = AddressSnapshot(
            address="TR7NHq",
            network="TRON",
            account_found=False,
            risk_labels=("фишинг",),
        )
        score = score_snapshot(
            snapshot,
            kyt=(kyt.KytVerdict(provider="misttrack", performed=True, risk_level=kyt.LOW),),
        )

        assert score.band == "высокий"

    def test_worst_of_several_wins(self):
        verdicts = (
            kyt.KytVerdict(provider="misttrack", performed=True, risk_level=kyt.LOW),
            kyt.KytVerdict(provider="amlbot", performed=True, risk_level=kyt.HIGH),
            kyt.KytVerdict(provider="chainalysis", performed=False),
        )

        assert kyt.worst(verdicts).provider == "amlbot"


class TestNothingBecomesFine:
    """Сводное свойство: неотработавший источник не делает сторону чистой."""

    @pytest.mark.parametrize(
        "hit",
        [
            SourceHit(source_id="dadata", performed=False, found=False, detail="ключ не задан"),
            SourceHit(source_id="kontur_focus", performed=False, found=False, detail="отказ"),
            SourceHit(source_id="opensanctions", performed=False, found=False, detail="нет сервера"),
        ],
    )
    def test_failed_source_never_reads_as_checked(self, hit):
        text = summarize((hit,), foreign=False, has_inn=True)

        assert "не выполнена" in text
        assert "порядке" not in text


# ------------------------------------------ модель как источник сведений о сторонах


class TestModelDrivenParties:
    """Модель читает договор и называет стороны; проверка остаётся детерминированной."""

    def _contract(self):
        from app.parsing.document import Document, SourceFormat
        from app.rules.contract import ContractView

        text = [
            "ДОГОВОР ПОСТАВКИ",
            "Покупатель: ООО «Уралимпорт», ИНН 6659123456.",
            "Поставщик: Shenzhen Silk Road Trading Co., Ltd, Китайская Народная Республика.",
        ]
        return ContractView.from_document(Document(SourceFormat.DOCX, text))

    def test_country_from_the_model_reaches_the_screening(self):
        from app.counterparty.service import review_counterparties
        from app.llm.clauses import PartyNote

        seen: list[dict] = []

        def lookup(inn, name, client=None, **kwargs):
            seen.append({"name": name, **kwargs})
            return ()

        review_counterparties(
            self._contract(),
            llm_parties=(
                PartyNote(
                    name="Shenzhen Silk Road Trading Co., Ltd",
                    inn=None,
                    role="поставщик",
                    country="CN",
                ),
            ),
            lookup=lookup,
        )

        foreign = [row for row in seen if "Shenzhen" in row["name"]]
        assert foreign, "иностранная сторона не дошла до сверки"
        assert foreign[0]["country"] == "CN"
        assert foreign[0]["foreign"] is True

    def test_country_alone_marks_a_party_as_foreign(self):
        from app.counterparty.service import review_counterparties
        from app.llm.clauses import PartyNote

        seen: list[dict] = []

        def lookup(inn, name, client=None, **kwargs):
            seen.append({"name": name, **kwargs})
            return ()

        review_counterparties(
            self._contract(),
            # Наименование без ООО, Ltd и прочих маркеров: по одному названию
            # иностранной сторону не определить, а по стране — можно.
            llm_parties=(PartyNote(name="Zhejiang Yongkang", inn=None, role="поставщик", country="CN"),),
            lookup=lookup,
        )

        row = next(row for row in seen if row["name"] == "Zhejiang Yongkang")
        assert row["foreign"] is True

    def test_model_name_replaces_a_fragment_from_text_parsing(self):
        from app.counterparty.service import _merge_parties
        from app.llm.clauses import PartyNote
        from app.parsing.extract import PartyMention

        rows = _merge_parties(
            [PartyMention(name="Shenzhen Silk Road", inn=None, role=None)],
            (
                PartyNote(
                    name="Shenzhen Silk Road Trading Co., Ltd",
                    inn=None,
                    role="поставщик",
                    country="CN",
                ),
            ),
        )

        names = [name for name, _inn, _role, _country in rows]
        assert names == ["Shenzhen Silk Road Trading Co., Ltd"]
        assert rows[0][3] == "CN"

    def test_different_names_stay_two_parties(self):
        from app.counterparty.service import _merge_parties
        from app.llm.clauses import PartyNote
        from app.parsing.extract import PartyMention

        rows = _merge_parties(
            [PartyMention(name="Tether Limited", inn=None, role=None)],
            (PartyNote(name="Shenzhen Silk Road Trading", inn=None, role="поставщик"),),
        )

        assert len(rows) == 2
