"""BOSSAPP-893: decision examples — overrides captured automatically, Save as example, and an
anonymized JSONL export (no secret, no person, no project id).
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from ._ops2_support import decide, fresh_engine, make_project
from boss_engine.services import decision_examples as dx
from boss_engine.services.core import create_blocking_decision

SECRET = "sk-live-NEVERLEAVES0123456789abcdef"


@pytest.fixture(scope="module")
def eng():
    engine, drop = fresh_engine("dex")
    yield engine
    drop()


@pytest.fixture()
def project(eng):
    return make_project(eng)


def _decision(eng, project, question="Deploy the shop to production?"):
    with eng.begin() as c:
        return create_blocking_decision(
            c, project, None, question,
            {"action": "review_deployment", "requested_by": "alice@example.com",
             "summary": f"Owner alice@example.com, key {SECRET}", "api_key": "abc123secretvalue",
             "run_id": "crun_123", "target": "production"},
            {}, "deploy_gate", risk_tier=1)


def _examples(eng, project, **kw):
    with eng.begin() as c:
        return dx.list_examples(c, project, **kw)


def test_a_rejection_is_captured_as_an_override_and_an_approval_is_not(eng, project):
    approved, rejected = _decision(eng, project), _decision(eng, project, "Merge it?")
    decide(eng, approved, "approve")
    decide(eng, rejected, "reject", note="tests are red")
    (example,) = _examples(eng, project)
    assert example["decision_id"] == rejected and example["source"] == "override"
    assert example["outcome"] == "reject" and example["note"] == "tests are red"
    assert example["action"] == "review_deployment"
    assert example["snapshot"]["question"] == "Merge it?" and example["decided_by"] == "ceo"


def test_a_revision_and_a_forced_approval_are_overrides(eng, project):
    import inspect

    from boss_engine.services.decisions import DecisionService

    # The transition calls the capture on every override path (revise needs an action whose
    # follow-on reads the note, so its arm is checked in the source and driven directly here).
    body = inspect.getsource(DecisionService.transition)
    for arm in ('action="reject"', 'action="revise"', 'action="approve"'):
        assert f"capture_override(connection, refreshed, {arm}" in body
    # Distinct questions: an identical open decision is deduplicated into one.
    revised, forced = _decision(eng, project, "Ship A?"), _decision(eng, project, "Ship B?")
    with eng.begin() as c:
        rows = {d: dict(c.execute(text("SELECT * FROM decisions WHERE id = :d"),
                                  {"d": d}).mappings().one()) for d in (revised, forced)}
        dx.capture_override(c, rows[revised], action="revise", actor="ceo",
                            note="deploy to staging first")
        assert dx.capture_override(c, rows[forced], action="approve", actor="ceo",
                                   note=None) is None             # a plain approval is not one
        dx.capture_override(c, rows[forced], action="approve", actor="ceo",
                            note="hotfix, checks flaky", force=True)
    got = {e["decision_id"]: e for e in _examples(eng, project)}
    assert (got[revised]["outcome"], got[revised]["note"]) == ("revise",
                                                               "deploy to staging first")
    assert got[forced]["outcome"] == "approve_forced"


def test_save_as_example_takes_an_answered_decision_with_a_label(eng, project):
    pending, answered = _decision(eng, project, "Ship A?"), _decision(eng, project, "Ship B?")
    decide(eng, answered, "approve", note="looks right")
    with eng.begin() as c:
        with pytest.raises(dx.ExampleError, match="only an answered decision"):
            dx.save(c, project_id=project, decision_id=pending, actor="ceo")
        saved = dx.save(c, project_id=project, decision_id=answered, actor="ceo",
                        label="good call")
        again = dx.save(c, project_id=project, decision_id=answered, actor="ceo")
    assert (saved["source"], saved["outcome"], saved["label"]) == ("saved", "approve",
                                                                    "good call")
    assert again["id"] == saved["id"] and again["label"] == "good call"   # one row, label kept
    assert _examples(eng, project, source="saved")[0]["note"] == "looks right"


def test_the_export_is_anonymized_jsonl(eng, project):
    d = _decision(eng, project, f"Deploy {project} for alice@example.com?")
    decide(eng, d, "reject", note="call me on +1 415 555 0100")
    with eng.begin() as c:
        out = dx.export_jsonl(c, project)
    lines = [json.loads(line) for line in out.strip().splitlines()]
    assert len(lines) == 1
    record = lines[0]
    blob = json.dumps(record)
    for leaked in (SECRET, "abc123secretvalue", "alice@example.com", project, "crun_123",
                   "415 555 0100"):
        assert leaked not in blob, leaked
    assert {record["decided_by"], record["context"]["requested_by"]} == {"person_1", "person_2"}
    assert record["context"]["api_key"]["$secret_placeholder"]["value"] is None
    assert record["outcome"] == "reject" and record["action"] == "review_deployment"
    assert record["context"]["target"] == "production"


def test_a_capture_failure_never_fails_the_answer(eng, project, monkeypatch):
    d = _decision(eng, project)
    monkeypatch.setattr(dx, "_insert", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    row = decide(eng, d, "reject")
    assert row["status"] == "rejected"
    with eng.begin() as c:
        assert c.execute(text("SELECT count(*) FROM decision_examples WHERE decision_id = :d"),
                         {"d": d}).scalar() == 0
