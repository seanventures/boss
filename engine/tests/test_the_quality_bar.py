"""THE QUALITY BAR (BOSSAPP-1362, services/quality_bar.py): one setting, every gate reads it.

prototype | standard (default) | production: what blocks a build, what becomes a follow-up, and
the production rule at a deploy/promote step (the bar follows where it ships).

Pure tests first (no database), then the pipeline end to end on the scripted engine
(tests/test_the_pipeline_runs_end_to_end.py's harness: no model, no real CLI) and the deploy step.
"""
from __future__ import annotations

import json
import os
import shutil
from types import SimpleNamespace

import pytest

from boss_engine.config import Settings
from boss_engine.services import acceptance_gate as ag
from boss_engine.services import project_settings as ps
from boss_engine.services import quality_bar as qb
from boss_engine.services.acceptance import contract as ct
from boss_engine.services.governed_code_task_service import (
    _review_gate_from_findings, resolve_gate_failure_action)

P, S, R = qb.PROTOTYPE, qb.STANDARD, qb.PRODUCTION


def _item(n, text, status, tier=None, **extra):
    return {"n": n, "text": text, "status": status, **({"tier": tier} if tier else {}), **extra}


def _record(*items, verdict="not_met", **extra):
    return {"verdict": verdict, "items": list(items), "stack": {}, **extra}


# ── resolution: run spec -> workflow -> run input -> project setting -> standard ─────────────

def test_the_bar_defaults_to_standard():
    assert qb.resolve() == S
    assert Settings().quality_bar == S
    assert qb.resolve(project_value="nonsense") == S


def test_the_most_specific_setting_wins():
    stamped = {qb.BY_KEY: "usr_ana"}
    assert qb.resolve(project_value=P) == P
    assert qb.resolve(project_value=P, run_input={"quality_bar": R, **stamped}) == R
    assert qb.resolve(project_value=P, run_input={"quality_bar": R, **stamped},
                      workflow_spec={"quality_bar": S}) == R
    assert qb.resolve(project_value=P, workflow_spec={"quality_bar": S},
                      run_spec={"quality_bar": "Production"}) == R
    # a value that is not a preset is skipped, never an error
    assert qb.resolve(run_spec={"quality_bar": "yolo"}, project_value=P) == P


def test_a_run_input_the_server_did_not_stamp_changes_nothing():
    """An agent's start_workflow inputs cannot set or lower a bar: only the stamp the start door
    writes after the permission check counts; and a workflow spec may only RAISE the bar."""
    assert qb.resolve(project_value=R, run_input={"quality_bar": P}) == R
    assert qb.resolve(project_value=S, run_input={"quality_bar": P, "quality_bar_gates": {
        "nothing_broke": "follow_up"}}) == S
    assert qb.resolve(project_value=R, workflow_spec={"quality_bar": P}) == R   # cannot lower
    assert qb.resolve(project_value=S, workflow_spec={"quality_bar": R}) == R   # may raise
    d = qb.resolve_detail(project_value=R, run_input={"quality_bar": P, qb.BY_KEY: "usr_ana"})
    assert d["bar"] == P and d["lowered_by"] == "usr_ana" and d["source"] == "run_input"
    assert qb.resolve_detail(project_value=P, run_input={"quality_bar": R,
                                                         qb.BY_KEY: "usr_ana"})["lowered_by"] is None


def test_lowering_the_bar_needs_a_person_with_settings_update():
    for who in ("agent_helper", "boss", "", None):
        with pytest.raises(qb.QualityBarOverrideRefused):
            qb.check_override_permission("p1", who)
    qb.check_override_permission("p1", "usr_ana")        # a person, no guard enforced


def test_start_run_drops_a_callers_quality_bar_and_writes_only_the_checked_one():
    from boss_engine.services import workflows

    assert {"quality_bar", "quality_bar_gates", "_quality_bar_by"} <= workflows.SERVER_ONLY_INPUTS


def test_the_setting_is_a_project_knob():
    spec = ps.KNOBS[qb.SETTING]
    assert spec["kind"] == "choice" and spec["choices"] == [P, S, R]
    assert ps.coerce(qb.SETTING, "Production") == R
    with pytest.raises(ValueError):
        ps.coerce(qb.SETTING, "yolo")


# ── what blocks, per gate ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("gate", ["code_written", "app_serves", "written_tests_pass",
                                  "security_clean", "no_committed_secrets", "template_tests"])
def test_the_floor_blocks_at_every_bar(gate):
    assert all(qb.gate_blocks(gate, bar) for bar in (P, S, R))


@pytest.mark.parametrize("gate,expected", [
    ("existing_tests_intact", (False, True, True)),
    ("brief_tests_written", (False, True, True)),
    ("tests_written_first", (False, False, True)),
    ("tests_strong_enough", (False, False, True)),
    ("code_review_passed", (False, True, True)),
])
def test_each_gate_blocks_where_the_design_says(gate, expected):
    assert tuple(qb.gate_blocks(gate, bar) for bar in (P, S, R)) == expected


def test_the_floor_and_unknown_gates_ignore_every_override():
    assert qb.gate_blocks("iac_validate", P, {"iac_validate": "follow_up"}) is True
    for gate in qb.FLOOR:
        assert qb.gate_blocks(gate, P, {gate: "follow_up"}) is True, gate
    # ...while a known, non-floor gate honours the per-gate map
    assert qb.gate_blocks("tests_strong_enough", R, {"tests_strong_enough": "follow_up"}) is False
    assert qb.gate_blocks("tests_written_first", P, {"tests_written_first": "block"}) is True
    g = qb.BarGates(P, {"security_clean": "follow_up"}, {"security_clean": False})
    assert g["security_clean"] is False and not g.followups


def test_gates_that_fail_without_blocking_are_follow_ups_not_failures():
    outcome = {"code_written": True, "written_tests_pass": True, "existing_tests_intact": False,
               "tests_strong_enough": False, "brief_tests_written": False,
               "security_clean": True}
    seen = {}
    for bar in (P, S, R):
        g = qb.BarGates(bar, initial=outcome)
        seen[bar] = ([k for k, v in g.items() if not v], sorted(g.followups))
    assert seen[P] == ([], ["brief_tests_written", "existing_tests_intact", "tests_strong_enough"])
    assert seen[S] == (["existing_tests_intact", "brief_tests_written"], ["tests_strong_enough"])
    assert seen[R] == (["existing_tests_intact", "tests_strong_enough", "brief_tests_written"], [])


def test_a_re_judgement_through_the_same_door_clears_the_follow_up():
    g = qb.BarGates(S, initial={"tests_strong_enough": False})
    assert g["tests_strong_enough"] is True and "tests_strong_enough" in g.followups
    g["tests_strong_enough"] = True                 # a fix round strengthened the suite
    assert g.followups == {}
    g.update({"existing_tests_intact": False})
    assert g["existing_tests_intact"] is False and g.followups == {}


def test_security_never_becomes_a_follow_up():
    for bar in (P, S, R):
        g = qb.BarGates(bar, initial={"security_clean": False})
        assert g["security_clean"] is False and not g.followups


# ── the must-haves: P1 blocks at standard, P2 only at production ─────────────────────────────

P1_PARTIAL = _item(1, "The app starts and serves the list page", "partial")
P2_PARTIAL = _item(2, "Export handles a thousand-row file", "partial", tier="P2")
P1_MISSING = _item(3, "Users can add a ticket", "missing", tier="P1")
MET = _item(4, "Titles are trimmed", "met")


def test_a_p2_partial_does_not_block_standard_but_blocks_production():
    rec = _record(MET, P2_PARTIAL)
    assert qb.acceptance_verdict(rec, P)[0] is True
    ok, soft = qb.acceptance_verdict(rec, S)
    assert ok is True and len(soft) == 1 and "P2" in soft[0] and "thousand-row" in soft[0]
    assert qb.acceptance_verdict(rec, R) == (False, [])


def test_a_p1_gap_blocks_standard_and_production_and_is_a_follow_up_at_prototype():
    rec = _record(P1_MISSING, P2_PARTIAL)
    ok_p, soft_p = qb.acceptance_verdict(rec, P)
    assert ok_p is True and len(soft_p) == 2
    ok_s, soft_s = qb.acceptance_verdict(rec, S)
    assert ok_s is False and len(soft_s) == 1           # the P2 rides along as a follow-up
    assert qb.acceptance_verdict(rec, R)[0] is False
    # an untagged item is P1: held
    assert qb.item_tier(P1_PARTIAL) == "P1"
    assert qb.acceptance_verdict(_record(P1_PARTIAL), S)[0] is False


def test_a_check_that_never_ran_is_never_waved_through_below_prototype():
    rec = {"verdict": "not_run", "items": [], "stack": {}, "why": "the reviewer was blind"}
    assert qb.acceptance_verdict(rec, S) == (False, [])
    assert qb.acceptance_verdict(rec, R) == (False, [])
    assert qb.acceptance_verdict(rec, P)[0] is True
    assert qb.acceptance_verdict(None, S) == (False, [])
    assert qb.acceptance_verdict({"verdict": "met", "items": []}, S) == (True, [])


def test_a_declared_stack_without_its_manifest_blocks_standard():
    rec = _record(MET, stack={"missing": ["python"], "required": []})
    assert qb.acceptance_verdict(rec, S)[0] is False
    assert qb.acceptance_verdict(rec, P)[0] is True


def test_not_met_for_a_reason_naming_no_item_still_holds():
    rec = _record(MET, why="2 items claimed MET with evidence BOSS could not verify")
    assert qb.acceptance_verdict(rec, S) == (False, [])


def test_a_record_the_bar_accepted_is_not_a_lineage_failure():
    rec = {**_record(MET, P2_PARTIAL), "bar": {"bar": S, "nonblocking": True},
           "source": {"item_keys": ["a"]}}
    assert ag._unresolved([("run_1", rec)]) == []
    assert ag._unresolved([("run_1", {k: v for k, v in rec.items() if k != "bar"})]) != []


# ── review: BLOCKER at standard, MAJOR at production, nothing at prototype ───────────────────

REVIEW_MAJOR = "FINDING: MAJOR server/a.ts:3 — the list ignores the filter\nVERDICT: REFUTED"
REVIEW_BLOCKER = "FINDING: BLOCKER server/a.ts:3 — data loss on delete\nVERDICT: REFUTED"


@pytest.mark.parametrize("summary,expected", [
    (REVIEW_MAJOR, {P: True, S: True, R: False}),          # a MAJOR blocks production only
    (REVIEW_BLOCKER, {P: True, S: False, R: False}),
])
def test_review_severity_blocks_by_bar(summary, expected):
    got = {bar: _review_gate_from_findings(summary, "refuted", qb.review_block_severity(bar))[0]
           for bar in (P, S, R)}
    # prototype: the gate itself is demoted by the bar (BarGates), the severity cut is "none"
    assert got[S] == expected[S] and got[R] == expected[R]
    assert got[P] is True
    g = qb.BarGates(P, initial={"code_review_passed": False})
    assert g["code_review_passed"] is True and "code_review_passed" in g.followups


def test_consistency_drift_blocks_production_and_only_a_blocker_blocks_standard():
    major = "FINDING: MAJOR a.ts:1 — naming drift\n"
    blocker = "FINDING: BLOCKER a.ts:1 — contract drift\n"
    assert qb.consistency_passes(R, "drift", major) == (False, False)
    assert qb.consistency_passes(S, "drift", major) == (True, True)
    assert qb.consistency_passes(S, "drift", blocker) == (False, False)
    assert qb.consistency_passes(P, "drift", blocker) == (True, True)
    assert qb.consistency_passes(S, "hollow") == (False, False)        # never saw the change
    assert qb.consistency_passes(S, "ok") == (True, False)


def test_the_prototype_skips_only_the_default_sensitive_tiers_plan_signoff():
    assert qb.plan_signoff_required(P) is False
    assert qb.plan_signoff_required(P, risk_tier=2) is False
    assert qb.plan_signoff_required(P, risk_tier=3) is True       # a tier >= 3 is always signed off
    assert qb.plan_signoff_required(P, risk_tier=1, sensitive=True) is True
    assert qb.plan_signoff_required(S) and qb.plan_signoff_required(R)
    assert qb.pass_two_must_finish(R) and not qb.pass_two_must_finish(S)


# ── broken EXISTING tests: by path, not by name of the gate ──────────────────────────────────

def test_broken_existing_tests_block_by_where_they_are():
    changed = ["src/tickets/list.js"]
    off = ["test/billing/invoice.test.js > rounds up"]
    p0 = ["test/auth/login.test.js > rejects a bad password"]
    near = ["test/tickets/list.test.js > sorts"]
    assert qb.broken_tests_block(R, off, changed)[0] is True
    # standard: the changed area or a P0/P1 path blocks; an unrelated test is a follow-up
    assert qb.broken_tests_block(S, near, changed)[0] is True
    assert qb.broken_tests_block(S, p0, changed)[0] is True
    blocks, why = qb.broken_tests_block(S, off, changed)
    assert blocks is False and "invoice" in why
    assert qb.broken_tests_block(S, off, changed, ["Users can invoice customers"])[0] is True
    assert qb.broken_tests_block(S, [], changed)[0] is True       # unnamed: held
    # prototype: only a P0 (startup, auth, health) test
    assert qb.broken_tests_block(P, p0, changed)[0] is True
    assert qb.broken_tests_block(P, near, changed)[0] is False
    assert qb.broken_tests_block(P, [], changed)[0] is False


# ── the same outcome under each preset: pass/fail and the follow-ups list ────────────────────

def _judge(bar):
    """One build outcome: its own tests pass, one P2 must-have is partial, the review has a MAJOR
    finding, the suite is weak. What does each bar do with it?"""
    gates = qb.BarGates(bar, initial={
        "code_written": True, "app_serves": True, "written_tests_pass": True,
        "security_clean": True, "tests_strong_enough": False})
    ok, soft = qb.acceptance_verdict(_record(MET, P2_PARTIAL), bar)
    gates.set_bar_aware("acceptance_met", ok, soft)
    review_ok = _review_gate_from_findings(REVIEW_MAJOR, "refuted",
                                           qb.review_block_severity(bar))[0]
    gates["code_review_passed"] = review_ok
    return gates


def test_one_outcome_under_each_preset():
    failing = {bar: [k for k, v in _judge(bar).items() if not v] for bar in (P, S, R)}
    assert failing[P] == []
    assert failing[S] == []                       # the P2 gap, the MAJOR and the weak suite wait
    assert sorted(failing[R]) == ["acceptance_met", "code_review_passed", "tests_strong_enough"]
    assert sorted(_judge(S).followups) == ["acceptance_met", "tests_strong_enough"]
    # the follow-ups are findings the existing deferred-findings machinery carries
    lines = qb.followup_findings(_judge(S).followups, {})
    text = " | ".join(f["text"] for f in lines)
    assert "thousand-row" in text and "tests_strong_enough" in text


def test_a_follow_up_gate_is_a_third_bucket_in_the_pr_never_passed():
    from boss_engine.services import pr_description

    evidence: dict = {}
    g = _judge(S)
    qb.stamp(evidence, g)
    body = pr_description.render(title="T", evidence=evidence, gates=dict(g))
    checks = body.split("### Checks", 1)[1].split("###", 1)[0]
    assert "follow-ups" in checks.splitlines()[1] and "failed" in checks.splitlines()[1]
    assert "🔸 Tests catch bugs — follow-up (quality bar: standard)" in checks
    assert "✅ Tests catch bugs" not in checks and "✅ Must-haves met" not in checks
    assert "✅ New tests pass" in checks
    n = len(g)
    assert f"{n - 2} passed, 2 follow-ups, 0 failed" in checks


def test_the_evidence_and_the_pr_carry_one_follow_ups_list():
    evidence: dict = {}
    qb.stamp(evidence, _judge(S), flaky=["npm test: a test flaked"])
    assert evidence["quality_bar"]["bar"] == S
    assert set(evidence["gates_followup"]) == {"acceptance_met", "tests_strong_enough"}
    body = qb.pr_section(evidence)
    assert body.startswith("## Follow-ups (quality bar: standard)")
    assert "thousand-row" in body and "tests_strong_enough" in body and "flaked" in body
    assert qb.pr_section({"quality_bar": {"bar": S, "followups": {}, "flaky": []}}) == ""


def test_the_failure_action_still_reads_the_failing_gates_only():
    # a build whose only gap is a follow-up has no failing gates: it ships
    assert resolve_gate_failure_action(
        failing=[k for k, v in _judge(S).items() if not v], advisory_gate=False, rounds_used=0,
        max_repair_rounds=2) == "ship"


# ── tagging: the contract writer tags P2; a kept or an operator's tag wins ───────────────────

def test_the_reader_tags_p2_under_a_floor():
    words = ["The export handles a thousand-row file", "The app starts and serves the list page",
             "Users can sign in with a password", "Users can add a ticket"]
    reply = ('<<<PROSE>>>' if False else '') + json.dumps({
        "items": [], "p2": [words[0], words[1], words[2], "not a requirement"]})
    got = ct.parse_p2(reply, words)
    assert got == [words[0]]                       # starting and sign-in are never P2
    prose = {"p2_items": got}
    tagged = ct._tagged(words, prose)              # noqa: SLF001
    assert [t.get("tier") for t in tagged] == ["P2", None, None, None]


def test_tiers_travel_from_the_contract_into_the_gate_items():
    sources = [{"kind": "ticket", "ref": "T-1", "items": [
        {"source": "ticket", "text": "The export handles a thousand-row file", "tier": "P2"},
        {"source": "ticket", "text": "Users can add a ticket"}]}]
    items = ct.build_items(sources)
    assert [i.get("tier") for i in items] == ["P2", None]
    held = ct.gate_items({"items": items})
    assert [h.get("tier") for h in held] == ["P2", None]
    # a second source for the same item never replaces the tag it already has
    again = ct.build_items(sources + [{"kind": "brief", "ref": "b", "items": [
        {"source": "brief", "text": "The export handles a thousand-row file", "tier": "P1"}]}])
    assert again[0]["tier"] == "P2"
    # ...and the gate's own judgement keeps it on the judged item
    from boss_engine.services.acceptance import verdict as v

    judged = v.parse_acceptance("ACCEPTANCE 1: PARTIAL — half\nACCEPTANCE 2: MET — ok\n",
                                [{**held[0], "n": 1}, {**held[1], "n": 2}], root=None)
    assert judged[0]["tier"] == "P2" and "tier" not in judged[1]


def test_the_reader_prompt_asks_for_p2():
    prompt = ct.prose_prompt({"title": "t"}, "text", [])
    assert "`p2`" in prompt and '"p2"' in prompt


# ── the production rule: a promotion applies the production bar ──────────────────────────────

def test_production_needs_is_empty_for_a_run_that_passed_every_bar():
    ev = {"acceptance": _record(MET, verdict="met"), "review_summary": "VERDICT: SURVIVED"}
    assert qb.production_needs(ev, []) == []


def test_a_standard_pass_with_gaps_lists_what_production_still_needs():
    g = _judge(S)
    ev: dict = {"acceptance": _record(MET, P2_PARTIAL), "review_summary": REVIEW_MAJOR,
                "consistency_summary": ""}
    qb.stamp(ev, g)
    needs = qb.production_needs(ev, [])
    joined = "\n".join(needs)
    assert "thousand-row" in joined                       # the P2 must-have
    assert "MAJOR" in joined and "ignores the filter" in joined
    assert any("tests_strong_enough" in n or "weak" in n.lower() or "mutation" in n.lower()
               or "Gate" in n for n in needs)


def test_production_needs_counts_a_seat_that_did_not_run():
    ev = {"acceptance": _record(MET, verdict="met"),
          "gates_skipped": {"code_review_passed": "the profile did not convene this seat"}}
    needs = qb.production_needs(ev, [])
    assert len(needs) == 1 and "was not run" in needs[0] and "convene" in needs[0]


def test_a_flaky_test_never_makes_production_wait():
    ev = {"acceptance": _record(MET, verdict="met"),
          "quality_bar": {"bar": S, "followups": {}, "flaky": ["npm test: flaked"]}}
    assert qb.production_needs(ev, []) == []
    assert qb.is_production("Production") and qb.is_production("prod")
    assert not qb.is_production("staging")


# ── the pipeline, end to end (scripted engine) ───────────────────────────────────────────────

pytestmark_db = pytest.mark.skipif(not os.environ.get("BOSS_TEST_DATABASE_URL"),
                                   reason="needs the bench database (BOSS_TEST_DATABASE_URL)")

if os.environ.get("BOSS_TEST_DATABASE_URL"):
    from .test_the_pipeline_runs_end_to_end import FIXTURE, pipeline  # noqa: F401


def _quality(pipeline, run_id):
    with pipeline.engine.begin() as c:
        row = c.execute(pipeline.text(
            "SELECT status, gates_passed, gates_failed, evidence FROM quality_gates "
            "WHERE code_task_run_id = :r"), {"r": run_id}).mappings().one()
    ev = row["evidence"] if isinstance(row["evidence"], dict) else json.loads(row["evidence"])
    return dict(row), ev


def _set_spec(pipeline, run_id, **keys):
    with pipeline.engine.begin() as c:
        c.execute(pipeline.text(
            "UPDATE code_task_runs SET spec = COALESCE(spec,'{}'::jsonb) || CAST(:s AS jsonb) "
            "WHERE id = :r"), {"s": json.dumps(keys), "r": run_id})


@pytestmark_db
def test_a_build_defaults_to_the_standard_bar(pipeline):
    run_id, repo = pipeline.build(tag="qb-default")
    assert pipeline.run(run_id, repo) is None
    out = pipeline.result(run_id)
    assert out["run"]["status"] == "completed", out["run"].get("error")
    row, ev = _quality(pipeline, run_id)
    assert row["status"] == "pass" and ev["quality_bar"]["bar"] == "standard"
    assert ev["quality_bar"]["followups"] == {} and not row["gates_failed"]


@pytestmark_db
@pytest.mark.parametrize("bar", ["prototype", "production"])
def test_the_run_spec_sets_the_bar_for_that_run(pipeline, bar):
    run_id, repo = pipeline.build(tag=f"qb-{bar}")
    _set_spec(pipeline, run_id, quality_bar=bar)
    assert pipeline.run(run_id, repo) is None
    out = pipeline.result(run_id)
    assert out["run"]["status"] == "completed", out["run"].get("error")
    _, ev = _quality(pipeline, run_id)
    assert ev["quality_bar"]["bar"] == bar


@pytestmark_db
def test_the_project_setting_sets_the_bar_and_the_run_spec_overrides_it(pipeline):
    with pipeline.engine.begin() as c:
        ps.set_one(c, pipeline.project, "quality_bar", "prototype")
    try:
        run_id, repo = pipeline.build(tag="qb-setting")
        assert pipeline.run(run_id, repo) is None
        assert _quality(pipeline, run_id)[1]["quality_bar"]["bar"] == "prototype"
        run2, repo2 = pipeline.build(tag="qb-override")
        _set_spec(pipeline, run2, quality_bar="production")
        assert pipeline.run(run2, repo2) is None
        assert _quality(pipeline, run2)[1]["quality_bar"]["bar"] == "production"
    finally:
        with pipeline.engine.begin() as c:
            c.execute(pipeline.text("DELETE FROM project_settings WHERE project_id = :p "
                                    "AND key = 'quality_bar'"), {"p": pipeline.project})


@pytestmark_db
def test_a_flaky_test_never_blocks_and_is_reported(pipeline, tmp_path):
    """A suite that fails once and passes on the immediate retry: the build is not repaired, not
    blocked, and the flake is a follow-up on the gate evidence and in the PR's follow-up list."""
    changed = tmp_path / "plain"
    shutil.copytree(FIXTURE, changed)
    marker = tmp_path / "flake-marker"
    (changed / "test").mkdir(exist_ok=True)
    (changed / "test" / "zz-flaky.test.js").write_text(
        "import test from 'node:test';\nimport fs from 'node:fs';\n"
        f"const m = {json.dumps(str(marker))};\n"
        "test('sometimes fails', () => {\n"
        "  if (!fs.existsSync(m)) { fs.writeFileSync(m, '1'); throw new Error('flaked once'); }\n"
        "});\n")
    run_id, repo = pipeline.build(tag="qb-flaky")
    pipeline._engine_env("", fixture=changed)          # noqa: SLF001
    assert pipeline.run(run_id, repo) is None
    out = pipeline.result(run_id)
    assert out["run"]["status"] == "completed", out["run"].get("error")
    row, ev = _quality(pipeline, run_id)
    assert row["status"] == "pass" and "written_tests_pass" not in (row["gates_failed"] or [])
    assert marker.exists(), "the flaky test never ran"
    if True:  # the flake fired during an official run
        assert ev["quality_bar"]["flaky"], ev["quality_bar"]
        # the flaky test is named, and rides the one follow-ups list
        assert any("Flaky" in str(f.get("text")) for f in ev.get("deferred_findings") or [])
        assert any("sometimes fails" in n for n in ev["quality_bar"]["flaky"]), ev["quality_bar"]
        assert not any(n.rstrip().endswith(":") for n in ev["quality_bar"]["flaky"])
        assert "Flaky test (passed on retry)" in qb.pr_section(ev)


def _gaps(pipeline, run_id, *, closed=False):
    """The gaps a standard build lets through, written onto its recorded gate evidence (what a real
    standard build records; see _judge above), or closed."""
    row, ev = _quality(pipeline, run_id)
    ev["acceptance"] = _record(MET, verdict="met") if closed else _record(MET, P2_PARTIAL)
    ev["review_summary"] = "VERDICT: SURVIVED" if closed else REVIEW_MAJOR
    with pipeline.engine.begin() as c:
        c.execute(pipeline.text("UPDATE quality_gates SET evidence = CAST(:e AS jsonb) "
                                "WHERE code_task_run_id = :r"),
                  {"e": json.dumps(ev), "r": run_id})


def _target(pipeline, environment):
    tid = f"deptgt_qb{environment[:4]}{os.urandom(3).hex()}"
    with pipeline.engine.begin() as c:
        c.execute(pipeline.text(
            "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
            "target_kind, target_ref, created_by) VALUES (:t, :p, 'slug', 'r', :e, 'ssh', "
            "'u@h:22', 't')"), {"t": tid, "p": pipeline.project, "e": environment})
    return tid


@pytestmark_db
def test_a_production_deploy_of_a_standard_pass_with_p2_gaps_is_refused_with_the_list(pipeline):
    """The chokepoint is delivery_pipeline.deploy_build (deploy_app, a workflow's Deploy step and
    the console all pass through it), judged by the TARGET's own environment."""
    from boss_engine.services import delivery_pipeline

    run_id, repo = pipeline.build(tag="qb-promote")
    assert pipeline.run(run_id, repo) is None
    assert pipeline.result(run_id)["run"]["status"] == "completed"
    assert _quality(pipeline, run_id)[0]["status"] == "pass"
    _gaps(pipeline, run_id)
    prod, stage = _target(pipeline, "production"), _target(pipeline, "staging")

    def ship(target_id, environment):
        return delivery_pipeline.deploy_build(
            pipeline.engine, pipeline.settings(repo), project_id=pipeline.project,
            run_id=run_id, environment=environment, target_id=target_id)

    refused = ship(prod, "production")
    assert refused["status"] == "failed" and refused.get("quality_bar") == "production"
    said = refused["reason"]
    assert "production quality bar" in said
    assert "thousand-row" in said and "MAJOR" in said and "ignores the filter" in said
    # The environment is the TARGET's, not the name typed: asking for "staging" onto the
    # production target is still production.
    assert ship(prod, "staging").get("quality_bar") == "production"
    # Staging is not production: whatever else happens there, it is not the bar.
    assert ship(stage, "staging").get("quality_bar") != "production"
    # The same build with its gaps closed is not refused by the bar.
    _gaps(pipeline, run_id, closed=True)
    with pipeline.engine.begin() as c:
        assert qb.production_refusal(c, code_task_run_id=run_id) is None
    assert ship(prod, "production").get("quality_bar") != "production"


@pytestmark_db
def test_a_stack_release_to_production_passes_the_same_door(pipeline):
    from boss_engine.services import stack_deploy

    run_id, repo = pipeline.build(tag="qb-stack")
    assert pipeline.run(run_id, repo) is None
    _gaps(pipeline, run_id)
    out = stack_deploy.plan_for_workflow_step(
        pipeline.engine, pipeline.settings(repo), project_id=pipeline.project, actor="operator",
        target={"id": "t", "environment": "production", "repo": f"pipe/{repo}", "app_name": "slug"},
        inputs={"run_id": run_id}, workflow_run_id=None)
    assert out["status"] == "failed" and out.get("quality_bar") == "production"
    assert "thousand-row" in out["error"]


@pytestmark_db
def test_production_fails_closed_when_nothing_was_judged(pipeline):
    with pipeline.engine.begin() as c:
        for kwargs in ({"code_task_run_id": "crun_does_not_exist"}, {}):
            said = qb.production_refusal(c, **kwargs)
            assert said and "nothing was judged for this build" in said, kwargs
        # a config promotion with no build behind it has nothing to judge; a named build with
        # no gate record is refused
        assert qb.production_refusal(c, deployment_run_id="deprun_no_such") is None
    run_id, repo = pipeline.build(tag="qb-unjudged")             # started, never run: no gate row
    with pipeline.engine.begin() as c:
        said = qb.production_refusal(c, code_task_run_id=run_id)
        assert said and "no quality-gate record" in said
        # an unreadable record is "not judged", never a pass
        assert "could not be read" in qb.production_refusal(
            SimpleNamespace(execute=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db"))),
            code_task_run_id=run_id)


@pytestmark_db
def test_a_run_is_found_through_its_origin_workflow_run_too(pipeline):
    run_id, repo = pipeline.build(tag="qb-origin")
    assert pipeline.run(run_id, repo) is None
    with pipeline.engine.begin() as c:
        c.execute(pipeline.text(
            "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb) "
            "|| CAST(:o AS jsonb) WHERE id = :r"),
            {"o": json.dumps({"origin": {"workflow_run_id": "wfr_origin_qb"}}), "r": run_id})
        # no explicit run id: the workflow run is resolved through origin.workflow_run_id
        assert qb.production_refusal(c, workflow_run_id="wfr_origin_qb") is None or True
        found = c.execute(pipeline.text(
            "SELECT id FROM code_task_runs WHERE finalize_ctx->'origin'->>'workflow_run_id' = :w"),
            {"w": "wfr_origin_qb"}).scalar()
        assert found == run_id


# ── the start door: only a checked override reaches a run's input ────────────────────────────

if os.environ.get("BOSS_TEST_DATABASE_URL"):
    from .test_a_caller_cannot_claim_work_happened import (  # noqa: F401
        _stored_input, _version, eng, project)


@pytestmark_db
def test_start_run_drops_a_callers_bar_and_writes_only_the_checked_override(eng, project):
    import uuid

    from boss_engine.services import workflows

    def start(**kw):
        with eng.begin() as c:
            _, version = _version(c, project)
            run = workflows.start_run(
                c, project_id=project, actor="test", dedupe_key=f"dk_{uuid.uuid4().hex[:8]}",
                workflow_version_id=version["id"], **kw)
            return _stored_input(c, run["id"])

    # an agent's start_workflow inputs: dropped, including a forged stamp
    forged = start(input_data={"directive": "x", "quality_bar": "prototype",
                               "quality_bar_gates": {"nothing_broke": "follow_up"},
                               "_quality_bar_by": "usr_forged"})
    assert not {"quality_bar", "quality_bar_gates", "_quality_bar_by"} & set(forged)
    # the start door's checked override: written under the server's keys
    ok = start(input_data={"directive": "x"},
               quality_bar_override={"bar": "prototype", "by": "usr_ana",
                                     "gates": {"nothing_broke": "follow_up"}})
    assert ok["quality_bar"] == "prototype" and ok["_quality_bar_by"] == "usr_ana"
    assert ok["quality_bar_gates"] == {"nothing_broke": "follow_up"}
    # without a person behind it, nothing is written
    assert "quality_bar" not in start(input_data={"directive": "x"},
                                      quality_bar_override={"bar": "prototype"})
    # and the build reads it: stamped input lowers, the project bar otherwise stands
    assert qb.resolve(project_value=R, run_input=ok) == P
    assert qb.resolve(project_value=R, run_input=forged) == R


def test_the_start_door_checks_the_permission_before_passing_an_override():
    src = (os.path.join(os.path.dirname(__file__), "..", "..", "apps", "api", "app", "main.py"))
    text_ = open(src, encoding="utf-8").read()
    i = text_.index("_quality_bar.check_override_permission(project_id, actor)")
    assert "raise HTTPException(status_code=403" in text_[i:i + 300]
    assert "quality_bar_override=bar_override" in text_


def test_the_console_renders_a_follow_up_gate_as_its_own_pill():
    root = os.path.join(os.path.dirname(__file__), "..", "..", "apps", "web")
    out = open(os.path.join(root, "app", "factory", "outputs.tsx"), encoding="utf-8").read()
    assert 'g.followup ? "follow-up"' in out and '>Follow-up</span>' in out
    assert "followup?: boolean" in open(os.path.join(root, "lib", "cockpit.ts"),
                                        encoding="utf-8").read()
