"""Markdown -> PDF rendering, pure-Python (reportlab, no system deps).

Self-contained on purpose: imports only reportlab + stdlib so it can be used
both by the API/worker (native publish path) and by the standalone doc-render
MCP server without dragging in the rest of the app.

Supports the markdown subset a spec/PRD needs: # / ## / ### headings, paragraphs,
- / * bullet lists, 1. numbered lists, ``` code blocks, --- rules, blockquotes,
and inline **bold** / *italic* / `code`.
"""
from __future__ import annotations

import html
import io
import re
from typing import Optional

from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import (
    HRFlowable,
    ListFlowable,
    ListItem,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


def _inline(md: str) -> str:
    """Convert inline markdown to reportlab's mini-HTML markup, escaping the rest."""
    # protect inline code spans first
    code_spans: list[str] = []

    def _stash(m: re.Match) -> str:
        code_spans.append(m.group(1))
        return f"\x00{len(code_spans) - 1}\x00"

    md = re.sub(r"`([^`]+)`", _stash, md)
    md = html.escape(md, quote=False)
    md = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", md)
    md = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", md)
    # markdown links [text](url) -> text (url)
    md = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<u>\1</u> (\2)', md)

    def _restore(m: re.Match) -> str:
        snippet = html.escape(code_spans[int(m.group(1))], quote=False)
        return f'<font face="Courier">{snippet}</font>'

    return re.sub(r"\x00(\d+)\x00", _restore, md)


def _styles():
    base = getSampleStyleSheet()
    styles = {
        "title": ParagraphStyle("PRDTitle", parent=base["Title"], fontSize=24, leading=28, spaceAfter=6),
        "subtitle": ParagraphStyle("PRDSub", parent=base["Normal"], fontSize=11, textColor="#555555", spaceAfter=2),
        "h1": ParagraphStyle("PRDH1", parent=base["Heading1"], fontSize=17, leading=21, spaceBefore=16, spaceAfter=6),
        "h2": ParagraphStyle("PRDH2", parent=base["Heading2"], fontSize=14, leading=18, spaceBefore=12, spaceAfter=4),
        "h3": ParagraphStyle("PRDH3", parent=base["Heading3"], fontSize=12, leading=15, spaceBefore=10, spaceAfter=3),
        "body": ParagraphStyle("PRDBody", parent=base["BodyText"], fontSize=10.5, leading=15, alignment=TA_LEFT, spaceAfter=6),
        "quote": ParagraphStyle("PRDQuote", parent=base["BodyText"], fontSize=10.5, leading=15, leftIndent=14,
                                 textColor="#444444", borderColor="#cccccc", spaceAfter=6),
        "code": ParagraphStyle("PRDCode", parent=base["Code"], fontSize=8.5, leading=11, backColor="#f4f4f6",
                               borderPadding=6, spaceAfter=8),
        # Table cells wrap, so they are Paragraphs rather than bare strings: a long agent name
        # in a fixed-width column otherwise runs off the page edge instead of wrapping.
        "th": ParagraphStyle("PRDTH", parent=base["BodyText"], fontSize=9, leading=12,
                             textColor="#333333", spaceAfter=0),
        "td": ParagraphStyle("PRDTD", parent=base["BodyText"], fontSize=9, leading=12,
                             spaceAfter=0),
    }
    return styles


#: The page margins, named once. The table width is derived from these, and a table sized
#: against a different margin than the page uses is a table that runs off the edge.
_MARGIN = 0.9 * inch
_CONTENT_WIDTH = LETTER[0] - (2 * _MARGIN)

#: A markdown table row: at least one pipe, and pipes at both ends after stripping.
_TABLE_ROW = re.compile(r"^\|.*\|$")
#: The separator under the header — `| --- | :--- | ---: |`.
_TABLE_RULE = re.compile(r"^\|[\s:|-]+\|$")


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _table_flowable(rows: list[list[str]], styles, width: float):
    """A markdown table as a real table.

    WHY THIS EXISTS. The renderer had no table case, so every row fell through to the
    paragraph handler and printed as literal `| Research | succeeded | 3m 41s | $0.87 |`,
    separator row and all. Documents that were readable in the console arrived as pipe soup in
    the PDF — and the PDF is the copy that gets mailed and filed.

    Columns share the width evenly rather than being measured: an even split is wrong by a
    little on every table, and measuring is wrong by a lot on the first one whose longest cell
    is an outlier.
    """
    if not rows:
        return None
    header, body = rows[0], rows[1:]
    data = [[Paragraph(_inline(c), styles["th"]) for c in header]]
    data += [[Paragraph(_inline(c), styles["td"]) for c in row] for row in body]

    columns = max(len(r) for r in data)
    # Ragged rows are legal markdown and would otherwise raise inside reportlab.
    for row in data:
        while len(row) < columns:
            row.append(Paragraph("", styles["td"]))

    table = Table(data, colWidths=[width / columns] * columns, hAlign="LEFT", repeatRows=1)
    table.setStyle(TableStyle([
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.HexColor("#bbbbbb")),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, colors.HexColor("#e4e4e8")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f4f4f6")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return table


def render_markdown_pdf(
    markdown_text: str,
    *,
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
) -> bytes:
    """Render markdown to PDF bytes."""
    styles = _styles()
    flow = []
    if title:
        flow.append(Paragraph(_inline(title), styles["title"]))
    if subtitle:
        flow.append(Paragraph(_inline(subtitle), styles["subtitle"]))
    if title or subtitle:
        flow.append(HRFlowable(width="100%", thickness=0.6, color="#dddddd", spaceBefore=8, spaceAfter=12))

    lines = (markdown_text or "").replace("\r\n", "\n").split("\n")
    i = 0
    n = len(lines)
    pending_bullets: list[str] = []
    pending_numbers: list[str] = []

    def flush_lists():
        nonlocal pending_bullets, pending_numbers
        if pending_bullets:
            flow.append(ListFlowable(
                [ListItem(Paragraph(_inline(b), styles["body"]), leftIndent=10) for b in pending_bullets],
                bulletType="bullet", start="•", leftIndent=14,
            ))
            flow.append(Spacer(1, 4))
            pending_bullets = []
        if pending_numbers:
            flow.append(ListFlowable(
                [ListItem(Paragraph(_inline(b), styles["body"]), leftIndent=10) for b in pending_numbers],
                bulletType="1", leftIndent=16,
            ))
            flow.append(Spacer(1, 4))
            pending_numbers = []

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # fenced code block
        if stripped.startswith("```"):
            flush_lists()
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            flow.append(Preformatted("\n".join(buf) or " ", styles["code"]))
            continue

        if not stripped:
            flush_lists()
            i += 1
            continue

        # headings
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_lists()
            level = len(m.group(1))
            key = "h1" if level == 1 else "h2" if level == 2 else "h3"
            flow.append(Paragraph(_inline(m.group(2)), styles[key]))
            i += 1
            continue

        # table — header row, separator, then body until the first non-row line
        if _TABLE_ROW.match(stripped) and i + 1 < n and _TABLE_RULE.match(lines[i + 1].strip()):
            flush_lists()
            rows = [_split_row(stripped)]
            i += 2  # the header and its separator
            while i < n and _TABLE_ROW.match(lines[i].strip()):
                rows.append(_split_row(lines[i].strip()))
                i += 1
            table = _table_flowable(rows, styles, _CONTENT_WIDTH)
            if table is not None:
                flow.append(table)
                flow.append(Spacer(1, 8))
            continue

        # horizontal rule
        if re.match(r"^(\*{3,}|-{3,}|_{3,})$", stripped):
            flush_lists()
            flow.append(HRFlowable(width="100%", thickness=0.5, color="#dddddd", spaceBefore=6, spaceAfter=6))
            i += 1
            continue

        # blockquote
        if stripped.startswith(">"):
            flush_lists()
            flow.append(Paragraph(_inline(stripped.lstrip("> ").strip()), styles["quote"]))
            i += 1
            continue

        # bullet list
        mb = re.match(r"^[-*+]\s+(.*)$", stripped)
        if mb:
            if pending_numbers:
                flush_lists()
            pending_bullets.append(mb.group(1))
            i += 1
            continue

        # numbered list
        mn = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if mn:
            if pending_bullets:
                flush_lists()
            pending_numbers.append(mn.group(1))
            i += 1
            continue

        # paragraph (gather consecutive non-empty, non-special lines)
        flush_lists()
        para = [stripped]
        i += 1
        while i < n and lines[i].strip() and not re.match(r"^(#{1,6}\s|[-*+]\s|\d+[.)]\s|>|```)", lines[i].strip()):
            para.append(lines[i].strip())
            i += 1
        flow.append(Paragraph(_inline(" ".join(para)), styles["body"]))

    flush_lists()
    if not flow:
        flow.append(Paragraph("(empty document)", styles["body"]))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=LETTER,
        leftMargin=_MARGIN, rightMargin=_MARGIN,
        topMargin=_MARGIN, bottomMargin=_MARGIN,
        title=title or "Document",
    )
    doc.build(flow)
    return buffer.getvalue()


def pdf_page_count(pdf_bytes: bytes) -> int:
    """Cheap page count without a PDF parser dependency (count /Type /Page)."""
    try:
        return max(1, len(re.findall(rb"/Type\s*/Page[^s]", pdf_bytes)))
    except Exception:
        return 1
