"""Review findings carry P1/P2/P3, and a disputed finding goes back to its reviewer (2026-10-30).

The operator asked for priorities the implementer can work by (P1 first) and for a real
back-and-forth: the implementer answers FIXED or DISPUTED, and the reviewer's next round judges
the dispute. Before this, a re-review saw only its own findings, so a DISPUTED finding was never
settled by anyone (CX PR #24 ended "0 fixed, 7 disputed")."""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from boss_engine.services import governed_code_task_service as g
from boss_engine.services import pr_review_post, review_comments as rc


@pytest.mark.parametrize("severity, priority", [
    ("blocker", "P1"), ("major", "P1"), ("minor", "P2"), ("nit", "P3"), ("MAJOR", "P1")])
def test_each_severity_has_one_priority(severity, priority):
    assert rc.priority_of(severity) == priority
    assert rc.priority_label(severity).startswith(priority + " · ")


def _comment(sev, path="app/pay.py", line=88, body="refunds twice"):
    return rc.ReviewComment.from_dict({"id": f"c-{sev}", "severity": sev, "path": path,
                                       "line": line, "body": body, "stage": "review_code",
                                       "status": "open", "author_kind": "agent", "round": 1})


def test_the_pull_request_shows_the_priority_on_each_line_comment():
    out = rc.to_github_comments([_comment("major")])
    assert out[0]["body"].startswith("**P1 · MAJOR** — ")


def test_a_round_counts_its_findings_by_priority():
    body = pr_review_post.round_body(
        {"round": 1, "seats": {"review_code": {"verdict": "refuted", "summary": "x"}}},
        [{"severity": "major", "body": "a", "round": 1},
         {"severity": "blocker", "body": "b", "round": 1},
         {"severity": "nit", "body": "c", "round": 1}],
        ordinal=1, total=1)
    assert "2 P1, 1 P3" in body
    assert "- **P1 · MAJOR** — a" in body


def test_the_implementer_works_p1_first_and_is_told_what_each_priority_means():
    ordered = rc.implementer_order([_comment("nit", line=3), _comment("major")])
    assert rc.priority_of(ordered[0].severity) == "P1"
    clause = rc.response_clause()
    assert "every P1 must be fixed before merge" in clause
    assert "goes back to its reviewer" in clause


def test_the_reviewer_is_told_how_severities_map_to_priorities():
    import inspect

    assert "BLOCKER and MAJOR are P1" in inspect.getsource(g)


class _Tx:
    def __init__(self, row):
        self.row = row

    def execute(self, *a, **k):
        return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: self.row))


def _with_record(monkeypatch, comments):
    ctx = comments if isinstance(comments, dict) else {"review_comments": comments}
    row = {"finalize_ctx": ctx}

    @contextmanager
    def tx(engine):
        yield _Tx(row)

    monkeypatch.setattr(g, "_transaction", tx)


def test_a_disputed_finding_goes_back_to_the_reviewer_with_the_implementers_reason(monkeypatch):
    _with_record(monkeypatch, [
        {"id": "a", "severity": "major", "path": "app/pay.py", "line": 88,
         "body": "refunds twice", "response_kind": "disputed",
         "response": "the second call is the idempotent retry; see test_refund_once"},
        {"id": "b", "severity": "nit", "path": "app/x.py", "line": 1, "body": "name",
         "response_kind": "fixed", "response": "renamed"},
        {"id": "c", "severity": "minor", "body": "unanswered"},
    ])
    answers = g._implementer_answers(None, "crun_x", "boss-platform")
    assert "[P1 · MAJOR] app/pay.py:88 — refunds twice" in answers
    assert "IMPLEMENTER: DISPUTED — the second call is the idempotent retry" in answers
    assert "IMPLEMENTER: FIXED — renamed" in answers
    assert "unanswered" not in answers
    assert "ACCEPTED: <path:line>" in answers and "raise the finding again" in answers
    assert "never on the implementer's say-so" in answers

    rereview = g._fix_round_change("FINDING: MAJOR app/pay.py:88 — refunds twice",
                                   {"app/pay.py": "def refund(): ...\n"}, answers)
    assert rereview.index("YOUR PREVIOUS FINDINGS") < rereview.index("THE IMPLEMENTER'S ANSWERS") \
        < rereview.index("WHAT THE FIX CHANGED")


def test_with_nothing_answered_the_re_review_is_unchanged(monkeypatch):
    _with_record(monkeypatch, [{"id": "c", "severity": "minor", "body": "x"}])
    assert g._implementer_answers(None, "crun_x", "boss-platform") == ""
    assert "THE IMPLEMENTER'S ANSWERS" not in g._fix_round_change("F", {"a.py": "x"}, "")


# ── the reviewers see what the operator asked for (2026-10-31) ──────────────────────────────

def test_the_operators_feedback_reaches_every_reviewer_as_p1_checks(monkeypatch):
    """KG P6a PR #25 passed review ("no blocking defect") while 4 of the operator's 8 points were
    undone: steers and fix notes reached only the implementer."""
    _with_record(monkeypatch, {"feedback_history": [
        {"attempt": 1, "fix_note": "old note"},
        {"attempt": 2, "fix_note": "(2) wire external_agent_tokens.verify into mcp/server.py"}],
        "applied_guidance": [{"text": "(6) route the endpoint through egress_rules"},
                             {"text": "(2) wire external_agent_tokens.verify into mcp/server.py"}]})
    block = g._operator_review_block(None, "crun_x", "boss-platform")
    assert "Every point below is a P1" in block and "REFUTED" in block
    assert "[1] (2) wire external_agent_tokens.verify" in block
    assert "[2] (6) route the endpoint through egress_rules" in block
    assert "old note" not in block and block.count("wire external_agent_tokens") == 1
    prompt = g._review_prompt("T", "C", change="x", operator=block)
    assert prompt.index("FEEDBACK THIS ATTEMPT MUST ADDRESS") < prompt.index("adversarial code reviewer")


def test_with_no_feedback_the_review_prompt_is_unchanged(monkeypatch):
    _with_record(monkeypatch, {})
    assert g._operator_review_block(None, "crun_x", "boss-platform") == ""
    assert g._review_prompt("T", "C", change="x") == g._review_prompt("T", "C", change="x",
                                                                      operator="")


def test_every_review_seat_is_handed_the_feedback():
    import inspect

    src = inspect.getsource(g)
    # The first panel seat, the merged panel section (BOSSAPP-1000) and the fix round's
    # re-review each hand the reviewer the operator's feedback.
    assert src.count("operator=_operator_review_block(") == 4
