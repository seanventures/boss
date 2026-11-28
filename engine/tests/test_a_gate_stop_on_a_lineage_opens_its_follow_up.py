"""Every gate stop on a lineage opens its PR with checks failing and one follow-up
(BOSSAPP-1319, AR-25).

northgate-gateway (2026-11-16), change #2's lineage (root crun_24f39976):
  * crun_5c4fca0a (attempt 3) shipped with its checks failing and opened follow-up
    crun_b14450fc; that follow-up's plan was REJECTED and it was cancelled;
  * crun_f5868a67 (attempt 4, a new build of the same PR) passed security, review and
    consistency and failed only its must-haves (acceptance_met: 20 met / 10 partial);
  * its round count reached the run supervisor's cap, the supervisor judged stop_report, and
    that short-circuit set the gate action to "block" WITHOUT passing through the operator's
    rule (checks_failing): the build parked "awaiting_gate_override — a human can waive these"
    and no follow-up opened, while the rounds before it (under the cap) had each opened one.

The rule is now applied once, to whatever decided the stop (checks_failing.stop_or_open), so a
supervisor stop, a spent inconclusive escalation and the gate disposition all open the PR and a
follow-up. Its caps are kept: a person's act, a template's gate and a run that opens no PR still
stop; a follow-up never spawns its own follow-up; and the setting off is the waiver stop.
"""
from __future__ import annotations

import inspect
import re

import pytest
from sqlalchemy import text

from boss_engine.services import checks_failing as cf
from boss_engine.services import governed_code_task_service as governed
from boss_engine.services.acceptance import ledger as al
from boss_engine.services.code_task_service import lineage_branch_owner
from boss_engine.services import run_supervisor_types as rst

from .test_acceptance_findings_ledger import (SPEC, _check, _run, _spawn,  # noqa: F401
                                              ledger_project, strict_transaction_guard)
from .test_workflow_capability_runner import eng, project  # noqa: F401 - fixtures used by name

ACCEPTANCE = ["acceptance_met"]


# ── the stop goes through the rule, whoever decided it ──────────────────────────────────────

def test_a_supervisor_stop_on_failing_must_haves_opens_the_pr():
    # What the supervisor's stop_report short-circuit hands on: "block".
    assert cf.stop_or_open("block", ACCEPTANCE, faults={"acceptance_met": "WORK"},
                           open_pr=True, enabled=True) == cf.ACTION
    # The setting off is the waiver stop, as it was.
    assert cf.stop_or_open("block", ACCEPTANCE, open_pr=True, enabled=False) == "block"


@pytest.mark.parametrize("failing,kw", [
    (["template_tests"], {}),                                 # an applied template's gate
    (["no_committed_secrets"], {}),
    (ACCEPTANCE, {"open_pr": False}),                         # a run that opens no PR
])
def test_what_the_rule_never_opens_still_stops(failing, kw):
    assert cf.stop_or_open("block", failing, enabled=True, **kw) == "block"


@pytest.mark.parametrize("action", ["ship", "repair", "ship_with_dissent", cf.ACTION])
def test_any_other_action_is_left_alone(action):
    assert cf.stop_or_open(action, ACCEPTANCE, open_pr=True, enabled=True) == action


def test_every_stop_in_the_pipeline_passes_through_the_rule():
    """At the source (the pipeline is too large to drive here): the supervisor's stop and every
    other branch that sets the action come BEFORE the one place the rule is applied, and the
    rule is applied before the dispositions read the action."""
    src = inspect.getsource(governed)
    sup = src.index("if _sup_action in (run_supervisor_types.STOP_REPORT, "
                    "run_supervisor_types.DEGRADED):")
    rule = src.index("action = _checks_failing.stop_or_open(action, failing, faults=gate_faults,")
    disposed = src.index('elif action in ("ship_with_dissent", _checks_failing.ACTION):')
    parked = src.index('"ph": "done" if _held else "awaiting_gate_override",')
    assert sup < rule < disposed < parked
    # Nothing between the rule and the dispositions sets the action again.
    between = src[rule + 1:disposed]
    assert not re.search(r"\n\s+action = ", between)
    assert rst.STOP_REPORT and rst.DEGRADED


# ── the northgate sequence: the new build opens the follow-up ──────────────────────────────

def test_a_new_build_after_a_rejected_follow_up_opens_one_for_its_open_must_haves(
        eng, ledger_project):  # noqa: F811
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, spec=SPEC)
        c.execute(text("UPDATE code_task_runs SET finalize_ctx = finalize_ctx || "
                       "jsonb_build_object('root_run_id', id) WHERE id = :i"), {"i": root})
        _check(c, p, root, {1: "MISSING", 3: "PARTIAL"})
        shipped = _run(c, p, spec=SPEC, fctx={"root_run_id": root, "attempt": 3})
    # Round 3 shipped with its checks failing and opened its follow-up, which holds the rows.
    first = _spawn(eng, p, shipped, root, [
        {"severity": "security", "text": "Failing gate 'security_clean' (Security clean)"}],
        checks_failing=cf.collect(["security_clean"]))
    assert first
    with eng.begin() as c:
        assert {r["followup_run_id"] for r in al.open_for(c, project_id=p)} == {first}
        # That follow-up's plan was rejected: it was cancelled at its plan.
        c.execute(text("UPDATE code_task_runs SET status = 'cancelled', "
                       "phase = 'awaiting_plan_approval', error = 'The plan was rejected', "
                       "finalize_ctx = finalize_ctx || '{\"plan_rejected\": true}'::jsonb "
                       "WHERE id = :i"), {"i": first})
        # A new build of the same PR (a retry of round 3, not a follow-up).
        latest = _run(c, p, spec=SPEC, status="running",
                      fctx={"root_run_id": root, "attempt": 4})
    # It failed only its must-haves, past the supervisor's cap: the stop opens the PR.
    action = cf.stop_or_open("block", ACCEPTANCE, faults={"acceptance_met": "WORK"},
                             open_pr=True, enabled=True)
    assert action == cf.ACTION
    # None of the follow-up guards holds it back.
    assert not governed._already_spawned_followup(eng, latest)  # noqa: SLF001
    with eng.begin() as c:
        assert lineage_branch_owner(c, p, root, exclude=(latest,)) is None
    record = cf.collect(ACCEPTANCE, {"acceptance_summary": (
        "FINDING: MAJOR — Must-have 1 MISSING: Unauthenticated requests get 401")},
        details={"acceptance_met": "Must-haves: 1 met, 1 partial, 1 missing"})
    new = _spawn(eng, p, latest, root, cf.followup_findings(record), checks_failing=record)
    assert new and new != first, "the new build's open must-haves get their own follow-up"
    with eng.begin() as c:
        rows = al.open_for(c, project_id=p)
        run = c.execute(text("SELECT title, finalize_ctx FROM code_task_runs WHERE id = :i"),
                        {"i": new}).mappings().one()
    # The rows the cancelled follow-up held went back to open and ride the new one.
    assert rows and all(r["followup_run_id"] == new for r in rows)
    assert run["title"].startswith("Follow-up: checks failing")
    assert run["finalize_ctx"]["is_followup"] is True
    assert run["finalize_ctx"]["root_run_id"] == root




# ── PR #158 review 2: an environment or refusal stop opens its PR, never a follow-up ───────

@pytest.mark.parametrize("failing,faults,reason,expect", [
    (["app_serves"], {"app_serves": "HARNESS"}, "environment_only", "environment_only"),
    (["app_serves"], {"app_serves": "PERSON"}, "refusal", "refusal"),
    (["app_serves", "nothing_broke"], {"app_serves": "HARNESS", "nothing_broke": "PERSON"},
     None, "environment_only"),
    (["app_serves"], {"app_serves": "PERSON"}, None, "refusal"),
])
def test_an_environment_or_refusal_stop_opens_the_pr_with_no_follow_up(failing, faults, reason,
                                                                       expect):
    assert cf.stop_or_open("block", failing, faults=faults, reason=reason) == cf.ACTION
    assert cf.environment_stop(failing, faults=faults, reason=reason) == expect
    rec = {**cf.collect(failing), "stopped": cf.stopped_record(expect, failing)}
    words = "environment problem" if expect == "environment_only" else "an agent refused"
    assert rec["stopped"]["words"].startswith(f"stopped: {words}")
    assert "No follow-up build is queued" in cf.pr_section(rec)
    assert f"Stopped: {words}" in cf.pr_section(rec)
    assert "no follow-up build is queued" in cf.decision_summary(rec)


@pytest.mark.parametrize("reason", ["repeated_failure", "stalled", "round_cap", None])
def test_a_work_failure_stop_still_gets_its_one_follow_up(reason):
    faults = {"acceptance_met": "WORK"}
    assert cf.stop_or_open("block", ACCEPTANCE, faults=faults, reason=reason) == cf.ACTION
    assert cf.environment_stop(ACCEPTANCE, faults=faults, reason=reason) is None
    assert "queued as a follow-up build" in cf.pr_section(cf.collect(ACCEPTANCE))


def test_held_gates_still_hold_on_an_environment_stop():
    assert cf.stop_or_open("block", ["template_tests"], faults={"template_tests": "HARNESS"},
                           reason="environment_only") == "block"
    assert cf.stop_or_open("block", ["app_serves"], faults={"app_serves": "HARNESS"},
                           reason="environment_only", open_pr=False) == "block"


def test_the_pipeline_records_the_stop_and_skips_the_follow_up():
    src = inspect.getsource(governed)
    assert "reason=_sup_reason)" in src
    assert '"stopped": _checks_failing.stopped_record(\n                        _env_stop, failing,' in src
    spawn = src.index("# A PR that opened with its checks failing always gets its follow-up")
    assert 'not ((_checks or {}).get("stopped") or {}).get("no_followup")' in src[spawn:spawn + 600]


# ── PR #158 re-review (c): a secret only a person can set — say what and where, then wait ──

def test_a_person_stop_names_the_secret_and_where_to_set_it():
    """The reviewer's probe: an `app_serves` PERSON fault, needs_secret=['STRIPE_KEY']."""
    failing, faults = ["app_serves"], {"app_serves": "PERSON"}
    reason = cf.environment_stop(failing, faults=faults)
    assert cf.stop_or_open("block", failing, faults=faults) == cf.ACTION
    stopped = cf.stopped_record(reason, failing, needs=["STRIPE_KEY"])
    assert stopped["reason"] == "person" and stopped["needs_secret"] == ["STRIPE_KEY"]
    assert ("Set STRIPE_KEY for environments build, preview and deploy in the app's Secrets, "
            "then press Retry") in stopped["words"]
    rec = {**cf.collect(failing), "stopped": stopped}
    assert "Set STRIPE_KEY for environments" in cf.pr_section(rec)
    assert "Set STRIPE_KEY for environments" in cf.decision_summary(rec)


def test_a_person_stop_carries_whose_file_or_start_error_asked():
    """ENT-REC-13: the card names the app and where the ask came from, not only the name."""
    headline = ("the app needs a secret: PAYMENTS_API_KEY (declared by helpdesk/.env.example, "
                "which marks it required)")
    stopped = cf.stopped_record("person", ["app_serves"], needs=["PAYMENTS_API_KEY"],
                                headline=headline)
    assert stopped["headline"] == headline
    assert stopped["words"].startswith("stopped: needs a person, ")
    assert f": {headline}. Set PAYMENTS_API_KEY" in stopped["words"], stopped["words"]
    assert "use a test key" in stopped["action"]
    src = inspect.getsource(governed)
    assert "headline=(_app_serves_headline_of(app_serves_record)" in src


def test_an_agent_refusal_reads_as_a_refusal_not_a_person():
    stopped = cf.stopped_record("refusal", ["code_review_passed"],
                                why="the reviewer refused to read the vendored keys")
    assert stopped["words"] == ("stopped: an agent refused: the reviewer refused to read the "
                                "vendored keys")
    assert "needs a person" not in stopped["words"]


def test_the_pipeline_passes_the_secret_and_waits_for_the_person():
    src = inspect.getsource(governed)
    assert 'needs=((app_serves_record or {}).get("needs_secret")' in src
    wait = src.index("_secret_card = _checks_failing_wait(")
    assert wait < src.index('        return {"status": "completed", "done": True, '
                            '"quality_gate_id": qg_id,\n                "deferred_findings"')


def test_a_person_stopped_build_waits_and_retry_once_set_reruns_it(eng, project,  # noqa: F811
                                                                   monkeypatch):
    import uuid

    from boss_engine.config import get_settings
    from boss_engine.services import code_task_service as cts
    from boss_engine.services import decision_actions as da
    from boss_engine.services import decisions as dmod

    calls: list = []
    monkeypatch.setattr(cts, "retry_code_task",
                        lambda engine, settings, jobs, run_id, **kw: calls.append((run_id, kw)))
    monkeypatch.setattr(dmod, "_spawn_as_this_request", lambda fn: fn())
    url = "https://g.test/acme/shop/pull/4"
    run_id = f"crun_{uuid.uuid4().hex[:12]}"
    failing = ["app_serves"]
    rec = {**cf.collect(failing), "stopped": cf.stopped_record(
        "refusal", failing, needs=["STRIPE_KEY"])}
    try:
        with eng.begin() as c:
            c.execute(text(
                "INSERT INTO project_members (id, project_id, member_id, display_name, "
                "member_type, role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', "
                "'[\"ceo\"]'::jsonb, '[\"decision_resolve\"]'::jsonb) ON CONFLICT DO NOTHING"),
                {"i": f"pm_{project}_ceo", "p": project})
            c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, phase, "
                           "pull_request_url) VALUES (:i, :p, 'shop', 'completed', 'done', :u)"),
                      {"i": run_id, "p": project, "u": url})
            dec = cf.wait_for_person(c, run_id=run_id, project_id=project, task_id=None,
                                     record=rec, pull_request_url=url)
            run = c.execute(text("SELECT status, phase, error FROM code_task_runs WHERE id=:i"),
                            {"i": run_id}).mappings().one()
            card = dict(c.execute(text("SELECT * FROM decisions WHERE id=:d"),
                                  {"d": dec}).mappings().one())
        assert (run["status"], run["phase"]) == ("waiting_on_person", "awaiting_secret")
        assert "Set STRIPE_KEY for environments" in run["error"]
        assert "STRIPE_KEY" in card["question"]
        assert card["recommendation"]["next_step"].startswith("Set STRIPE_KEY")
        acts = {a["action"]: a["label"] for a in da.allowed_actions(card)}
        assert acts["approve"] == "Retry once set"
        with eng.begin() as c:
            dmod.DecisionService(get_settings()).transition(c, dec, action="approve",
                                                            actor="ceo", note="set it")
            status = c.execute(text("SELECT status FROM code_task_runs WHERE id=:i"),
                               {"i": run_id}).scalar()
        assert status == "completed"
        assert calls and calls[0][0] == run_id and calls[0][1]["by_person"] is True
        assert "STRIPE_KEY" in calls[0][1]["fix_note"]
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM decisions WHERE project_id=:p"), {"p": project})
            c.execute(text("DELETE FROM jobs WHERE project_id=:p"), {"p": project})
            c.execute(text("DELETE FROM code_task_runs WHERE project_id=:p"), {"p": project})
            c.execute(text("DELETE FROM project_members WHERE project_id=:p"), {"p": project})
