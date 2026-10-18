"""Every self-fix script of the scripted conformance engine ends in the same BOSS state
(BOSSAPP-571, spec §9, R13). BOSS — never the model — decides the confidence, the claim, red and
green, every gate, the tier and the card; the engine only writes a diagnosis, a test and a diff.
So the flow is the same on every engine, and these runs prove it with no model at all.

Runs the REAL pipeline (self_fix_build.Build) on a BOSS-shaped git repository with a real bug,
with BOSS's own test runner (pytest subprocesses on the workspace), the real gates and the real
database.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.services import boss_recommendations
from boss_engine.services import self_fix as sf
from boss_engine.services import self_fix_build
from boss_engine.services import self_fix_install as sfi

from ._self_fix_support import self_fix_on  # noqa: F401 - fixture used by name
from ._self_fix_support import harness  # noqa: F401
from .test_workflow_capability_runner import eng, project  # noqa: F401


def _build(h, script: str) -> dict:
    h.script(script)
    sfx = h.propose()
    assert sfx["outcome"] == "started", sfx
    out = self_fix_build.Build(engine=h.eng, settings=h.settings, sfx_id=sfx["self_fix"]["id"],
                               source=h.mirror).run()
    with h.eng.begin() as c:
        return {**out, "row": sf.get(c, sfx["self_fix"]["id"])}


def test_a_good_fix_reaches_the_card_red_then_green_with_every_gate(harness):
    out = _build(harness, "good_fix")
    row = out["row"]
    assert row["status"] == sf.AWAITING_APPROVAL, out
    assert row["location"] == "engine/boss_engine/services/widget.py::double"
    assert row["red_green"]["base"]["exit_code"] == 1 and row["red_green"]["head"]["exit_code"] == 0
    assert {k for k, v in row["gates"].items() if v.get("ok")} >= {
        "protected_paths", "tests_not_weakened", "scope_matches_diagnosis",
        "no_new_dependencies", "size", "no_committed_secrets", "red_then_green",
        "affected_suites", "p0_tier"}
    assert row["tier"] == 2 and row["flags"] == []
    assert row["diagnosis"]["rejected"], "the recommendation's guess is recorded as rejected"
    assert {"source:boss-agent", "boss:self-fix", "engine:scripted"} <= set(row["pr"]["labels"])
    assert row["pr"]["trailers"]["BOSS-Self-Fix"] == row["id"]
    assert row["fix_commit_sha"] and row["fix_tree_sha"] and row["branch"].startswith(
        "boss/self-fix/")
    with harness.eng.begin() as c:
        decision = c.execute(text("SELECT status, risk_tier, recommendation FROM decisions "
                                  "WHERE id = :i"), {"i": row["decision_id"]}).mappings().first()
        bundle = c.execute(text("SELECT bundle FROM self_fixes WHERE id = :i"),
                           {"i": row["id"]}).scalar()
    assert decision["status"] == "pending" and decision["risk_tier"] == 2
    assert decision["recommendation"]["action"] == sfi.ACTION
    assert decision["recommendation"]["action_preview"]["content_hash"] == sfi.content_hash(row)
    assert bundle, "the fix travels as a git bundle"


@pytest.mark.parametrize("script, refusal", [
    ("weakens_test", "tests_weakened"),
    ("protected_path", "protected_path"),
    ("protected_diagnosis", "protected_path"),
    ("injection_follower", "protected_path"),
    ("out_of_scope", "scope"),
    ("no_red", "no_reproduction"),
])
def test_each_fault_ends_in_its_refusal(harness, script, refusal):
    out = _build(harness, script)
    row = out["row"]
    assert row["status"] == sf.REFUSED and row["refusal"] == refusal, (row["refusal"],
                                                                       row["refusal_detail"])
    assert row["decision_id"] is None, "a refused self-fix never reaches a person's card"
    with harness.eng.begin() as c:
        rec = boss_recommendations.list_recommendations(c, harness.project)[0]
        claim = c.execute(text("SELECT 1 FROM self_fix_claims WHERE self_fix_id = :i"),
                          {"i": row["id"]}).first()
    assert rec["status"] == "open" and rec["attempts"][0]["outcome"] == "refused"
    assert claim is None, "the cause is free for the next attempt"


def test_a_protected_diagnosis_stops_before_any_code_is_written(harness):
    row = _build(harness, "protected_diagnosis")["row"]
    assert not (row.get("red_green") or {}).get("tests")
    assert "never changes by itself" in row["refusal_detail"]


def test_a_protected_first_answer_is_asked_again_for_a_fix_outside_it(harness):
    """The real-engine Replay A finding (2026-10-17): a real model placed the held-port fix in
    the recovery classifier (protected) and the self-fix ended, though a fix outside it existed.
    Mender is now told what is protected and asked once more; the protected answer is kept as a
    rejected hypothesis, and only a second protected answer refuses."""
    row = _build(harness, "protected_then_outside")["row"]
    assert row["status"] == sf.AWAITING_APPROVAL, (row["refusal"], row["refusal_detail"])
    assert row["location"] == "engine/boss_engine/services/widget.py::double"
    rejected = row["diagnosis"]["rejected"]
    assert any("fixed outside the protected code" in r["why"] for r in rejected), rejected
    with harness.eng.begin() as c:
        said = [r[0] for r in c.execute(text(
            "SELECT data->>'sentence' FROM events WHERE event_type = 'self_fix.progress' "
            "AND data->>'self_fix_id' = :i"), {"i": row["id"]}).all()]
    assert any("looking for a fix outside" in (s or "") for s in said), said


def test_the_diagnosis_prompt_names_what_is_protected():
    prompt = self_fix_build.diagnose_prompt({"signature": "deploy:port_held:8000",
                                             "evidence": {}})
    assert "PROTECTED" in prompt and "engine/boss_engine/services/step_recovery.py" in prompt
    assert "OUTSIDE" in prompt


def test_open_review_findings_get_one_repair_round(harness):
    row = _build(harness, "review_blocker")["row"]
    assert row["status"] == sf.AWAITING_APPROVAL and row["repair_rounds"] == 1


def test_the_same_script_ends_the_same_way_twice(harness):
    first = _build(harness, "good_fix")["row"]
    with harness.eng.begin() as c:
        sf.cancel(c, first["id"], actor="test", why="again")
    second = _build(harness, "good_fix")["row"]
    assert first["status"] == second["status"] == sf.AWAITING_APPROVAL
    assert first["fix_tree_sha"] == second["fix_tree_sha"], "same script, same tree"
