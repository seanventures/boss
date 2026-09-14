"""A review that cannot point at a line makes the implementer guess.

The reviewer's contract was `FINDING: <SEVERITY> — <prose>`. Severity graded the gate
correctly, and then the prose went back to the implementer with no idea WHERE. On a
thirty-hunk change that spends a bounded repair round re-reading the diff to work out which
line was meant — before it can change anything.

A person reviewing the same change comments on line 47. That is the difference, and it is the
difference between a review that improves the code and one that only blocks it.

The anchor is OPTIONAL on purpose. A model that forgets it must degrade to the old behaviour,
because a dropped finding is a silent quality regression while an unanchored one is merely a
worse comment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boss_engine.services import review_comments as rc
from boss_engine.services.governed_code_task_service import (
    _anchored_findings,
    _parse_findings,
    _review_gate_from_findings,
)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "inventory" / "pricing").mkdir(parents=True)
    (tmp_path / "inventory" / "pricing" / "rules.py").write_text(
        "\n".join(f"line {i}" for i in range(1, 31)))
    return tmp_path


# --- parsing ------------------------------------------------------------------

def test_a_finding_carries_its_file_and_line() -> None:
    [c] = rc.parse_findings(
        "FINDING: MAJOR inventory/pricing/rules.py:14 — unknown SKU returns 1.0")
    assert c.severity == "major"
    assert c.path == "inventory/pricing/rules.py" and c.line == 14
    assert c.body == "unknown SKU returns 1.0"
    assert c.anchored


def test_a_finding_can_span_lines() -> None:
    [c] = rc.parse_findings("FINDING: BLOCKER app/pay.py:88-91 — the refund path double-charges")
    assert (c.line, c.end_line) == (88, 91)
    assert c.location() == "app/pay.py:88-91"


def test_an_unanchored_finding_still_counts() -> None:
    """The old contract. A reviewer that forgets the anchor must not have its finding dropped."""
    [c] = rc.parse_findings("FINDING: NIT — naming could be clearer")
    assert c.severity == "nit" and c.body == "naming could be clearer"
    assert not c.anchored and c.location() == "(no location given)"


def test_a_backwards_range_is_a_typo_not_an_instruction() -> None:
    [c] = rc.parse_findings("FINDING: MINOR a.py:40-10 — something")
    assert c.line == 40 and c.end_line is None


def test_no_findings_never_means_clean() -> None:
    """An empty list is 'no structured signal', and the caller falls back to the verdict.
    Reading it as a pass would turn a reviewer that ignored the contract into an approval."""
    assert rc.parse_findings("I looked at it and it seems fine.") == []
    passed, blocking, _ = _review_gate_from_findings("no structured output", "refuted")
    assert passed is False and blocking == []


def test_the_gate_still_grades_on_severity(tmp_path: Path) -> None:
    summary = ("FINDING: NIT a.py:1 — naming\n"
               "FINDING: MAJOR b.py:2 — a criterion is violated")
    passed, blocking, deferred = _review_gate_from_findings(summary, "survived", "major")
    assert passed is False
    assert [f["severity"] for f in blocking] == ["major"]
    assert [f["severity"] for f in deferred] == ["nit"]


def test_the_pipeline_parser_keeps_the_shape_its_callers_expect() -> None:
    """`text` and `severity` are read by the gate and the follow-up task; adding a location
    must not move them."""
    [f] = _parse_findings("FINDING: MAJOR a/b.py:3 — broken")
    assert f["severity"] == "major" and f["text"] == "broken"
    assert f["path"] == "a/b.py" and f["line"] == 3


# --- what the implementer receives ---------------------------------------------

def test_the_implementer_is_shown_the_code_the_comment_is_about(repo: Path) -> None:
    out = _anchored_findings(
        "FINDING: MAJOR inventory/pricing/rules.py:14 — unknown SKU returns 1.0",
        repo, stage="review_code", header="ADVERSARIAL REVIEW FINDINGS:")
    assert "rules.py:14" in out
    assert "line 14" in out, "the source at that line must be quoted"
    assert ">    14 |" in out, "the line itself must be marked"
    assert "line 11" in out and "line 17" in out, "with surrounding context"


def test_a_reviewer_may_name_a_short_path(repo: Path) -> None:
    """Reviewers write `rules.py` when the file is `inventory/pricing/rules.py`. Refusing that
    punishes the reviewer for being readable."""
    assert rc.resolve_path(repo, "rules.py") is not None
    assert rc.resolve_path(repo, "inventory/pricing/rules.py") is not None


def test_an_ambiguous_path_resolves_to_nothing_rather_than_a_guess(repo: Path) -> None:
    """Pointing the implementer at the WRONG line is worse than giving it no line."""
    (repo / "other").mkdir()
    (repo / "other" / "rules.py").write_text("x = 1\n")
    assert rc.resolve_path(repo, "rules.py") is None


def test_a_comment_past_the_end_of_the_file_says_the_code_moved(repo: Path) -> None:
    out = rc.quote_source(repo, rc.ReviewComment(
        severity="major", body="x", path="inventory/pricing/rules.py", line=9999))
    assert "moved" in out, "a stale anchor must be visible, not silently misapplied"


def test_blocking_comments_are_put_first(repo: Path) -> None:
    """A repair round is bounded, so what it spends its turns on is decided here."""
    out = rc.render_for_implementer([
        rc.ReviewComment(severity="nit", body="naming", path="a.py", line=1),
        rc.ReviewComment(severity="blocker", body="data loss", path="b.py", line=2),
        rc.ReviewComment(severity="minor", body="edge case", path="c.py", line=3),
    ])
    assert out.index("data loss") < out.index("edge case") < out.index("naming")


def test_a_resolved_comment_is_not_sent_again() -> None:
    out = rc.render_for_implementer([
        rc.ReviewComment(severity="major", body="done already", path="a.py", line=1,
                         status=rc.STATUS_ADDRESSED),
    ])
    assert out == ""


def test_an_unparseable_review_still_reaches_the_implementer(repo: Path) -> None:
    """A reviewer that ignores the contract must not be silenced — that would turn a
    formatting problem into a quality one."""
    out = _anchored_findings("The error handling here is weak.", repo,
                             stage="review_code", header="ADVERSARIAL REVIEW FINDINGS:")
    assert "The error handling here is weak." in out


def test_the_reader_is_told_who_raised_each_comment() -> None:
    out = rc.render_for_implementer([
        rc.ReviewComment(severity="major", body="from a person", path="a.py", line=1,
                         author="ceo", author_kind=rc.AUTHOR_HUMAN),
        rc.ReviewComment(severity="major", body="from the panel", path="b.py", line=1),
    ])
    assert "the operator" in out and "a reviewer" in out


# --- what reaches the pull request ---------------------------------------------

def test_anchored_comments_become_inline_pr_comments() -> None:
    payload = rc.to_github_comments([
        rc.ReviewComment(severity="major", body="returns 1.0", path="rules.py", line=14),
        rc.ReviewComment(severity="blocker", body="double charge", path="pay.py",
                         line=88, end_line=91),
    ])
    assert payload[0] == {"path": "rules.py", "line": 14, "body": "**MAJOR** — returns 1.0"}
    assert payload[1]["start_line"] == 88 and payload[1]["line"] == 91


def test_an_unanchored_finding_is_not_silently_dropped_from_the_pr() -> None:
    """GitHub rejects a review comment with no path+line, so those go in the review body
    instead. Dropping them would hide findings on the artifact people actually read."""
    comments = [
        rc.ReviewComment(severity="major", body="anchored", path="a.py", line=1),
        rc.ReviewComment(severity="minor", body="no location", path=None, line=None),
    ]
    assert len(rc.to_github_comments(comments)) == 1
    assert "no location" in rc.unanchored_body(comments)


def test_the_prompt_asks_for_a_location() -> None:
    from boss_engine.services.governed_code_task_service import _review_prompt

    prompt = _review_prompt("t", ["c"])
    assert "FINDING: <SEVERITY> <path>:<line>" in prompt
    assert "SAY WHERE" in prompt
    # And says what to do when there genuinely is no one place.
    assert "omit the path" in prompt


def test_a_comment_survives_a_round_trip_through_storage() -> None:
    original = rc.ReviewComment(severity="major", body="x", path="a.py", line=3,
                                author="ceo", author_kind=rc.AUTHOR_HUMAN)
    assert rc.ReviewComment.from_dict({**original.to_dict(), "id": "rc_1"}) == original
