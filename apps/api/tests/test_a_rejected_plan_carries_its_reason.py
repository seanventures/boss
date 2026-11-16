"""A rejected plan carries the person's reason in words (BOSSAPP-1321, AR-27).

northgate-gateway, crun_b14450fc (2026-11-16): the plan card was rejected with only the "Why
no?" chips to say why, and the build recorded "The plan was rejected" and nothing more, so the
next attempt's planner could not read what was wrong. A reject now takes an optional note: it is
stored on the decision, it is the build's stated outcome ("The plan was rejected: <why>", which
retry_code_task hands the next attempt as the previous attempt's outcome) and it is kept on the
run as `plan_rejected_note`. A surface's placeholder ("Resolved from the BOSS console.") is not a
reason and is never recorded as one.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from apps.api.tests.test_the_command_center_v2_tells_the_truth import _plan_wait
from boss_engine.services.decisions import person_reject_note


def _reject(client, dec: str, note: str | None):
    body = {"actor": "ceo", **({"note": note} if note is not None else {})}
    got = client.post(f"/v1/decisions/{dec}/reject", json=body)
    assert got.status_code in (200, 201), got.text
    return got.json()


def _run(engine, run_id: str) -> dict:
    with engine.begin() as c:
        return dict(c.execute(text("SELECT status, error, finalize_ctx FROM code_task_runs "
                                   "WHERE id = :i"), {"i": run_id}).mappings().one())


def test_the_reason_reaches_the_decision_and_the_build(engine, client):
    with engine.begin() as c:
        run_id, dec = _plan_wait(c, title="Northgate gateway")
    _reject(client, dec, "  The plan drops the HIGH path traversal; fix that first.  ")
    with engine.begin() as c:
        note = c.execute(text("SELECT resolution_note FROM decisions WHERE id = :i"),
                         {"i": dec}).scalar()
    assert "HIGH path traversal" in note
    run = _run(engine, run_id)
    assert run["status"] == "cancelled"
    assert run["error"] == ("The plan was rejected: The plan drops the HIGH path traversal; "
                            "fix that first.")
    assert run["finalize_ctx"]["plan_rejected"] is True
    assert run["finalize_ctx"]["plan_rejected_note"] == (
        "The plan drops the HIGH path traversal; fix that first.")


@pytest.mark.parametrize("note", [None, "", "Resolved from the BOSS console.",
                                  "Rejected from the project page."])
def test_no_written_reason_records_none(engine, client, note):
    with engine.begin() as c:
        run_id, dec = _plan_wait(c, title="Northgate gateway")
    _reject(client, dec, note)
    run = _run(engine, run_id)
    assert run["status"] == "cancelled"
    assert run["error"] == "The plan was rejected"
    assert "plan_rejected_note" not in (run["finalize_ctx"] or {})


def test_a_placeholder_is_not_a_reason():
    assert person_reject_note("Resolved from the BOSS console.") is None
    assert person_reject_note("  ") is None
    assert person_reject_note("too\n  vague") == "too vague"



@pytest.mark.parametrize("note", ["Rejected by Ana Ruiz from chat.", "Rejected from Infra.",
                                  "Rejected by usr_12ab from chat", "Approved from Infra.",
                                  "Rejected from the project page.",
                                  "Resolved from the BOSS console."])
def test_a_surfaces_who_and_where_is_not_a_reason(note):
    """PR #158 review 3: chat_channels' "Rejected by … from chat." and Infra's words name only
    who and where — never the person's why."""
    assert person_reject_note(note) is None


@pytest.mark.parametrize("note", ["The plan uses Node; the brief says Go.",
                                  "Rejected from the start: it ignores the rulebook."])
def test_a_written_reason_is_kept(note):
    assert person_reject_note(note) == note
