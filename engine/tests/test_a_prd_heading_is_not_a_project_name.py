"""A document's heading is not the name of the thing the document describes.

Two runs appeared in the console called "Product Requirements Document (PRD)", one of them
still wearing the markdown asterisks it arrived with. The pipeline hands the PRD text to
`project_name`, which walks the lines looking for the first that is not boilerplate — and
`BOILERPLATE` was anchored so that the phrase had to END the line.

Real PRDs never do. They are headed "Product Requirements Document (PRD)", and the pipeline's
own markdown arrives bold: "**Product Requirements Document (PRD)**". Both slipped past a
check written for the bare phrase, so the document TYPE became the project's name.

This has bitten before in this exact file — the docstring on `_unadorn` records the emoji
variant ("📄 Product Requirements Document"), which had the same cause and was fixed by
stripping leading adornment only. Trailing adornment was left, which is why the bold form got
through. The tests below cover the shape of the mistake, not just the two spellings that
happened to reach the screen.
"""

from __future__ import annotations

import pytest

from boss_engine.services.naming import BOILERPLATE, _unadorn, project_name

SUBJECT = "A shift-swap board where staff can offer and claim shifts."


class TestTheHeadingIsRecognisedAsBoilerplate:
    @pytest.mark.parametrize("heading", [
        "Product Requirements Document",
        "Product Requirements Document (PRD)",
        "**Product Requirements Document (PRD)**",
        "# Product Requirements Document (PRD)",
        "📄 Product Requirements Document",
        "*PRD*",
        "PRD (v2)",
        "PRD:",
        "Spec (draft)",
        "Overview",
        "__Summary__",
        "`Design`",
    ])
    def test_a_document_heading_is_not_a_name(self, heading):
        assert BOILERPLATE.match(_unadorn(heading)), (
            f"{heading!r} names the document, not the product; if this is not recognised the "
            "document type becomes the project name"
        )

    @pytest.mark.parametrize("real", [
        "Shift-Swap Board",
        "Incident timeline builder",
        "PRD Review Tool",           # genuinely about PRDs — the words are the product
        "Document Scanner",
        "Design System Explorer",
    ])
    def test_a_real_name_is_not_mistaken_for_boilerplate(self, real):
        assert not BOILERPLATE.match(_unadorn(real)), (
            f"{real!r} is a product whose name happens to contain a document word"
        )


class TestTheNameComesFromTheSubject:
    @pytest.mark.parametrize("heading", [
        "# Product Requirements Document (PRD)",
        "**Product Requirements Document (PRD)**",
        "📄 Product Requirements Document",
        "## PRD",
    ])
    def test_the_heading_is_skipped_for_what_follows(self, heading):
        name, _blurb, _label = project_name(f"{heading}\n\n{SUBJECT}")
        lowered = name.lower()
        assert "requirements document" not in lowered
        assert "prd" != lowered.strip()
        assert name.strip(), "skipping the heading must not leave an empty name"

    def test_no_markdown_survives_into_the_name(self):
        name, _b, _l = project_name(f"**Product Requirements Document (PRD)**\n\n{SUBJECT}")
        for junk in ("**", "__", "##", "`"):
            assert junk not in name

    def test_an_explicit_project_line_still_wins(self):
        name, _b, _l = project_name(
            "# Product Requirements Document (PRD)\n\n**Project:** BugBox\n\nA bug triage board."
        )
        assert name == "BugBox"


class TestAdornmentIsStrippedFromBothEnds:
    """Leading-only stripping is what let the bold heading through."""

    @pytest.mark.parametrize("raw,expected", [
        ("**Bold**", "Bold"),
        ("__Underscored__", "Underscored"),
        ("# Heading #", "Heading"),
        ("`code`", "code"),
        ("📄 Document 📄", "Document"),
        ("  • Bullet  ", "Bullet"),
    ])
    def test_both_ends(self, raw, expected):
        assert _unadorn(raw) == expected

    def test_an_all_adornment_string_does_not_underflow(self):
        assert _unadorn("***") == ""
        assert _unadorn("") == ""
