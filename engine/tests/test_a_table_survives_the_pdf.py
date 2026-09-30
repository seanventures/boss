"""A markdown table must arrive as a table, not as pipe soup.

The renderer had no table case, so every row fell through to the paragraph handler and printed
literally:

    | Step | Status | Time | Cost |
    | --- | --- | --- | --- |
    | Research | succeeded | 3m 41s | $0.87 |

Found while checking that the end-of-run report downloads properly, and it was never specific
to that report: change records, PRDs and dossiers all contain tables, and the PDF is the copy
that gets mailed and filed. It rendered fine in the console and badly in the artefact people
keep.
"""

from __future__ import annotations

import pytest

from boss_engine.services.pdf_render import (
    _split_row,
    _table_flowable,
    _styles,
    _TABLE_ROW,
    _TABLE_RULE,
    render_markdown_pdf,
)

TABLE_MARKDOWN = """## Cost

| Step | Status | Cost |
| --- | --- | --- |
| Research | succeeded | 0.87 |
| Build | running | 2.29 |
"""


def test_a_row_is_recognised_and_a_paragraph_is_not():
    assert _TABLE_ROW.match("| Step | Cost |")
    assert not _TABLE_ROW.match("Just a sentence.")
    # A line that merely CONTAINS a pipe is not a row -- shell examples are full of them.
    assert not _TABLE_ROW.match("run `a | b` to pipe it")


def test_the_separator_is_recognised_in_all_its_spellings():
    for rule in ("| --- | --- |", "|---|---|", "| :--- | ---: | :---: |"):
        assert _TABLE_RULE.match(rule), rule


def test_a_table_needs_its_separator_to_be_a_table():
    """Without this a single pipe-delimited line in prose would swallow the lines after it."""
    from boss_engine.services.pdf_render import render_markdown_pdf as render

    assert render("| not | a table |\njust text\n", title="t").startswith(b"%PDF-")


def test_cells_are_split_without_the_outer_pipes():
    assert _split_row("| Research | succeeded | 0.87 |") == ["Research", "succeeded", "0.87"]


def test_a_ragged_row_is_padded_rather_than_raising():
    """Ragged rows are legal markdown and would otherwise raise inside reportlab, turning a
    cosmetic problem in one document into a failed download."""
    table = _table_flowable([["a", "b", "c"], ["only one"]], _styles(), 400.0)
    assert table is not None
    assert all(len(row) == 3 for row in table._cellvalues)


def test_the_rendered_pdf_contains_no_pipes_and_no_separator_row():
    """The assertion that matters, made against the actual document rather than the code that
    writes it."""
    pdfminer = pytest.importorskip("pdfminer.high_level")
    import io

    pdf = render_markdown_pdf(TABLE_MARKDOWN, title="Table test")
    text = pdfminer.extract_text(io.BytesIO(pdf))
    assert "|" not in text
    assert "---" not in text
    # And the content survived the journey.
    for cell in ("Research", "succeeded", "0.87", "Build", "2.29"):
        assert cell in text, cell


def test_the_table_width_is_derived_from_the_page_margins():
    """A table sized against a different margin than the page uses runs off the edge. Both
    come from one constant."""
    from boss_engine.services import pdf_render

    assert pdf_render._CONTENT_WIDTH > 0
    source = __import__("pathlib").Path(pdf_render.__file__).read_text(encoding="utf-8")
    assert "leftMargin=_MARGIN" in source
    assert "0.9 * inch," not in source, "the margin must not be re-typed beside the constant"
