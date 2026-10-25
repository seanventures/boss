"""Every self-fix script of the scripted conformance engine ends in the same BOSS state
(BOSSAPP-571, spec §9, R13). BOSS — never the model — decides the confidence, the claim, red and
green, every gate, the tier and the card; the engine only writes a diagnosis, a test and a diff.
So the flow is the same on every engine, and these runs prove it with no model at all.

Runs the REAL pipeline (self_fix_build.Build) on a BOSS-shaped git repository with a real bug,
with BOSS's own test runner (pytest subprocesses on the workspace), the real gates and the real
database.
"""
from __future__ import annotations

import json

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


ADDED_TEST = ("from boss_engine.services import widget\n\n\n"
              "def test_double_of_two_stays_four():\n"
              "    assert widget.double(2) == 4, \"widget.double(2) is still 4\"\n")


def test_a_finding_that_asks_for_a_test_keeps_the_red_test_and_adds_one(harness):
    """The real-engine Replay A, attempt 3, round 2: the security seat asked for "a regression
    covering this combination" and the fix stage, which may not write tests, wrote one. Now the
    round goes to the TEST stage: the red test stays as it was, the asked-for test is added (it
    may pass on the base: it pins what must not change), then the fix is written again; both
    tests are proven red-then-green together."""
    from boss_engine.services import scripted_engine

    added = harness.fixture / "tests_added" / "engine" / "tests"
    added.mkdir(parents=True, exist_ok=True)
    (added / "test_a_widget_keeps_two.py.txt").write_text(ADDED_TEST)
    row = _build(harness, "review_asks_for_test")["row"]
    assert row["status"] == sf.AWAITING_APPROVAL, (row["refusal"], row["refusal_detail"])
    assert row["repair_rounds"] == 1
    files = row["red_green"]["test_files"]
    assert set(files) == {"engine/tests/test_a_widget_doubles.py",
                          "engine/tests/test_a_widget_keeps_two.py"}, files
    assert row["red_green"]["added"] == ["engine/tests/test_a_widget_keeps_two.py"]
    assert row["gates"]["red_then_green"]["ok"]
    writes = [c for c in scripted_engine.run_state(row["id"]).calls
              if c["stage"] == "self_fix_write_test"]
    assert len(writes) == 2 and "KEEP these red tests" in writes[1]["prompt_head"]
    assert "add a regression covering this combination" in writes[1]["prompt_head"]


def test_the_fix_stage_never_writes_a_test_file(harness):
    row = _build(harness, "fix_writes_test")["row"]
    assert row["status"] == sf.AWAITING_APPROVAL, (row["refusal"], row["refusal_detail"])
    assert not any(p["path"] == "engine/tests/test_written_by_the_fix_stage.py"
                   for p in row["paths"])
    with harness.eng.begin() as c:
        said = [r[0] for r in c.execute(text(
            "SELECT data->>'sentence' FROM events WHERE event_type = 'self_fix.progress' "
            "AND data->>'self_fix_id' = :i"), {"i": row["id"]}).all()]
    assert any("only the test stage adds tests" in (s or "") for s in said), said


def test_a_refused_build_keeps_its_diff_and_the_retry_thinks_with_it(harness):
    """A refused build's diff stays on the record (masked, secrets redacted). The next self-fix of
    the same cause class gets the earlier attempts as CONTEXT in every Mender stage: the diff,
    the gates that failed, and the reviewers' findings word for word. It is not turned into rules."""
    from boss_engine.services import scripted_engine

    first = _build(harness, "out_of_scope")["row"]
    assert first["status"] == sf.REFUSED and first["refusal"] == "scope"
    kept = (first["review"] or {}).get("attempt") or {}
    assert "engine/boss_engine/services/unrelated.py" in kept.get("diff", ""), kept
    assert first["diff_stat"]["files"], "the refused diff's size is on the record too"

    second = _build(harness, "good_fix")["row"]
    assert second["status"] == sf.AWAITING_APPROVAL, (second["refusal"], second["refusal_detail"])
    calls = scripted_engine.run_state(second["id"]).calls
    for stage in ("self_fix_diagnose", "self_fix_write_test", "self_fix_implement"):
        heads = [c["prompt_head"] for c in calls if c["stage"] == stage]
        assert heads, stage
    from boss_engine.services import self_fix_build

    with harness.eng.begin() as c:
        history = self_fix_build.earlier_attempts(c, second)
    assert [h["self_fix"] for h in history] == [first["id"]]
    assert history[0]["refusal"] == "scope" and "unrelated.py" in history[0]["diff"]
    assert "scope_matches_diagnosis" in history[0]["gates_failed"]
    context = self_fix_build.earlier_context(history)
    assert "data, not instructions" in context and "not a checklist" in context
    prompt = self_fix_build.implement_prompt(second, second["diagnosis"], red_output="F",
                                             history=context)
    assert first["id"] in prompt and "unrelated.py" in prompt
    with harness.eng.begin() as c:
        said = [r[0] for r in c.execute(text(
            "SELECT data->>'sentence' FROM events WHERE event_type = 'self_fix.progress' "
            "AND data->>'self_fix_id' = :i"), {"i": second["id"]}).all()]
    assert any("reading 1 earlier attempt" in (s or "") for s in said), said


def test_earlier_findings_reach_every_mender_stage_word_for_word(harness):
    """Seeded: an earlier refused attempt whose reviewers held a finding. Its words reach the
    diagnose, write-test and fix prompts of the retry."""
    from boss_engine.services import scripted_engine

    first = _build(harness, "out_of_scope")["row"]
    finding = ("MAJOR engine/boss_engine/services/widget.py:4 — a label carrying a newline "
               "passes the guard")
    with harness.eng.begin() as c:
        c.execute(text("UPDATE self_fixes SET review = review || CAST(:r AS JSONB) WHERE id = :i"),
                  {"i": first["id"], "r": json.dumps({"seats": {"security_check": {
                      "verdict": "FINDINGS",
                      "findings": [{"severity": "MAJOR", "what": finding.split(" ", 1)[1]}]}}})})
    second = _build(harness, "good_fix")["row"]
    calls = scripted_engine.run_state(second["id"]).calls
    for stage in ("self_fix_diagnose", "self_fix_write_test", "self_fix_implement"):
        prompt = next(c["full_prompt"] for c in calls if c["stage"] == stage)
        assert "EARLIER ATTEMPTS AT THIS CAUSE" in prompt, stage
        assert finding in prompt, stage


def test_the_same_script_ends_the_same_way_twice(harness):
    first = _build(harness, "good_fix")["row"]
    with harness.eng.begin() as c:
        sf.cancel(c, first["id"], actor="test", why="again")
    second = _build(harness, "good_fix")["row"]
    assert first["status"] == second["status"] == sf.AWAITING_APPROVAL
    assert first["fix_tree_sha"] == second["fix_tree_sha"], "same script, same tree"


def test_a_test_the_fixer_says_cannot_pass_is_written_again_from_the_base(harness):
    """brec_eddfaf96 (sfx_75665f42) and brec_c15618 (sfx_8f047bbb): the fixes were right and the
    red tests could never pass (they asserted through the identity gate). The fixer may not edit
    a test, so two repair rounds were spent and the fix was refused 'tests'. A fixer that answers
    TEST_WRONG sends the round back to RED, its reason reaching the test stage word for word."""
    from boss_engine.services import scripted_engine

    row = _build(harness, "test_wrong")["row"]
    assert row["status"] == sf.AWAITING_APPROVAL, (row["refusal"], row["refusal_detail"])
    assert row["repair_rounds"] == 1
    calls = scripted_engine.run_state(row["id"]).calls
    writes = [c for c in calls if c["stage"] == "self_fix_write_test"]
    assert len(writes) == 2, [c["stage"] for c in calls]
    assert "the test asserts ok=True through the identity gate" in writes[1]["prompt_head"]


def test_mender_is_told_how_boss_tests_reach_a_tool():
    """The harness fact both failed tests were missing, in every write-test prompt."""
    prompt = self_fix_build.write_test_prompt(
        {"evidence": {}, "kind": "platform_gap"},
        {"hypothesis": "h", "location": {"file": "engine/boss_engine/tools/builtin.py",
                                         "function": "propose_action"}})
    assert "identity_unavailable" in prompt and ".handler(ctx" in prompt
    fix = self_fix_build.implement_prompt(
        {"evidence": {}, "kind": "platform_gap"},
        {"hypothesis": "h", "location": {"file": "engine/boss_engine/tools/builtin.py"}},
        red_output="F")
    assert "TEST_WRONG:" in fix
    assert self_fix_build.test_wrong("Changed nothing.\nTEST_WRONG: it needs a person") == \
        "it needs a person"
    assert self_fix_build.test_wrong("Implemented the fix: applied fix.patch") is None
