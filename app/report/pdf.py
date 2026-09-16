"""PDF-заключение по результатам проверки.

Тексты те же, что в JSON и в консоли. Статические фразы проходят ту же
проверку, что и заключение: часть 2 статьи 30 № 282-ФЗ.
"""

from __future__ import annotations

from datetime import date
from io import BytesIO
from pathlib import Path

from reportlab.lib.colors import HexColor, black
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.norms.index import get_norms
from app.report.analysis import Assessment, assess_party, assess_wallet
from app.report.serialize import STATUS_LABEL, quote_norms_for
from app.rules.engine import ContractStatus, Finding, FindingStatus, Report
from app.rules.guardrail import assert_clean

FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    Path("/Library/Fonts/Arial.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
)

FONT_NAME = "ReportCyrillic"

_STATUS_COLOR = {
    ContractStatus.GREEN: HexColor("#1B5E20"),
    ContractStatus.YELLOW: HexColor("#E65100"),
    ContractStatus.RED: HexColor("#B71C1C"),
}

_MONTHS = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}

TITLE = "Предварительная проверка внешнеторгового контракта"
SUBTITLE = (
    "на соответствие требованиям Федерального закона от 04.08.2026 № 282-ФЗ "
    "и связанных актов"
)
NOTICE_TITLE = "Правовой статус документа"
NOTICE_LEAD = (
    "Настоящий документ сформирован программным средством предварительного "
    "автоматизированного контроля и носит информационно-справочный характер."
)
NOTICE_ITEMS = (
    "юридической консультацией, правовым заключением или иным документом, "
    "выражающим позицию квалифицированного юриста;",
    "подтверждением соответствия договора требованиям законодательства "
    "Российской Федерации;",
    "документом, предназначенным для представления в кредитную организацию, "
    "налоговый или иной государственный орган либо в суд в подтверждение "
    "такого соответствия;",
    "цифровым анализом в значении статьи 35 Федерального закона "
    "от 04.08.2026 № 282-ФЗ и не заменяет его.",
)
NOTICE_SOURCES = (
    "Сведения, полученные из внешних источников, приведены по состоянию "
    "на момент запроса и подлежат самостоятельной проверке. Отсутствие "
    "сведений по источнику означает, что сверка не выполнена, и не "
    "равнозначно отсутствию риска."
)
NOTICE_LIABILITY = (
    "Решения о заключении, изменении и исполнении договора принимаются "
    "пользователем самостоятельно. Ответственность за такие решения "
    "и их последствия несёт лицо, их принявшее."
)
DISCLAIMER = (
    f"{NOTICE_LEAD} Документ не является: " + " ".join(NOTICE_ITEMS)
)
FOOTER = (
    "Предварительный отчёт. Перепроверить вручную. Не для банка. "
    "Не юридическая консультация. Не цифровой анализ по ст. 35 № 282-ФЗ."
)
SECTION_BLOCKING = "Нарушены обязательные требования"
SECTION_ADVISORY = "Замечания"
SECTION_DEFERRED = "Нормы, вступающие в силу позднее"
SECTION_MANUAL = "Требует оценки юриста"
SECTION_CONTRACT = "Блок 1. Договор"
SECTION_CLAUSES = "Оговорки, которые формальная проверка не ловит"
SECTION_WALLET = "Блок 2. Адрес расчёта"
SECTION_PARTY = "Блок 3. Стороны и иные лица, названные в договоре"
WALLET_EMPTY = "В тексте договора не найден адрес кошелька. Оценка по открытым данным не выполнялась."
WALLET_NOT_ANALYSIS = (
    "Оценка адреса по открытым данным не является цифровым анализом "
    "в смысле статьи 35 Федерального закона от 04.08.2026 № 282-ФЗ."
)
WALLET_NOT_SCREENED = (
    "Коммерческий скоринг адреса не выполнялся: ни один из подключаемых "
    "сервисов не отработал. Оценка ниже построена только на открытых данных "
    "блокчейна."
)
PARTY_EMPTY = "Сверка не выполнена: в прогоне нет данных о сторонах."
HEAD_ESTABLISHED = "Установлено"
HEAD_CONCERNS = "Обращает на себя внимание"
HEAD_GAPS = "Не установлено"
HEAD_MANUAL = "Проверить до подписания"
LLM_SILENT = (
    "Локальная модель не ответила. Вердикт поставлен по матрице правил; "
    "смысл нестандартных оговорок модель не разбирала."
)
LLM_USED = (
    "Локальная модель разобрала оговорки, которые не ловятся регулярками. "
    "Вердикт поставлен матрицей правил."
)
CLAUSES_PARTIAL = (
    "Раздел заполнен не по всему тексту договора. Пустая строка по оговорке "
    "означает только то, что модель её не выписала."
)


class MissingCyrillicFontError(RuntimeError):
    pass


def resolve_font() -> Path:
    for candidate in FONT_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise MissingCyrillicFontError(
        "не найден TTF-шрифт с кириллицей; установите DejaVu Sans или Arial"
    )


def _ensure_font() -> str:
    if FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(FONT_NAME, str(resolve_font())))
    return FONT_NAME


def _xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br/>")
    )


def _format_date(value: date) -> str:
    return f"{value.day} {_MONTHS[value.month]} {value.year} г."


def _styles(font: str) -> dict[str, ParagraphStyle]:
    return {
        "title": ParagraphStyle(
            "title",
            fontName=font,
            fontSize=14,
            leading=18,
            alignment=TA_CENTER,
            spaceAfter=4,
            textColor=black,
        ),
        "subtitle": ParagraphStyle(
            "subtitle",
            fontName=font,
            fontSize=9,
            leading=12,
            alignment=TA_CENTER,
            spaceAfter=12,
            textColor=HexColor("#333333"),
        ),
        "status": ParagraphStyle(
            "status",
            fontName=font,
            fontSize=12,
            leading=16,
            alignment=TA_CENTER,
            spaceAfter=10,
        ),
        "meta": ParagraphStyle(
            "meta",
            fontName=font,
            fontSize=9,
            leading=12,
            alignment=TA_LEFT,
            spaceAfter=2,
        ),
        "heading": ParagraphStyle(
            "heading",
            fontName=font,
            fontSize=11,
            leading=14,
            spaceBefore=12,
            spaceAfter=6,
            textColor=black,
        ),
        "body": ParagraphStyle(
            "body",
            fontName=font,
            fontSize=9,
            leading=12,
            alignment=TA_JUSTIFY,
            spaceAfter=3,
        ),
        "indent": ParagraphStyle(
            "indent",
            fontName=font,
            fontSize=9,
            leading=12,
            leftIndent=10,
            spaceAfter=2,
        ),
        "quote": ParagraphStyle(
            "quote",
            fontName=font,
            fontSize=8,
            leading=11,
            leftIndent=14,
            alignment=TA_JUSTIFY,
            textColor=HexColor("#222222"),
            spaceAfter=4,
        ),
        "verdict": ParagraphStyle(
            "verdict",
            fontName=font,
            fontSize=9.5,
            leading=13,
            leftIndent=10,
            spaceAfter=5,
            textColor=HexColor("#8A3324"),
        ),
        "subhead": ParagraphStyle(
            "subhead",
            fontName=font,
            fontSize=8.5,
            leading=11,
            leftIndent=10,
            spaceBefore=4,
            spaceAfter=2,
            textColor=HexColor("#333333"),
        ),
        "notice_head": ParagraphStyle(
            "notice_head",
            fontName=font,
            fontSize=9.5,
            leading=13,
            spaceAfter=4,
            textColor=HexColor("#6B4E16"),
        ),
        "notice": ParagraphStyle(
            "notice",
            fontName=font,
            fontSize=8,
            leading=11,
            alignment=TA_JUSTIFY,
            spaceAfter=3,
            textColor=HexColor("#2B2B2B"),
        ),
        "notice_item": ParagraphStyle(
            "notice_item",
            fontName=font,
            fontSize=8,
            leading=11,
            leftIndent=10,
            alignment=TA_JUSTIFY,
            spaceAfter=2,
            textColor=HexColor("#2B2B2B"),
        ),
        "disclaimer": ParagraphStyle(
            "disclaimer",
            fontName=font,
            fontSize=8,
            leading=11,
            alignment=TA_JUSTIFY,
            spaceBefore=8,
            spaceAfter=10,
            textColor=HexColor("#333333"),
        ),
    }


def _footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont(FONT_NAME, 7)
    canvas.setFillColor(HexColor("#444444"))
    canvas.drawString(18 * mm, 12 * mm, FOOTER)
    canvas.drawRightString(A4[0] - 18 * mm, 12 * mm, str(doc.page))
    canvas.restoreState()


def _notice_block(styles: dict[str, ParagraphStyle]) -> KeepTogether:
    """Правовая оговорка отдельной рамкой в начале документа.

    Она стоит первой намеренно: читатель должен увидеть границы применения
    раньше, чем выводы, а не после них мелким шрифтом.
    """
    inner: list = [
        Paragraph(_xml(NOTICE_TITLE), styles["notice_head"]),
        Paragraph(_xml(NOTICE_LEAD), styles["notice"]),
        Paragraph("Документ не является:", styles["notice"]),
    ]
    for number, item in enumerate(NOTICE_ITEMS, start=1):
        inner.append(Paragraph(_xml(f"{number}) {item}"), styles["notice_item"]))
    inner.append(Paragraph(_xml(NOTICE_SOURCES), styles["notice"]))
    inner.append(Paragraph(_xml(NOTICE_LIABILITY), styles["notice"]))

    table = Table([[inner]], colWidths=[A4[0] - 36 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.8, HexColor("#8A6D3B")),
                ("BACKGROUND", (0, 0), (-1, -1), HexColor("#FBF7EF")),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return KeepTogether([table, Spacer(1, 10)])


def _assessment_blocks(
    assessment: Assessment, styles: dict[str, ParagraphStyle]
) -> list:
    """Разбор одного объекта: вывод, затем четыре именованных списка."""
    blocks: list = [
        Paragraph(_xml(assessment.subject), styles["body"]),
        Paragraph(_xml(assessment.headline), styles["verdict"]),
    ]
    for head, lines in (
        (HEAD_ESTABLISHED, assessment.established),
        (HEAD_CONCERNS, assessment.concerns),
        (HEAD_GAPS, assessment.gaps),
        (HEAD_MANUAL, assessment.manual),
    ):
        if not lines:
            continue
        blocks.append(Paragraph(_xml(head), styles["subhead"]))
        for line in lines:
            blocks.append(Paragraph(_xml(f"— {line}"), styles["indent"]))
    blocks.append(Spacer(1, 8))
    return blocks


def _finding_blocks(
    finding: Finding,
    styles: dict[str, ParagraphStyle],
    quoted: list[dict[str, str | None]] | None,
) -> list:
    blocks = [
        Paragraph(_xml(f"[{finding.code}] {finding.title}"), styles["body"]),
    ]
    if finding.evidence:
        blocks.append(Paragraph(_xml(finding.evidence), styles["indent"]))
    if finding.clauses:
        blocks.append(
            Paragraph(
                _xml("пункты договора: " + ", ".join(finding.clauses)),
                styles["indent"],
            )
        )
    if finding.norm_refs:
        refs = "; ".join(str(ref) for ref in finding.norm_refs)
        blocks.append(Paragraph(_xml(f"норма: {refs}"), styles["indent"]))
    if quoted:
        for item in quoted:
            if item["text"]:
                blocks.append(
                    Paragraph(_xml(f"{item['ref']}: {item['text']}"), styles["quote"])
                )
            elif item["note"]:
                blocks.append(
                    Paragraph(_xml(f"{item['ref']}: {item['note']}"), styles["quote"])
                )
    if finding.sanction:
        blocks.append(Paragraph(_xml(f"последствие: {finding.sanction}"), styles["indent"]))
    if finding.recommendation:
        blocks.append(Paragraph("редакция:", styles["indent"]))
        blocks.append(Paragraph(_xml(finding.recommendation), styles["indent"]))
    blocks.append(Spacer(1, 6))
    return blocks


def _llm_status_line(llm) -> str:
    if llm is None:
        return ""
    payload = llm.to_dict() if hasattr(llm, "to_dict") else llm
    if payload.get("available"):
        model = str(payload.get("model") or "").strip()
        line = LLM_USED if not model else f"{LLM_USED} Модель: {model}."
        coverage = payload.get("coverage") or {}
        summary = str(coverage.get("summary") or "").strip()
        if summary and not coverage.get("complete", True):
            line = f"{line} Покрытие: {summary}."
        tier = payload.get("tier") or {}
        note = str(tier.get("note") or "").strip()
        if note and tier.get("tier") in ("limited", "unsupported", "unknown"):
            line = f"{line} {note}"
        assert_clean(line)
        return line
    detail = str(payload.get("detail") or "").strip()
    line = detail or LLM_SILENT
    assert_clean(line)
    return line


def render_pdf(
    report: Report,
    *,
    source_name: str,
    quote_norms: bool = False,
    address_scores: list | None = None,
    counterparties: list | None = None,
    llm=None,
) -> bytes:
    for fragment in (
        TITLE,
        SUBTITLE,
        DISCLAIMER,
        NOTICE_TITLE,
        NOTICE_LEAD,
        NOTICE_SOURCES,
        NOTICE_LIABILITY,
        *NOTICE_ITEMS,
        HEAD_ESTABLISHED,
        HEAD_CONCERNS,
        HEAD_GAPS,
        HEAD_MANUAL,
        WALLET_NOT_SCREENED,
        FOOTER,
        SECTION_BLOCKING,
        SECTION_ADVISORY,
        SECTION_CONTRACT,
        SECTION_CLAUSES,
        SECTION_WALLET,
        SECTION_PARTY,
        WALLET_EMPTY,
        WALLET_NOT_ANALYSIS,
        PARTY_EMPTY,
        LLM_SILENT,
        LLM_USED,
    ):
        assert_clean(fragment)

    font = _ensure_font()
    styles = _styles(font)
    index = get_norms() if quote_norms else None
    color = _STATUS_COLOR[report.status]
    styles["status"].textColor = color

    passed = sum(1 for finding in report.findings if finding.status is FindingStatus.PASSED)
    skipped = sum(
        1 for finding in report.findings if finding.status is FindingStatus.NOT_APPLICABLE
    )
    deferred = [finding for finding in report.findings if finding.status is FindingStatus.DEFERRED]
    manual = report.needs_manual_review()

    story: list = [
        Paragraph(_xml(TITLE), styles["title"]),
        Paragraph(_xml(SUBTITLE), styles["subtitle"]),
        _notice_block(styles),
        Paragraph(_xml(f"Статус: {STATUS_LABEL[report.status]}"), styles["status"]),
        Paragraph(_xml(f"Документ: {Path(source_name).name}"), styles["meta"]),
        Paragraph(
            _xml(f"Проверено на дату: {_format_date(report.checked_on)}"),
            styles["meta"],
        ),
        Paragraph(
            _xml(
                f"Итого правил: {len(report.findings)}. Выполнено: {passed}. "
                f"Нарушено: {len(report.violations())}. Не применимо: {skipped}. "
                f"На ручной оценке: {len(manual)}."
            ),
            styles["meta"],
        ),
    ]
    llm_line = _llm_status_line(llm)
    if llm_line:
        story.append(Paragraph(_xml(llm_line), styles["meta"]))

    story.append(Paragraph(_xml(SECTION_CONTRACT), styles["heading"]))

    blocking = report.blocking_violations()
    if blocking:
        story.append(Paragraph(_xml(SECTION_BLOCKING), styles["heading"]))
        for finding in blocking:
            quoted = quote_norms_for(finding, index) if index else None
            story.extend(_finding_blocks(finding, styles, quoted))

    advisory = report.advisory_violations()
    if advisory:
        story.append(Paragraph(_xml(SECTION_ADVISORY), styles["heading"]))
        for finding in advisory:
            story.extend(_finding_blocks(finding, styles, None))

    if deferred:
        story.append(Paragraph(_xml(SECTION_DEFERRED), styles["heading"]))
        for finding in deferred:
            story.extend(_finding_blocks(finding, styles, None))

    if manual:
        story.append(Paragraph(_xml(SECTION_MANUAL), styles["heading"]))
        for finding in manual:
            story.append(Paragraph(_xml(f"[{finding.code}] {finding.title}"), styles["body"]))

    if llm is not None:
        payload = llm.to_dict() if hasattr(llm, "to_dict") else llm
        story.append(Paragraph(_xml(SECTION_CLAUSES), styles["heading"]))
        story.append(Paragraph(_xml(str(payload.get("detail") or "")), styles["body"]))
        if payload.get("model"):
            model_line = f"модель: {payload['model']}"
            tier = payload.get("tier") or {}
            if tier.get("label"):
                model_line += f" ({tier['label']})"
            story.append(Paragraph(_xml(model_line), styles["indent"]))
        coverage = payload.get("coverage") or {}
        # Покрытие имеет смысл, только если модель вообще отвечала: иначе
        # строка «прочитала 0%» дублирует «модель не ответила».
        if payload.get("available") and coverage.get("summary"):
            story.append(Paragraph(_xml(str(coverage["summary"])), styles["indent"]))
        if payload.get("available") and not coverage.get("complete", True):
            story.append(Paragraph(_xml(CLAUSES_PARTIAL), styles["quote"]))
        for note in payload.get("notes") or []:
            present = note.get("present")
            mark = "есть в тексте" if present else "в тексте не видно" if present is False else "не ясно"
            line = f"[{note.get('code')}] {mark}"
            story.append(Paragraph(_xml(line), styles["body"]))
            if note.get("quote"):
                story.append(Paragraph(_xml("цитата: " + note["quote"]), styles["quote"]))
            if note.get("reading"):
                story.append(Paragraph(_xml(note["reading"]), styles["indent"]))

    story.append(Paragraph(_xml(SECTION_WALLET), styles["heading"]))
    story.append(Paragraph(_xml(WALLET_NOT_ANALYSIS), styles["quote"]))
    if address_scores:
        for item in address_scores:
            payload = item.to_dict() if hasattr(item, "to_dict") else item
            story.extend(_assessment_blocks(assess_wallet(payload), styles))
            for note in payload.get("source_notes") or []:
                story.append(Paragraph(_xml(str(note)), styles["indent"]))
    else:
        story.append(Paragraph(_xml(WALLET_EMPTY), styles["body"]))

    story.append(Paragraph(_xml(SECTION_PARTY), styles["heading"]))
    if counterparties:
        for item in counterparties:
            payload = item.to_dict() if hasattr(item, "to_dict") else item
            story.extend(_assessment_blocks(assess_party(payload), styles))
    else:
        story.append(Paragraph(_xml(PARTY_EMPTY), styles["body"]))

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=20 * mm,
        title=TITLE,
        author="CompLeggeAI",
    )
    document.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()


def write_pdf(
    report: Report,
    path: Path,
    *,
    source_name: str,
    quote_norms: bool = False,
    address_scores: list | None = None,
    counterparties: list | None = None,
    llm=None,
) -> None:
    path.write_bytes(
        render_pdf(
            report,
            source_name=source_name,
            quote_norms=quote_norms,
            address_scores=address_scores,
            counterparties=counterparties,
            llm=llm,
        )
    )

