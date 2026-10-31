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
    assert payload[0] == {"path": "rules.py", "line": 14, "body": "**P1 · MAJOR** — returns 1.0"}
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


# --- what models actually emit -------------------------------------------------
#
# Every case below is real reviewer output, or the shape of it. The first one was captured
# from deepseek-chat reviewing the brownfield bake-off fixture, and the parser as originally
# written matched NONE of it: the expression required the line to start with `FINDING:`, and
# the model wrote a numbered, bolded list. All findings were dropped, the gate fell back to the
# binary verdict, and the verdict was SURVIVED — a graded review became a silent pass.
#
# A parser that only accepts the contract as written is a parser that rejects the reviewers it
# exists to read.

REAL_REVIEWER_OUTPUT = """### Findings:

1. **FINDING: MINOR inventory/pricing/rules.py:8** — The `BULK_TIERS` are ordered from smallest
to largest discount thresholds (10, 50, 100).

2. **FINDING: MINOR inventory/pricing/rules.py:10** — The `applicable_rate` function does not
handle negative or zero quantities explicitly.

### Verdict:
**VERDICT: SURVIVED** — The change satisfies the task and criteria.
"""


def test_the_output_a_real_model_produced_is_parsed() -> None:
    found = rc.parse_findings(REAL_REVIEWER_OUTPUT)
    assert len(found) == 2, "captured from a live reviewer; the parser must read it"
    assert all(c.anchored for c in found)
    assert [c.line for c in found] == [8, 10]
    assert all(c.path == "inventory/pricing/rules.py" for c in found)
    assert found[0].body.startswith("The")
    assert "**" not in found[0].body and "FINDING" not in found[0].body


@pytest.mark.parametrize(
    "line,severity,path,lineno",
    [
        ("1. **FINDING: MINOR a/b.py:8** — text", "minor", "a/b.py", 8),
        ("2) FINDING: MAJOR x.ts:3 — text", "major", "x.ts", 3),
        ("- **FINDING: BLOCKER** `src/x.ts:12` — text", "blocker", "src/x.ts", 12),
        ("  > FINDING: NIT lib/u.go:44 - text", "nit", "lib/u.go", 44),
        ("* __FINDING: MAJOR app/pay.py:88-91__ – text", "major", "app/pay.py", 88),
        ("FINDING : MINOR a.py:1 — spaced colon", "minor", "a.py", 1),
    ],
)
def test_the_shapes_models_write_in(line: str, severity: str, path: str, lineno: int) -> None:
    [c] = rc.parse_findings(line)
    assert (c.severity, c.path, c.line) == (severity, path, lineno)
    assert c.body == "text" or "spaced colon" in c.body


def test_markdown_never_leaks_into_the_comment_body() -> None:
    """The body is shown to the implementer and posted on the pull request. Emphasis markers
    that arrived with the finding are formatting, not content."""
    [c] = rc.parse_findings("1. **FINDING: MAJOR a.py:1** — `rate` is wrong**")
    assert c.body == "`rate` is wrong"


def test_a_bolded_verdict_line_is_not_read_as_a_finding() -> None:
    assert rc.parse_findings("**VERDICT: SURVIVED** — all good") == []


def test_a_fix_round_that_changes_nothing_is_reported() -> None:
    """Measured on a live loop (deepseek implementing, Claude Opus reviewing): one fix round in
    two read the anchored comment, called two tools, edited NO file, and returned a confident
    summary. The convergence budget is bounded, so a silent no-op spends a third of it and the
    next round starts from the same code with the same findings — and nothing in the run said
    so, because the phase looked identical to one that worked."""
    import inspect

    from boss_engine.services import governed_code_task_service as g

    src = inspect.getsource(g)
    assert "code_task.fix_round_noop" in src, "a no-op fix round must emit an event"
    assert "fix_round_changed_nothing" in src, "and log it"
    # Compared against the sandbox diff rather than the agent's self-report, because the
    # self-report is exactly what was wrong in the measured case.
    assert "_before = {fc.path: fc.content for fc in sandbox.diff()}" in src


# --- the comments exist BEFORE the implementer is asked to fix anything --------
#
# They used to be produced twice from the same prose: once rendered into the repair prompt,
# and once again — much later — when the pull request was opened. Two code paths, two moments,
# one source text, and nothing keeping them in agreement. The review now records comments as it
# runs, and the repair round, the pull request and `list_review_comments` all read that one set.

def test_the_panel_records_its_comments_when_the_review_runs() -> None:
    import inspect

    from boss_engine.services import governed_code_task_service as g

    src = inspect.getsource(g)
    # Recorded at every point a review gate resolves, not only at the end of the run — a
    # re-review records ITS OWN summary (`{**evidence, "review_summary": ...}`), not the stale
    # one `evidence` still holds until the round passes.
    assert src.count("_record_review_comments(") >= 4
    assert "code_task.review_comments_recorded" in src


def test_the_repair_round_reads_the_recorded_set() -> None:
    import inspect

    from boss_engine.services import governed_code_task_service as g

    src = inspect.getsource(g)
    assert "_open = _open_review_comments(engine, run_id, project_id)" in src
    assert "REVIEW COMMENTS ON THIS CHANGE:" in src
    # And an unparseable reviewer is still not silenced.
    assert "must still reach the implementer rather than be silenced" in src


def test_the_pull_request_posts_the_same_recorded_set() -> None:
    import inspect

    from boss_engine.services import governed_code_task_service as g

    src = inspect.getsource(g)
    # The poster reads the stored set (and its rounds) the repair round and the build page read.
    post = inspect.getsource(g._post_review_comments_to_pr)
    assert "_review_record(engine, run_id, project_id)" in post


def test_recording_is_idempotent_on_the_same_finding() -> None:
    """A re-review of the same defect must not stack a second copy of a comment the
    implementer has already been shown."""
    import inspect

    from boss_engine.services import governed_code_task_service as g

    src = inspect.getsource(g._record_review_comments)
    assert "seen" in src and "continue" in src
    assert "stage" in src and "path" in src and "severity" in src


def test_operator_comments_sort_ahead_of_the_panels() -> None:
    ordered = sorted(
        [rc.ReviewComment(severity="blocker", body="panel", path="a.py", line=1),
         rc.ReviewComment(severity="nit", body="person", path="b.py", line=2,
                          author_kind=rc.AUTHOR_HUMAN)],
        key=lambda c: (0 if c.author_kind == rc.AUTHOR_HUMAN else 1, -c.blocking_rank),
    )
    assert ordered[0].body == "person", (
        "a person who pointed at a line outranks a reviewer, even a blocker: the repair round "
        "is bounded and they asked for this specifically"
    )


# --- a comment has to survive the retry it was written for --------------------
#
# Comments are stored per run and a retry mints a NEW run, so a comment left on a finished
# attempt reached nothing — and the attempt that would have acted on it started without it.
# That is the only moment a person leaves one: they read the PR, they point at a line, they
# press retry.

def test_open_comments_follow_the_work_into_the_next_attempt() -> None:
    from boss_engine.services.code_task_service import _carry_open_comments

    carried = _carry_open_comments({"review_comments": [
        {"id": "rc_1", "status": "open", "body": "still wrong"},
        {"id": "rc_4", "body": "no status means open"},
    ]})
    assert [c["id"] for c in carried] == ["rc_1", "rc_4"]


def test_settled_comments_are_left_behind() -> None:
    """Carrying an addressed or dismissed comment forward would re-litigate a decision the
    last attempt already made, and spend a bounded repair round doing it."""
    from boss_engine.services.code_task_service import _carry_open_comments

    carried = _carry_open_comments({"review_comments": [
        {"id": "rc_2", "status": "addressed", "body": "fixed"},
        {"id": "rc_3", "status": "dismissed", "body": "wontfix"},
    ]})
    assert carried == []


def test_a_run_with_no_comments_carries_nothing() -> None:
    from boss_engine.services.code_task_service import _carry_open_comments

    assert _carry_open_comments({}) == []
    assert _carry_open_comments({"review_comments": []}) == []


def test_the_retry_writes_them_onto_the_new_run() -> None:
    import inspect

    from boss_engine.services import code_task_service as cts

    src = inspect.getsource(cts.retry_code_task)
    assert '"review_comments": _carry_open_comments(fctx_prev)' in src, (
        "a retry must seed the new run with the comments the operator left on the last one"
    )
