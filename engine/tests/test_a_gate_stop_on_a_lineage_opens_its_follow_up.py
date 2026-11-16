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
    (["app_serves"], {"faults": {"app_serves": "PERSON"}}),   # a secret only a person sets
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

