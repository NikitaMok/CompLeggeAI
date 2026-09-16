"""Проверка контракта из командной строки.

    python -m scripts.check_contract путь/к/договору.docx
    python -m scripts.check_contract договор.pdf --on 2027-07-01
    python -m scripts.check_contract --search "репатриация цифровой валюты"
    python -m scripts.check_contract --search "возврат валюты в Россию" --dense
    python -m scripts.check_contract договор.docx --on 2026-09-01 --pdf заключение.pdf
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from datetime import date
from pathlib import Path

from app.norms.hybrid import hybrid_search
from app.norms.index import NormIndex, get_norms
from app.parsing.document import EmptyDocumentError, UnsupportedFormatError
from app.pipeline import run_check
from app.report.serialize import STATUS_LABEL
from app.rules.contract import ContractView
from app.rules.engine import ContractStatus, Finding, FindingStatus, Report
from app.rules.guardrail import CircumventionAttempt

RULE = "-" * 78

# Консоль Windows по умолчанию работает в cp1251: кириллица в неё пишется, но
# любой символ вне кодировки роняет вывод. Заменяем такие символы вместо падения.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")


def _format_norms(finding: Finding) -> str:
    return "; ".join(str(ref) for ref in finding.norm_refs) or "—"


def _wrap(text: str, indent: str = " " * 8, width: int = 70) -> str:
    return textwrap.fill(
        " ".join(text.split()),
        width=width,
        initial_indent=indent,
        subsequent_indent=indent,
    )


def _print_norm_texts(finding: Finding, norms: NormIndex) -> None:
    for ref in finding.norm_refs:
        resolved = norms.resolve_ref(ref.act, ref.ref)
        if not resolved:
            print(f"    {ref}: текста нормы в корпусе нет")
            continue

        with_text = [norm for norm in resolved if norm.has_text]
        if not with_text:
            note = next((norm.note for norm in resolved if norm.note), "")
            print(f"    {ref}: текст не выверен. {note.strip()}")
            continue

        print(f"    {ref}:")
        for norm in with_text[:2]:
            print(_wrap(norm.text))


def _print_findings(
    title: str, findings: list[Finding], norms: NormIndex | None = None
) -> None:
    if not findings:
        return

    print(f"\n{title}\n{RULE}")
    for finding in findings:
        print(f"[{finding.code}] {finding.title}")
        if finding.evidence:
            print(f"    {finding.evidence}")
        if finding.clauses:
            print(f"    пункты договора: {', '.join(finding.clauses)}")
        if norms is None:
            print(f"    норма: {_format_norms(finding)}")
        else:
            _print_norm_texts(finding, norms)
        if finding.sanction:
            print(f"    последствие: {finding.sanction}")
        if finding.recommendation:
            print("    редакция:")
            print(_wrap(finding.recommendation))
        print()


def print_search(query: str, limit: int = 8, *, use_dense: bool | None = None) -> None:
    if use_dense:
        from app.norms.dense import dense_available

        if not dense_available():
            print("эмбеддинги недоступны, ищу только по словам", file=sys.stderr)
    hits = hybrid_search(query, limit=limit, use_dense=use_dense)
    print(f"Запрос: {query}")
    print(RULE)
    if not hits:
        print("Ничего не найдено.")
        return
    for hit in hits:
        print(f"{hit.norm.reference}  ({hit.score:.2f})")
        print(_wrap(hit.norm.text, indent="    "))
        print()


def print_clause_notes(llm) -> None:
    if llm is None:
        return
    payload = llm.to_dict() if hasattr(llm, "to_dict") else llm
    print(f"\nОГОВОРКИ, КОТОРЫЕ ФОРМАЛЬНАЯ ПРОВЕРКА НЕ ЛОВИТ\n{RULE}")
    print(payload.get("detail") or "")
    if payload.get("model"):
        tier = payload.get("tier") or {}
        suffix = f" ({tier['label']})" if tier.get("label") else ""
        print(f"    модель: {payload['model']}{suffix}")
        if tier.get("note") and tier.get("tier") in ("limited", "unsupported", "unknown"):
            print(_wrap(tier["note"]))
    coverage = payload.get("coverage") or {}
    if payload.get("available") and coverage.get("summary"):
        print(_wrap("покрытие: " + coverage["summary"]))
    for note in payload.get("notes") or []:
        present = note.get("present")
        mark = "есть в тексте" if present else "в тексте не видно" if present is False else "не ясно"
        print(f"[{note.get('code')}] {mark}")
        if note.get("quote"):
            print(_wrap("цитата: " + note["quote"]))
        if note.get("reading"):
            print(_wrap(note["reading"]))
    print()


def print_counterparties(parties: list) -> None:
    print(f"\nЛИЦА, НАЗВАННЫЕ В ДОГОВОРЕ\n{RULE}")
    if not parties:
        print("сверка не выполнена")
        print()
        return
    for item in parties:
        payload = item.to_dict() if hasattr(item, "to_dict") else item
        who = payload.get("name") or payload.get("inn") or "сторона не названа"
        role = payload.get("role")
        print(f"{who} — {role}" if role else who)
        if payload.get("inn"):
            print(f"    ИНН {payload['inn']}")
        if payload.get("foreign"):
            print("    иностранная сторона")
        if payload.get("summary"):
            print(_wrap(str(payload["summary"]), indent="    "))
        for hit in payload.get("hits") or []:
            print(_wrap(str(hit.get("detail") or hit.get("source")), indent="    "))
        print()


def print_address_scores(scores: list) -> None:
    print(f"\nКОШЕЛЁК\n{RULE}")
    if not scores:
        print("в тексте нет адреса; оценка по открытым данным не выполнялась")
        print()
        return
    for item in scores:
        payload = item.to_dict() if hasattr(item, "to_dict") else item
        line = f"{payload.get('address')} ({payload.get('network')}): {payload.get('band')}"
        if payload.get("score") is not None:
            line += f", оценка {payload['score']}"
        print(line)
        for factor in payload.get("factors") or []:
            print(f"    {factor}")
        for label in payload.get("labels") or []:
            print(f"    метка: {label}")
        if payload.get("error"):
            print(f"    {payload['error']}")
        for note in payload.get("source_notes") or []:
            print(f"    {note}")
        if payload.get("disclaimer"):
            print(_wrap(str(payload["disclaimer"]), indent="    "))
        print()


def print_report(report: Report, source: Path, quote_norms: bool = False) -> None:
    norms = get_norms() if quote_norms else None

    print(f"\nДокумент: {source.name}")
    print(f"Проверено на дату: {report.checked_on.isoformat()}")
    print(f"Статус: {STATUS_LABEL[report.status]}")

    _print_findings(
        "НАРУШЕНЫ ОБЯЗАТЕЛЬНЫЕ ТРЕБОВАНИЯ", report.blocking_violations(), norms
    )
    _print_findings("ЗАМЕЧАНИЯ", report.advisory_violations())

    deferred = [f for f in report.findings if f.status is FindingStatus.DEFERRED]
    _print_findings("НОРМЫ, ВСТУПАЮЩИЕ В СИЛУ ПОЗДНЕЕ", deferred)

    manual = report.needs_manual_review()
    if manual:
        print(f"\nТРЕБУЕТ ОЦЕНКИ ЮРИСТА\n{RULE}")
        for finding in manual:
            print(f"[{finding.code}] {finding.title}")

    passed = sum(1 for f in report.findings if f.status is FindingStatus.PASSED)
    skipped = sum(1 for f in report.findings if f.status is FindingStatus.NOT_APPLICABLE)
    print(
        f"\n{RULE}\nИтого правил: {len(report.findings)}. "
        f"Выполнено: {passed}. Нарушено: {len(report.violations())}. "
        f"Не применимо: {skipped}. На ручной оценке: {len(manual)}."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка внешнеторгового контракта")
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        help="контракт в формате PDF или DOCX",
    )
    parser.add_argument(
        "--search",
        metavar="QUERY",
        help="найти нормы по формулировке, без проверки договора",
    )
    parser.add_argument(
        "--on",
        type=date.fromisoformat,
        default=None,
        help="дата, на которую проверяется применимость норм "
        "(если не указана и сегодня раньше 01.09.2026 — проверяется на 01.09.2026)",
    )
    parser.add_argument(
        "--quote-norms",
        action="store_true",
        help="приводить текст нормы под каждым нарушением обязательного требования",
    )
    parser.add_argument(
        "--pdf",
        type=Path,
        metavar="PATH",
        help="сохранить заключение в PDF",
    )
    parser.add_argument(
        "--dense",
        action="store_true",
        help="подключить поиск по эмбеддингам (нужен fastembed и кэш векторов)",
    )
    args = parser.parse_args(argv)

    if args.dense and args.search is None:
        parser.error("--dense только вместе с --search")

    if args.search is not None:
        if args.path is not None or args.pdf is not None:
            parser.error("укажите либо путь к контракту, либо --search")
        print_search(args.search, use_dense=True if args.dense else None)
        return 0

    if args.path is None:
        parser.error("укажите путь к контракту или --search")

    try:
        contract = ContractView.from_file(args.path)
    except (FileNotFoundError, UnsupportedFormatError, EmptyDocumentError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2

    try:
        result = run_check(contract, source_name=args.path.name, moment=args.on)
    except CircumventionAttempt as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2

    print_report(result.report, args.path, quote_norms=args.quote_norms)
    print_clause_notes(result.llm)
    print_address_scores(list(result.address_scores))
    print_counterparties(list(result.counterparties))

    if args.pdf is not None:
        from app.report.pdf import MissingCyrillicFontError, write_pdf

        try:
            write_pdf(
                result.report,
                args.pdf,
                source_name=args.path.name,
                quote_norms=args.quote_norms,
                address_scores=list(result.address_scores),
                counterparties=list(result.counterparties),
                llm=result.llm,
            )
        except (MissingCyrillicFontError, OSError) as error:
            print(f"Ошибка: {error}", file=sys.stderr)
            return 2
        print(f"PDF: {args.pdf}")

    return 1 if result.report.status is ContractStatus.RED else 0


if __name__ == "__main__":
    raise SystemExit(main())
