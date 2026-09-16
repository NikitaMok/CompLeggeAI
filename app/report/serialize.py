"""Сериализация заключения для HTTP и PDF.

Тексты нарушений те же, что уходят в консоль: рекомендации только из матрицы,
цитаты норм — только из выверенного корпуса.
"""

from __future__ import annotations

from app.llm.clauses import ClauseAnalysis
from app.norms.index import NormIndex, get_norms
from app.report.analysis import assess_party, assess_wallet
from app.rules.engine import ContractStatus, Finding, FindingStatus, Report

STATUS_LABEL = {
    ContractStatus.GREEN: "ЗЕЛЁНЫЙ — обязательные требования выполнены",
    ContractStatus.YELLOW: "ЖЁЛТЫЙ — есть замечания рекомендательного характера",
    ContractStatus.RED: "КРАСНЫЙ — нарушены обязательные требования",
}


def quote_norms_for(finding: Finding, index: NormIndex) -> list[dict[str, str | None]]:
    quoted: list[dict[str, str | None]] = []
    for ref in finding.norm_refs:
        label = str(ref)
        resolved = index.resolve_ref(ref.act, ref.ref)
        if not resolved:
            quoted.append(
                {"ref": label, "text": None, "note": "текста нормы в корпусе нет"}
            )
            continue

        with_text = [norm for norm in resolved if norm.has_text]
        if not with_text:
            note = next((norm.note for norm in resolved if norm.note), "")
            suffix = f" {note.strip()}" if note and note.strip() else ""
            quoted.append(
                {"ref": label, "text": None, "note": f"текст не выверен.{suffix}"}
            )
            continue

        for norm in with_text[:2]:
            quoted.append({"ref": label, "text": norm.text, "note": None})
    return quoted


def finding_to_dict(
    finding: Finding,
    *,
    quoted: list[dict[str, str | None]] | None = None,
) -> dict:
    payload: dict = {
        "code": finding.code,
        "group": finding.group,
        "severity": finding.severity.value,
        "title": finding.title,
        "status": finding.status.value,
        "evidence": finding.evidence,
        "clauses": list(finding.clauses),
        "norms": [str(ref) for ref in finding.norm_refs],
        "sanction": finding.sanction,
        "recommendation": finding.recommendation,
    }
    if quoted is not None:
        payload["quoted_norms"] = quoted
    return payload


def counts(report: Report) -> dict[str, int]:
    findings = report.findings
    return {
        "total": len(findings),
        "passed": sum(1 for finding in findings if finding.status is FindingStatus.PASSED),
        "failed": len(report.violations()),
        "not_applicable": sum(
            1 for finding in findings if finding.status is FindingStatus.NOT_APPLICABLE
        ),
        "deferred": sum(1 for finding in findings if finding.status is FindingStatus.DEFERRED),
        "manual": len(report.needs_manual_review()),
        "blocking": len(report.blocking_violations()),
        "advisory": len(report.advisory_violations()),
    }


def serialize_report(
    report: Report,
    *,
    source_name: str,
    quote_norms: bool = False,
    address_scores: list | None = None,
    counterparties: list | None = None,
    llm: ClauseAnalysis | None = None,
) -> dict:
    index = get_norms() if quote_norms else None

    def _dump(finding: Finding, *, with_quotes: bool) -> dict:
        quoted = quote_norms_for(finding, index) if with_quotes and index else None
        return finding_to_dict(finding, quoted=quoted)

    wallets = [
        item.to_dict() if hasattr(item, "to_dict") else item
        for item in (address_scores or [])
    ]
    parties = [
        item.to_dict() if hasattr(item, "to_dict") else item
        for item in (counterparties or [])
    ]

    payload = {
        "status": report.status.value,
        "status_label": STATUS_LABEL[report.status],
        "source": source_name,
        "checked_on": report.checked_on.isoformat(),
        "counts": counts(report),
        "blocking": [
            _dump(finding, with_quotes=quote_norms)
            for finding in report.blocking_violations()
        ],
        "advisory": [_dump(finding, with_quotes=False) for finding in report.advisory_violations()],
        "deferred": [
            _dump(finding, with_quotes=False)
            for finding in report.findings
            if finding.status is FindingStatus.DEFERRED
        ],
        "manual": [_dump(finding, with_quotes=False) for finding in report.needs_manual_review()],
        "address_scores": wallets,
        "counterparties": parties,
        # Разбор тот же, что печатается в PDF: страница и заключение
        # не должны расходиться в выводах.
        "wallet_analysis": [assess_wallet(item).to_dict() for item in wallets],
        "party_analysis": [assess_party(item).to_dict() for item in parties],
        "llm": llm.to_dict() if llm is not None else None,
    }
    return payload
