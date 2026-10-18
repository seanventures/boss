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


def test_a_fix_that_touched_protected_code_is_undone_and_asked_once_more(harness):
    """The real-engine Replay A finding (second run): the diagnosis and the red test were right,
    and the fix ALSO edited the recovery classifier. The protected edit is undone and Mender is
    asked once more; a fix that edits protected code again is refused (script protected_path)."""
    row = _build(harness, "protected_once")["row"]
    assert row["status"] == sf.AWAITING_APPROVAL, (row["refusal"], row["refusal_detail"])
    assert row["repair_rounds"] == 1
    assert not any(p["path"].startswith("engine/boss_engine/identity/") for p in row["paths"])
    with harness.eng.begin() as c:
        said = [r[0] for r in c.execute(text(
            "SELECT data->>'sentence' FROM events WHERE event_type = 'self_fix.progress' "
            "AND data->>'self_fix_id' = :i"), {"i": row["id"]}).all()]
    assert any("that edit was undone" in (s or "") for s in said), said


def test_the_diagnosis_prompt_names_what_is_protected():
    prompt = self_fix_build.diagnose_prompt({"signature": "deploy:port_held:8000",
                                             "evidence": {}})
    assert "PROTECTED" in prompt and "engine/boss_engine/services/step_recovery.py" in prompt
    assert "OUTSIDE" in prompt
    fix = self_fix_build.implement_prompt({}, {"hypothesis": "h", "location": {}},
                                          red_output="F")
    assert "PROTECTED" in fix, "the implementer is told too"


def test_open_review_findings_get_one_repair_round(harness):
    row = _build(harness, "review_blocker")["row"]
    assert row["status"] == sf.AWAITING_APPROVAL and row["repair_rounds"] == 1


def test_a_finding_about_the_new_test_writes_the_test_again_from_the_base(harness):
    """The real-engine Replay A, third run: a reviewer's MAJOR was about Mender's own test (it
    drove a path the failing step never takes), which a fix that may not edit its test can never
    answer. The round goes back to RED: the test is written again on the base (red again, every
    gate again), then the fix; the finding reached Mender word for word."""
    from boss_engine.services import scripted_engine

    row = _build(harness, "review_test_finding")["row"]
    assert row["status"] == sf.AWAITING_APPROVAL, (row["refusal"], row["refusal_detail"])
    assert row["repair_rounds"] == 1
    with harness.eng.begin() as c:
        said = [r[0] for r in c.execute(text(
            "SELECT data->>'sentence' FROM events WHERE event_type = 'self_fix.progress' "
            "AND data->>'self_fix_id' = :i"), {"i": row["id"]}).all()]
    assert any("writing it again from BOSS as it is" in (s or "") for s in said), said
    calls = scripted_engine.run_state(row["id"]).calls
    rewrites = [c for c in calls if c["stage"] == "self_fix_write_test"]
    assert len(rewrites) == 2, [c["stage"] for c in calls]
    assert "test exercises a helper,\nnot the path the failing step takes." in \
        rewrites[1]["prompt_head"], "the finding reached Mender word for word"
    assert row["review"]["lanes"]["diagnose"]["engine"] == "scripted"


def test_a_secret_shaped_fixture_value_is_said_at_red_while_the_test_can_change(harness):
    """The real-engine Replay A, fourth run: a test driving apply_on_host passed
    apply_token="tok_new", and the final secret gate (rightly final) refused the whole self-fix.
    The scan now also runs at RED, where Mender is told and the test can still change."""
    from boss_engine.services import scripted_engine

    row = _build(harness, "secret_in_test")["row"]
    assert row["status"] == sf.AWAITING_APPROVAL, (row["refusal"], row["refusal_detail"])
    assert row["gates"]["no_committed_secrets"]["ok"]
    writes = [c for c in scripted_engine.run_state(row["id"]).calls
              if c["stage"] == "self_fix_write_test"]
    assert len(writes) == 2 and "secret-shaped values" in writes[1]["prompt_head"]


def test_the_same_script_ends_the_same_way_twice(harness):
    first = _build(harness, "good_fix")["row"]
    with harness.eng.begin() as c:
        sf.cancel(c, first["id"], actor="test", why="again")
    second = _build(harness, "good_fix")["row"]
    assert first["status"] == second["status"] == sf.AWAITING_APPROVAL
    assert first["fix_tree_sha"] == second["fix_tree_sha"], "same script, same tree"
