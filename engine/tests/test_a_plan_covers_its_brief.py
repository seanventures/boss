"""A PLAN COVERS ITS BRIEF before a person sees it or code is written (AR-24).

THE CASE (northgate-gateway, 2026-11-16): follow-up crun_b14450fc2b034508a756's brief said "fix
every open finding below, security first" and listed a HIGH dot-segment path traversal in
internal/proxy/router.go first, then a MEDIUM CORS hole in handler.go's applyCORS. The plan
fixed a request-id bug and added two test files, and went to a person on plan card
dec_dcd91e76a6254f9a9939 offering only Approve or Reject.

The fixture is that run's real brief, criteria and plan (read read-only from the laptop
install). The model answering the coverage question is a scripted decider: no model is called.
"""
from __future__ import annotations

import inspect
import json
import uuid
from pathlib import Path

from sqlalchemy import text

from boss_engine.services import decision_actions as da
from boss_engine.services import governed_code_task_service as g
from boss_engine.services import plan_coverage as pc
from boss_engine.services.typed_decisions import Decision, DecisionBatch

from .test_workflow_capability_runner import eng, project  # noqa: F401 - fixtures used by name

ROW = json.loads((Path(__file__).parent / "fixtures" / "plan_coverage_ar24.json")
                 .read_text(encoding="utf-8"))
BRIEF, CRITERIA = ROW["instructions"], ROW["acceptance_criteria"]


class Decider:
    """Answers every coverage question with `default`, or per item with `by` (a function of
    the question's text)."""

    def __init__(self, default="covered", by=None, fail=False):
        self.default, self.by, self.fail, self.calls = default, by, fail, 0

    def decide(self, context, questions):
        self.calls += 1
        if self.fail:
            raise RuntimeError("no model")
        out = {}
        for q in questions:
            answer = (self.by(q.instructions) if self.by else None) or self.default
            out[q.key] = Decision(answer if answer != "none" else None, 0.9,
                                  reason=f"scripted {answer}")
        return DecisionBatch(decisions=out, backend="scripted", model="scripted", latency_ms=0)


def _missing(cov):
    return [m["item"] for m in cov.missing]


def test_the_brief_lists_the_security_findings_first_with_their_locations():
    items = pc.brief_items(BRIEF, CRITERIA)
    findings = [i for i in items if i.files]
    high = findings[0]
    assert high.severity == "HIGH" and high.files == ["internal/proxy/router.go"]
    assert set(high.symbols) == {"pathMatches", "Match", "rewritePath"}
    cors = next(i for i in findings if "applyCORS" in i.symbols)
    assert cors.severity == "MEDIUM" and cors.files == ["internal/proxy/handler.go"]
    # The LOW/ADVISORY paragraph and the MINOR finding are not required.
    assert not any(i.severity in ("LOW", "MINOR") for i in items)
    assert not any("map, whose order is randomized" in i.text for i in items)
    # A follow-up's criteria ("Fixed: - [SECURITY] …") restate its findings: not counted twice.
    assert not any(i.kind == "must_have" for i in items)
    # Stable: the same brief gives the same ids.
    assert [i.id for i in items] == [i.id for i in pc.brief_items(BRIEF, CRITERIA)]


def test_the_real_plan_leaves_the_high_path_traversal_and_the_cors_hole_out():
    cov = pc.check(ROW, instructions=BRIEF, criteria=CRITERIA, decider=None)
    assert not cov.ok
    missing = _missing(cov)
    assert any("HIGH internal/proxy/router.go" in m for m in missing)
    assert any("applyCORS" in m for m in missing)
    assert any("router.go:39" in m for m in missing)        # the review seat's MAJOR, same bug
    # The request-id / ReadAll findings name handler.go's proxy(), which the plan changes: the
    # deterministic match lets the model decide those.
    assert not any("io.ReadAll(r.Body)" in m for m in missing)


def test_a_model_cannot_wave_through_a_finding_whose_file_the_plan_never_names():
    cov = pc.check(ROW, instructions=BRIEF, criteria=CRITERIA, decider=Decider("covered"))
    assert not cov.ok and any("HIGH internal/proxy/router.go" in m for m in _missing(cov))
    assert cov.by.startswith("scripted")


def test_an_explicit_skip_with_a_reason_is_not_a_gap():
    decider = Decider("skipped")
    cov = pc.check(ROW, instructions=BRIEF, criteria=CRITERIA, decider=decider)
    assert cov.ok and cov.skipped and decider.calls == 1


def test_the_model_finds_what_the_file_match_cannot():
    # handler.go's proxy() is in the plan, but the plan never fixes the swallowed ReadAll error.
    def by(q):
        return "missing" if "io.ReadAll" in q else "covered"

    cov = pc.check(ROW, instructions=BRIEF, criteria=CRITERIA, decider=Decider(by=by))
    assert any("io.ReadAll(r.Body)" in m for m in _missing(cov))


def test_a_decider_that_fails_leaves_the_deterministic_gaps_and_counts_the_rest_unjudged():
    cov = pc.check(ROW, instructions=BRIEF, criteria=CRITERIA, decider=Decider(fail=True))
    assert not cov.ok and any("HIGH internal/proxy/router.go" in m for m in _missing(cov))
    assert cov.unjudged > 0


def test_must_haves_are_checked_for_every_build_not_only_follow_ups():
    criteria = ["Rate limits answer 429 on the 11th request in a second",
                "Hot reload keeps the last good config"]
    design = {"plan": "**Files** internal/ratelimit/bucket.go — token bucket.", "plan_review": ""}
    def by(q):
        return "missing" if "Hot reload" in q else "covered"

    cov = pc.check(design, instructions="Build the gateway.", criteria=criteria,
                   decider=Decider(by=by))
    assert [m["kind"] for m in cov.missing] == ["must_have"]
    assert "Hot reload" in cov.missing[0]["item"]
    # No brief items at all: nothing to check, no model call.
    d = Decider()
    assert pc.check(design, instructions="Tidy the README.", criteria=[], decider=d).ok
    assert d.calls == 0


def test_an_uncovered_plan_goes_back_to_the_planner_with_the_missing_items_named():
    notes = []

    def replan(note, current):
        notes.append(note)
        assert current["plan"] == ROW["plan"]          # the plan as it stands is handed back
        return {"plan": ROW["plan"] + "\n- `internal/proxy/router.go`: `pathMatches`/`Match` — "
                "path.Clean before matching; reject `..`.\n- `internal/proxy/handler.go` "
                "`applyCORS`: never Allow-Credentials with `*`.\n",
                "plan_review": "PLAN: OK", "plan_ok": True}

    design, cov = pc.ensure_covers(dict(ROW), instructions=BRIEF, criteria=CRITERIA,
                                   decider=Decider("covered"), replan=replan)
    assert cov.ok and len(notes) == 1
    assert "HIGH internal/proxy/router.go" in notes[0] and "applyCORS" in notes[0]
    assert design["coverage"]["ok"] and design["coverage"]["replans"] == 1
    assert "path.Clean" in design["plan"]


def test_replans_are_bounded_then_a_person_sees_the_gap():
    rounds = []
    design, cov = pc.ensure_covers(
        dict(ROW), instructions=BRIEF, criteria=CRITERIA, decider=Decider("covered"),
        replan=lambda note, current: {"plan": current["plan"], "plan_review": "",
                                      "plan_ok": True},
        on_round=lambda n, c: rounds.append((n, c.ok)))
    assert not cov.ok and rounds == [(0, False), (1, False), (2, False)]
    assert design["coverage"]["replans"] == pc.MAX_REPLANS
    words = pc.gap_words(design["coverage"])
    assert words.startswith("The plan still leaves") and "HIGH internal/proxy/router.go" in words


def test_the_design_phase_checks_the_plan_before_it_is_persisted_or_gated():
    """Wiring: the check runs on every design (full or from an approved PRD), before the plan is
    persisted, before instructions are built from it and before the plan gate — and a plan
    with gaps is gated whatever the risk tier."""
    src = inspect.getsource(g)
    check = src.index("design, _cov_cost = _plan_covers_brief(")
    assert src.index("_design_from_approved_prd(\n") < check
    assert check < src.index("# PERSIST THE PLAN THE MOMENT IT EXISTS.")
    assert check < src.index("if _requires_plan_approval(risk_tier) or _gaps or _asked_again:")
    assert "_plan_coverage.change_request_brief(prior_state)" in src


def test_the_plan_card_offers_request_changes():
    row = {"status": "pending", "recommendation": {"action": "resume_code_task", "run_id": "r",
                                                   "plan": "…"}}
    assert da.consumes_revision(row)
    acts = {a["action"]: a for a in da.allowed_actions(row)}
    assert {"approve", "reject", "revise"} <= set(acts)
    assert acts["revise"]["label"] == "Request changes" and acts["revise"]["needs"] == "note"
    assert "plans again" in acts["revise"]["effect"]


# ── the database path: Request changes re-plans the paused build ─────────────────────────────

def _paused_build(c, project_id):
    from boss_engine.services.core import create_blocking_decision

    run_id = f"crun_{uuid.uuid4().hex[:20]}"
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, title, status, phase, spec, state) VALUES "
        "(:i, :p, 'northgate follow-up', 'blocked', 'awaiting_plan_approval', CAST(:s AS JSONB), "
        "CAST(:st AS JSONB))"),
        {"i": run_id, "p": project_id,
         "s": json.dumps({"instructions": BRIEF, "acceptance_criteria": CRITERIA}),
         "st": json.dumps({"design": {"plan": ROW["plan"], "plan_review": ""},
                           "awaiting_plan_approval": True, "plan_interrupt_id": "int_x"})})
    dec = create_blocking_decision(
        c, project_id, None, "Approve the plan?",
        {"action": "resume_code_task", "run_id": run_id, "plan": ROW["plan"][:4000]},
        {"phase": "plan"}, "plan gate", risk_tier=3)
    c.execute(text("UPDATE code_task_runs SET decision_id = :d WHERE id = :i"),
              {"d": dec, "i": run_id})
    return run_id, dec


def test_request_changes_on_a_plan_re_plans_with_the_note(eng, project):  # noqa: F811
    from boss_engine.config import get_settings
    from boss_engine.services.decisions import DecisionService

    try:
        with eng.begin() as c:
            c.execute(text(
                "INSERT INTO project_members (id, project_id, member_id, display_name, "
                "member_type, role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', "
                "'[\"ceo\"]'::jsonb, '[\"decision_resolve\"]'::jsonb) ON CONFLICT DO NOTHING"),
                {"i": f"pm_{project}_ceo", "p": project})
            run_id, dec = _paused_build(c, project)
            out = DecisionService(get_settings()).transition(
                c, dec, action="revise", actor="ceo",
                note="Fix the HIGH path traversal in router.go first.")
        assert out["status"] == "revision_requested"
        with eng.begin() as c:
            run = c.execute(text("SELECT status, phase, state FROM code_task_runs WHERE id=:i"),
                            {"i": run_id}).mappings().one()
            job = c.execute(text("SELECT kind, payload FROM jobs WHERE project_id=:p"),
                            {"p": project}).mappings().all()
            kinds = {r[0] for r in c.execute(text(
                "SELECT event_type FROM events WHERE entity_id=:i"), {"i": run_id})}
        assert run["status"] == "running" and run["phase"] == "design"
        state = run["state"]
        assert "design" not in state and "awaiting_plan_approval" not in state
        change = state["plan_change_request"]
        assert change["note"].startswith("Fix the HIGH path traversal")
        assert change["decision_id"] == dec and "reqID" in change["previous_plan"]
        assert [j["kind"] for j in job] == ["code.task"] and job[0]["payload"]["run_id"] == run_id
        assert "code_task.plan_sent_back" in kinds
        # The re-plan's brief carries the note (and the plan that was sent back).
        brief = pc.change_request_brief(state)
        assert "A PERSON SENT THE PLAN BACK" in brief and "router.go first" in brief
        # A run that is no longer waiting on the plan is not re-planned.
        with eng.begin() as c:
            assert not pc.on_plan_sent_back(
                c, {"id": dec, "project_id": project,
                    "recommendation": {"action": "resume_code_task", "run_id": run_id}},
                actor="usr_test", note="again", jobs=None)
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM jobs WHERE project_id=:p"), {"p": project})
            c.execute(text("DELETE FROM code_task_runs WHERE project_id=:p"), {"p": project})
            c.execute(text("DELETE FROM project_members WHERE project_id=:p"), {"p": project})


def test_the_plan_card_shows_the_gap_first(eng, project):  # noqa: F811
    """A plan still uncovered after its re-plans is gated even below tier 3, and its card leads
    with what the plan leaves out (and carries the coverage for the console)."""
    from boss_engine.config import get_settings
    from boss_engine.services.budget import BudgetGovernor

    design = {"plan": ROW["plan"], "plan_review": "", "plan_ok": True,
              "coverage": {"ok": False, "items": 19, "replans": 2, "skipped": [], "missing": [
                  {"id": "bi_1", "severity": "HIGH", "kind": "finding",
                   "item": "HIGH internal/proxy/router.go (`pathMatches`) — traversal", "why": ""}]}}
    run_id = f"crun_{uuid.uuid4().hex[:20]}"
    try:
        with eng.begin() as c:
            c.execute(text("INSERT INTO code_task_runs (id, project_id, title) "
                           "VALUES (:i, :p, 'gateway')"), {"i": run_id, "p": project})
        out = g._block_for_plan_approval(
            eng, BudgetGovernor(get_settings()), run_id=run_id, project_id=project, task_id=None,
            reservation_id=None, design=design, risk_tier=2, title="gateway")
        with eng.begin() as c:
            d = c.execute(text("SELECT question, recommendation FROM decisions WHERE id=:d"),
                          {"d": out["decision_id"]}).mappings().one()
            run = c.execute(text("SELECT status, phase FROM code_task_runs WHERE id=:i"),
                            {"i": run_id}).mappings().one()
        assert (run["status"], run["phase"]) == ("blocked", "awaiting_plan_approval")
        assert "leaves 1 brief item(s) out" in d["question"]
        rec = d["recommendation"]
        assert rec["why"].startswith("The plan still leaves 1 item")
        assert "HIGH internal/proxy/router.go" in rec["why"]
        assert rec["coverage"]["missing"][0]["id"] == "bi_1" and rec["coverage"]["replans"] == 2
        acts = {a["action"] for a in da.allowed_actions({"status": "pending", **d})}
        assert "revise" in acts
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM code_task_runs WHERE project_id=:p"), {"p": project})
