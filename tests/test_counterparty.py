from datetime import date

from app.counterparty.models import SourceHit, names_match, summarize
from app.counterparty.providers import lookup_free_sources
from app.counterparty.service import review_counterparties
from app.llm.clauses import ClauseAnalysis
from app.parsing.document import Document, SourceFormat
from app.pipeline import run_check
from app.rules.contract import ContractView
from app.rules.engine import ContractStatus
import httpx
import pytest


def _view(text: str) -> ContractView:
    lines = [line for line in text.strip().split("\n") if line.strip()]
    return ContractView.from_document(Document(SourceFormat.DOCX, lines))


class TestNameMatch:
    def test_ignores_legal_form_and_quotes(self):
        assert names_match("ООО «Уралимпорт»", 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "УРАЛИМПОРТ"')


class TestSummarize:
    def test_no_sources_is_not_ok(self):
        text = summarize((), foreign=False, has_inn=True)

        assert "не выполнена" in text
        assert "порядке" not in text.lower()

    def test_foreign_without_inn(self):
        text = summarize((), foreign=True, has_inn=False)

        assert "сверка не выполнена" in text
        assert "иностранн" in text.lower()


class TestEgrulMock:
    def test_parses_search_rows(self):
        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host or ""
            path = request.url.path
            if "nalog.gov.ru" in host and request.method == "POST":
                return httpx.Response(200, json={"t": "token-1"})
            if "search-result" in path:
                return httpx.Response(
                    200,
                    json={
                        "rows": [
                            {
                                "n": 'ООО "УРАЛИМПОРТ"',
                                "i": "6659123456",
                                "o": "1026600000001",
                                "e": "",
                            }
                        ]
                    },
                )
            return httpx.Response(503, text="offline")

        transport = httpx.MockTransport(handler)
        with httpx.Client(transport=transport) as client:
            hits = lookup_free_sources("6659123456", "ООО «Уралимпорт»", client)

        egrul = next(hit for hit in hits if hit.source_id == "egrul")
        assert egrul.performed
        assert egrul.found
        assert egrul.name_match is True

    def test_captcha_is_not_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"captchaRequired": True})

        transport = httpx.MockTransport(handler)
        with httpx.Client(transport=transport) as client:
            hits = lookup_free_sources("6659123456", "ООО «Уралимпорт»", client)

        egrul = next(hit for hit in hits if hit.source_id == "egrul")
        assert not egrul.performed
        assert "сверка не выполнена" in egrul.detail or "не вернул" in egrul.detail

    def test_html_challenge_is_not_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host or ""
            html = "<html><div class='g-recaptcha'></div>Just a moment</html>"
            if "rusprofile" in host:
                return httpx.Response(200, text=html)
            if "saby.ru" in host:
                return httpx.Response(200, text=html)
            if "nalog.gov.ru" in host:
                return httpx.Response(200, text=html)
            return httpx.Response(503, text="offline")

        transport = httpx.MockTransport(handler)
        with httpx.Client(transport=transport) as client:
            hits = lookup_free_sources("6659123456", "ООО «Уралимпорт»", client)

        for source_id in ("egrul", "rusprofile", "saby"):
            hit = next(item for item in hits if item.source_id == source_id)
            assert not hit.performed
            assert "сверка не выполнена" in hit.detail or "капч" in hit.detail.lower()


@pytest.fixture
def opencorporates_token(monkeypatch):
    """OpenCorporates без токена отвечает 401, поэтому запрос не отправляется.

    Разбор его ответа проверяется с заданным токеном.
    """
    from app.core.config import get_settings

    settings = get_settings()
    saved = settings.opencorporates_api_token
    settings.opencorporates_api_token = "test-token"
    yield
    settings.opencorporates_api_token = saved


class TestForeignLookup:
    def test_russian_inn_does_not_query_foreign_registries(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host or ""
            seen.append(host)
            if "nalog.gov.ru" in host and request.method == "POST":
                return httpx.Response(200, json={"t": "token-1"})
            if "search-result" in (request.url.path or ""):
                return httpx.Response(200, json={"rows": []})
            return httpx.Response(200, text="<html><h1>card</h1></html>")

        transport = httpx.MockTransport(handler)
        with httpx.Client(transport=transport) as client:
            lookup_free_sources("6659123456", "ООО «Уралимпорт»", client, foreign=False)

        assert not any("opencorporates" in host or "gleif" in host for host in seen)

    def test_name_match_is_found(self, opencorporates_token):
        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host or ""
            if "opencorporates.com" in host:
                return httpx.Response(
                    200,
                    json={
                        "results": {
                            "companies": [
                                {
                                    "company": {
                                        "name": "SHENZHEN PRECISION MACHINERY CO., LTD.",
                                        "company_number": "91440300MA5XXXXX",
                                        "jurisdiction_code": "cn",
                                        "inactive": False,
                                        "current_status": "Active",
                                    }
                                }
                            ]
                        }
                    },
                )
            if "gleif.org" in host:
                return httpx.Response(200, json={"data": []})
            return httpx.Response(503, text="offline")

        transport = httpx.MockTransport(handler)
        with httpx.Client(transport=transport) as client:
            hits = lookup_free_sources(
                None,
                "Shenzhen Precision Machinery Co., Ltd.",
                client,
                foreign=True,
            )

        oc = next(hit for hit in hits if hit.source_id == "opencorporates")
        assert oc.performed
        assert oc.found
        assert oc.name_match is True
        assert oc.jurisdiction == "cn"

    def test_http_error_is_not_success(self, opencorporates_token):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="offline")

        transport = httpx.MockTransport(handler)
        with httpx.Client(transport=transport) as client:
            hits = lookup_free_sources(
                None,
                "Shenzhen Precision Machinery Co., Ltd.",
                client,
                foreign=True,
            )

        for source_id in ("opencorporates", "gleif"):
            hit = next(item for item in hits if item.source_id == source_id)
            assert not hit.performed
            assert "сверка не выполнена" in hit.detail

    def test_foreign_summary_from_hit(self):
        hit = SourceHit(
            source_id="opencorporates",
            performed=True,
            found=True,
            legal_name="SHENZHEN PRECISION MACHINERY CO., LTD.",
            name_match=True,
            status="Active",
            detail="OpenCorporates: found",
        )
        text = summarize((hit,), foreign=True, has_inn=False)
        assert "найден" in text
        assert "правоспособности" in text


class TestReview:
    def test_no_identifiers_stated_plainly(self):
        checks = review_counterparties(
            _view("1.1. Стороны заключили договор."),
            lookup=lambda inn, name, client=None, **_: (),
        )

        assert checks[0].summary.startswith("в тексте нет")


@pytest.mark.no_pipeline_stub
class TestPipelineVerdict:
    def test_model_notes_do_not_paint_status(self, violating_docx):
        def fake_llm(contract):
            return ClauseAnalysis(
                available=True,
                model="test",
                detail="локальная модель разобрала оговорки, которые не ловятся регулярками",
                notes=(),
            )

        result = run_check(
            ContractView.from_file(violating_docx),
            source_name=violating_docx.name,
            moment=date(2026, 9, 1),
            analyze=fake_llm,
            score_addresses=lambda contract: [],
            review_parties=lambda contract, llm_parties=(): [],
        )

        assert result.report.status is ContractStatus.RED
        assert result.llm.available
