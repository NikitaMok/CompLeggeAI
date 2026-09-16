"""Что можно утверждать по HTML-карточке агрегатора.

Здесь закрыт самый опасный из найденных дефектов. На живом прогоне 15.09.2026
карточка Сбербанка на saby.ru дала «ликвидирована»: статус определялся поиском
слова по всей разметке страницы, а слово встречается где-то в вёрстке.
В заключении это становилось «по реестру организация ликвидирована либо
исключена из ЕГРЮЛ» — ложным обвинением действующего контрагента.

Правило теперь такое: правовой статус даёт только ЕГРЮЛ структурированным
ответом. HTML-страница подтверждает ровно одно — карточка по этому ИНН открылась.
"""

from __future__ import annotations

import httpx
import pytest

from app.counterparty.models import summarize
from app.counterparty.providers import OPENCORPORATES_NO_TOKEN, lookup_free_sources

pytestmark = pytest.mark.no_pipeline_stub

INN = "7707083893"

# Разметка, повторяющая устройство реальной карточки: наименование в заголовке,
# а слово «ликвидированные» — в постороннем блоке страницы.
CARD_WITH_STRAY_WORD = f"""
<html><head><title>Сбербанк, ПАО — реквизиты, ИНН {INN}</title></head>
<body>
  <h1>Сбербанк, ПАО</h1>
  <div class="requisites">ИНН {INN}, ОГРН 1027700132195</div>
  <nav class="sidebar">
    <a href="/lists/active">Действующие организации</a>
    <a href="/lists/liquidated">Ликвидированные организации</a>
  </nav>
  <section class="related">
    <p>Связанные организации: ООО «Ромашка» (ликвидирована 12.03.2019)</p>
  </section>
</body></html>
"""


def _html_only(html: str):
    """Транспорт: ЕГРЮЛ молчит, HTML-карточки отвечают."""

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if "nalog.gov.ru" in host:
            return httpx.Response(503, text="offline")
        return httpx.Response(200, text=html)

    return httpx.MockTransport(handler)


class TestStatusIsNotGuessedFromMarkup:
    def test_stray_word_does_not_liquidate_a_live_company(self):
        with httpx.Client(transport=_html_only(CARD_WITH_STRAY_WORD)) as client:
            hits = lookup_free_sources(INN, "Сбербанк", client=client)

        html_hits = [hit for hit in hits if hit.source_id in ("rusprofile", "saby")]
        assert html_hits, "HTML-источники должны были отработать"
        for hit in html_hits:
            assert hit.performed
            assert hit.found
            assert hit.status is None, f"{hit.source_id}: статус не должен браться из разметки"
            assert "ликвидир" not in hit.detail.lower()

    def test_summary_does_not_call_a_live_company_liquidated(self):
        with httpx.Client(transport=_html_only(CARD_WITH_STRAY_WORD)) as client:
            hits = lookup_free_sources(INN, "Сбербанк", client=client)

        summary = summarize(hits, foreign=False, has_inn=True)

        assert "ликвидир" not in summary.lower()
        assert "исключен" not in summary.lower()

    def test_report_says_where_the_status_should_be_checked(self):
        with httpx.Client(transport=_html_only(CARD_WITH_STRAY_WORD)) as client:
            hits = lookup_free_sources(INN, "Сбербанк", client=client)

        saby = next(hit for hit in hits if hit.source_id == "saby")
        assert "ЕГРЮЛ" in saby.detail

    def test_company_name_is_still_read_from_the_card(self):
        with httpx.Client(transport=_html_only(CARD_WITH_STRAY_WORD)) as client:
            hits = lookup_free_sources(INN, "Сбербанк", client=client)

        saby = next(hit for hit in hits if hit.source_id == "saby")
        assert saby.legal_name == "Сбербанк, ПАО"
        assert saby.name_match is True


class TestSiteChromeIsNotMistakenForACompany:
    def test_generic_page_title_is_not_taken_as_a_legal_name(self):
        html = f"""<html><head><title>Проверка контрагентов онлайн</title></head>
        <body><h1>Проверка контрагентов онлайн</h1><p>ИНН {INN}</p></body></html>"""

        with httpx.Client(transport=_html_only(html)) as client:
            hits = lookup_free_sources(INN, "Сбербанк", client=client)

        saby = next(hit for hit in hits if hit.source_id == "saby")
        assert saby.legal_name is None
        assert saby.name_match is None
        assert "карточка по ИНН открыта" in saby.detail

    def test_page_without_the_inn_is_not_a_find(self):
        html = "<html><head><title>Ничего не найдено</title></head><body></body></html>"

        with httpx.Client(transport=_html_only(html)) as client:
            hits = lookup_free_sources(INN, "Сбербанк", client=client)

        saby = next(hit for hit in hits if hit.source_id == "saby")
        assert saby.found is False


class TestHonestFailures:
    def test_rusprofile_404_is_not_read_as_absence_of_a_record(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "rusprofile" in request.url.host:
                return httpx.Response(404, text="not found")
            return httpx.Response(503, text="offline")

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            hits = lookup_free_sources(INN, "Сбербанк", client=client)

        rusprofile = next(hit for hit in hits if hit.source_id == "rusprofile")
        assert rusprofile.performed is False
        assert rusprofile.found is False
        assert "сверка не выполнена" in rusprofile.detail
        assert "адрес поиска" in rusprofile.detail

    def test_opencorporates_without_a_token_says_what_to_do(self):
        """Без токена запрос не отправляется вовсе: он гарантированно 401."""
        asked: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            asked.append(request.url.host)
            return httpx.Response(200, json={"data": []})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            hits = lookup_free_sources(None, "Tether Limited", client=client, foreign=True)

        assert not any("opencorporates" in host for host in asked)
        oc = next(hit for hit in hits if hit.source_id == "opencorporates")
        assert oc.performed is False
        assert oc.detail == OPENCORPORATES_NO_TOKEN
        assert "OPENCORPORATES_API_TOKEN" in oc.detail

    def test_silent_egrul_with_open_cards_is_not_a_confirmation(self):
        with httpx.Client(transport=_html_only(CARD_WITH_STRAY_WORD)) as client:
            hits = lookup_free_sources(INN, "Сбербанк", client=client)

        egrul = next(hit for hit in hits if hit.source_id == "egrul")
        assert egrul.performed is False
        summary = summarize(hits, foreign=False, has_inn=True)
        assert "не подтверждение правоспособности" in summary
