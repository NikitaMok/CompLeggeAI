"""Договор реалистичного объёма, а не полстраницы.

Образцы на 500 и 4 500 знаков не проверяют главного: доходит ли разбор до
последних страниц. Здесь тот же образцовый договор с восемьюдесятью
приложениями — 67 000 знаков, около тридцати страниц, пятьсот с лишним пунктов.
Оговорка про агента и комиссионера намеренно унесена в самый конец: если
разбор до неё не добирается, тест это показывает.
"""

from __future__ import annotations

import time

import pytest

from app.llm.clauses import _MAX_CHARS, analyze_clauses
from app.report.pdf import render_pdf
from app.report.serialize import serialize_report
from app.rules.contract import ContractView
from app.rules.engine import evaluate
from tests.fake_ollama import Behaviour, FakeOllama, payload, run_against

TAIL_MARKER = "агент, комиссионер или поверенный"


@pytest.fixture(scope="module")
def long_docx(fixtures_dir):
    return fixtures_dir / "contract_long.docx"


@pytest.fixture(scope="module")
def long_pdf(fixtures_dir):
    return fixtures_dir / "contract_long.pdf"


class TestParsingReachesTheEnd:
    def test_document_is_actually_long(self, long_docx):
        contract = ContractView.from_file(long_docx)

        assert len(contract.text) > 60_000
        assert len(contract.clauses) > 400

    def test_docx_and_pdf_give_the_same_verdict(self, long_docx, long_pdf):
        """Юрист приносит один и тот же договор в двух форматах.

        Дословное совпадение текста недостижимо: извлечение из PDF иначе
        переносит строки. Значение имеет другое — заключение не должно
        зависеть от того, в каком виде файл сохранили.
        """
        from_docx = evaluate(ContractView.from_file(long_docx))
        from_pdf = evaluate(ContractView.from_file(long_pdf))

        assert from_docx.status is from_pdf.status
        assert [(f.code, f.status) for f in from_docx.findings] == [
            (f.code, f.status) for f in from_pdf.findings
        ]

    def test_key_clauses_survive_both_formats(self, long_docx, long_pdf):
        markers = (
            TAIL_MARKER,
            "делистинг",
            "иностранным цифровым инструментом",
            "актуализируют реквизиты",
        )
        from_docx = ContractView.from_file(long_docx).text
        from_pdf = ContractView.from_file(long_pdf).text

        for marker in markers:
            assert marker in from_docx, marker
            assert marker in from_pdf, marker

    def test_last_clause_is_not_cut_off(self, long_docx):
        contract = ContractView.from_file(long_docx)

        assert TAIL_MARKER in contract.text
        assert contract.text.index(TAIL_MARKER) > _MAX_CHARS

    def test_clause_numbers_from_the_tail_are_addressable(self, long_docx):
        contract = ContractView.from_file(long_docx)
        matches = contract.matching_clauses("комиссионер")

        assert matches
        assert any(clause.number.startswith("99") for clause in matches)


class TestVerdictOnALongContract:
    def test_matrix_reads_the_whole_document(self, long_docx):
        """Матрица работает по полному тексту, а не по окну модели."""
        contract = ContractView.from_file(long_docx)
        report = evaluate(contract, moment=None)

        assert report.findings
        # Договор собран из образцового: обязательных нарушений быть не должно.
        assert not report.blocking_violations()

    def test_evaluation_is_not_slow(self, long_docx):
        contract = ContractView.from_file(long_docx)

        started = time.perf_counter()
        evaluate(contract)
        elapsed = time.perf_counter() - started

        assert elapsed < 10.0, f"матрица считала {elapsed:.1f} с на 67 000 знаках"

    def test_report_renders_to_pdf(self, long_docx, cyrillic_font):
        contract = ContractView.from_file(long_docx)
        report = evaluate(contract)

        pdf = render_pdf(report, source_name="contract_long.docx")

        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 10_000

    def test_report_serialises(self, long_docx):
        contract = ContractView.from_file(long_docx)
        payload_dict = serialize_report(evaluate(contract), source_name="contract_long.docx")

        assert payload_dict["counts"]["total"] > 0
        assert payload_dict["status"] in ("green", "yellow", "red")

    def test_long_pdf_fits_the_upload_limit(self, long_pdf):
        from app.core.config import get_settings

        assert long_pdf.stat().st_size < get_settings().max_upload_mb * 1024 * 1024


class TestModelCoverageOnALongContract:
    @pytest.mark.no_pipeline_stub
    def test_tail_is_inside_one_of_the_windows(self, long_docx):
        """Оговорка из конца договора обязана попасть хотя бы в одно окно."""
        contract = ContractView.from_file(long_docx)
        empty = payload({"parties": [], "notes": [], "wallets": []})

        with FakeOllama() as server:
            server.script([empty] * 6)
            run_against(server, lambda: analyze_clauses(contract))
            windows = server.prompts()

        assert len(windows) > 1
        assert any(TAIL_MARKER in window for window in windows)

    @pytest.mark.no_pipeline_stub
    def test_coverage_is_reported_as_complete_when_all_windows_answer(self, long_docx):
        contract = ContractView.from_file(long_docx)
        empty = payload({"parties": [], "notes": [], "wallets": []})

        with FakeOllama() as server:
            server.script([empty] * 6)
            result = run_against(server, lambda: analyze_clauses(contract))

        assert result.available
        assert result.coverage.complete
        assert result.coverage.chars_seen >= len(contract.text)

    @pytest.mark.no_pipeline_stub
    def test_silent_truncation_is_gone(self, long_docx):
        """Раньше модель видела первые 24 000 знаков и отчёт об этом молчал."""
        contract = ContractView.from_file(long_docx)
        empty = payload({"parties": [], "notes": [], "wallets": []})

        with FakeOllama() as server:
            server.script([empty])  # ответит только первое окно
            server.default = Behaviour(status=503, body={"error": "model unloaded"})
            result = run_against(server, lambda: analyze_clauses(contract))

        assert not result.coverage.complete
        assert result.coverage.chars_seen <= _MAX_CHARS
        assert "прочитала" in result.detail
        assert str(result.coverage.chars_total) in result.detail
