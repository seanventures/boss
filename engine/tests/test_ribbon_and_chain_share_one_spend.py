"""The build ribbon and the act chain name one spend (BOSSAPP-1020).

Three aggregations read the same llm_calls rows: the build list (load_facts), the cockpit
cost line (_cost), and the act chain. Two shapes of that ledger — every call included, and
a mix of money and included — must come back as the same (money, included) pair from each.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from ._obs_seed import llm_call, pg_engine, project
from boss_engine.services import agent_acts, factory_view, run_retrospective

SHAPES = {
    "all_included": [(12_500_000, True), (22_500_000, True)],
    "mixed": [(2_000_000, False), (30_000_000, True)],
}


def _pair(money: float, included: float) -> tuple[float, float]:
    return (round(float(money), 6), round(float(included), 6))


@pytest.fixture(scope="module")
def pg():
    return pg_engine()


def _seed(c, shape: str) -> tuple[str, str]:
    pid = project(c)
    run_id = f"crun_{uuid4().hex[:12]}"
    act_id = f"act_{uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, title, status) "
        "VALUES (:id, :p, 'Ledger shapes', 'completed')"),
        {"id": run_id, "p": pid})
    c.execute(text(
        "INSERT INTO agent_acts (act_id, project_id, role_key, kind, status, root_act_id, "
        "handed_by_kind, how, accountable, label, code_task_run_id) "
        "VALUES (:a, :p, 'software_engineer', 'run', 'active', :a, 'boss', 'started', "
        "'owner', 'Build', :r)"),
        {"a": act_id, "p": pid, "r": run_id})
    now = c.execute(text("SELECT now()")).scalar()
    for micros, included in SHAPES[shape]:
        llm_call(c, pid, now, cost=micros, included=included, run_id=run_id, stage="implement")
    c.execute(text("UPDATE llm_calls SET act_id = :a WHERE code_task_run_id = :r"),
              {"a": act_id, "r": run_id})
    return pid, run_id


def _three(c, pid: str, run_id: str) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]]:
    run = c.execute(text("SELECT * FROM code_task_runs WHERE id = :id"), {"id": run_id}).mappings().one()
    facts = factory_view.load_facts(c, pid, [dict(run)], acts=False, deploys=False)
    cost = facts.cost[run_id]
    ribbon = _pair(cost.get("money_usd", cost.get("metered_usd", 0)), cost.get("included_usd", -1))
    line = factory_view._cost(c, run_id, {"state": "done"})
    cockpit = _pair(line.get("metered_usd", 0), line.get("included_usd", -1))
    chain = agent_acts.chain_for_code_task(c, pid, run_id)
    assert chain is not None
    totals = chain["totals"]
    credits = _pair(totals["cost"], totals["included"])
    return ribbon, cockpit, credits


@pytest.mark.parametrize("shape", list(SHAPES))
def test_ribbon_cockpit_and_chain_report_the_same_pair(pg, shape):
    with pg.begin() as c:
        pid, run_id = _seed(c, shape)
        ribbon, cockpit, credits = _three(c, pid, run_id)
        money = sum(micros for micros, included in SHAPES[shape] if not included) / 1e6
        included = sum(micros for micros, included in SHAPES[shape] if included) / 1e6
        want = _pair(money, included)
    assert ribbon == want
    assert cockpit == want
    assert credits == want
    assert ribbon == cockpit == credits


def _seed_workflow_with_all_surfaces(
    c,
    *,
    build_calls: list[tuple[int, bool]],
    discovery_micros: int,
    discovery_billing: str,
    meeting_micros: int,
    meeting_billing: str,
) -> tuple[str, str, str]:
    """Seed one workflow run with discovery + meeting + code_task and matching agent acts."""
    import json

    pid = project(c)
    wf_def_id = f"wfd_{uuid4().hex[:10]}"
    wf_ver_id = f"wfv_{uuid4().hex[:10]}"
    wf_run_id = f"wfr_{uuid4().hex[:12]}"
    drun_id = f"drun_{uuid4().hex[:10]}"
    mrun_id = f"mrun_{uuid4().hex[:10]}"
    crun_id = f"crun_{uuid4().hex[:10]}"
    root_act_id = f"act_{uuid4().hex[:10]}"
    mtg_act_id = f"act_{uuid4().hex[:10]}"
    bld_act_id = f"act_{uuid4().hex[:10]}"
    stg_act_id = f"act_{uuid4().hex[:10]}"

    c.execute(text(
        "INSERT INTO workflow_definitions (id, project_id, workflow_key, name, description, created_by) "
        "VALUES (:id, :p, :k, 'Full delivery', 'test', 'ceo')"),
        {"id": wf_def_id, "p": pid, "k": f"wf_{uuid4().hex[:8]}"})
    c.execute(text(
        "INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, spec, "
        " content_hash, created_by) "
        "VALUES (:id, :p, :d, 1, '{}'::jsonb, :h, 'ceo')"),
        {"id": wf_ver_id, "p": pid, "d": wf_def_id, "h": uuid4().hex})
    c.execute(text(
        "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, workflow_version_id, "
        " status, actor, input) "
        "VALUES (:id, :p, :d, :v, 'completed', 'ceo', '{\"directive\": \"Build app\"}'::jsonb)"),
        {"id": wf_run_id, "p": pid, "d": wf_def_id, "v": wf_ver_id})

    c.execute(text(
        "INSERT INTO discovery_runs (id, project_id, status, phase, title, seed, context, state, usage) "
        "VALUES (:id, :p, 'completed', 'done', 'Research', 'seed', '', '{}'::jsonb, CAST(:u AS JSONB))"),
        {"id": drun_id, "p": pid, "u": json.dumps({
            "cost_usd_micros": discovery_micros,
            "model": "claude-sonnet-4-6",
            "billing": discovery_billing,
        })})
    c.execute(text(
        "INSERT INTO meeting_runs (id, project_id, status, phase, directive, url, constraints, "
        " role_keys, state, usage) "
        "VALUES (:id, :p, 'completed', 'done', 'Build app', '', '{}'::jsonb, '[]'::jsonb, "
        " '{}'::jsonb, CAST(:u AS JSONB))"),
        {"id": mrun_id, "p": pid, "u": json.dumps({
            "cost_usd_micros": meeting_micros,
            "model": "claude-sonnet-4-6",
            "billing": meeting_billing,
        })})
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, title, status) "
        "VALUES (:id, :p, 'Build app', 'completed')"),
        {"id": crun_id, "p": pid})

    for step_id, kind, work_id in (
        ("research", "discovery", drun_id),
        ("panel", "meeting", mrun_id),
        ("build", "code_task", crun_id),
    ):
        c.execute(text(
            "INSERT INTO workflow_step_runs (id, workflow_run_id, project_id, step_id, step_kind, "
            " status, attempt, output) "
            "VALUES (:id, :r, :p, :s, 'capability', 'succeeded', 1, CAST(:o AS JSONB))"),
            {"id": f"wsr_{uuid4().hex[:10]}", "r": wf_run_id, "p": pid, "s": step_id,
             "o": json.dumps({"await": {"kind": kind, "id": work_id}})})

    c.execute(text(
        "INSERT INTO agent_acts (act_id, project_id, role_key, kind, status, root_act_id, "
        " handed_by_kind, how, accountable, label, workflow_run_id) "
        "VALUES (:a, :p, NULL, 'run', 'active', :a, 'boss', 'started', 'owner', 'Workflow', :w)"),
        {"a": root_act_id, "p": pid, "w": wf_run_id})
    c.execute(text(
        "INSERT INTO agent_acts (act_id, project_id, role_key, kind, status, parent_act_id, "
        " root_act_id, handed_by_kind, how, accountable, label, workflow_run_id, step_id, meeting_run_id) "
        "VALUES (:a, :p, 'product_manager', 'step', 'active', :parent, :parent, 'boss', 'started', "
        " 'owner', 'Panel', :w, 'panel', :m)"),
        {"a": mtg_act_id, "p": pid, "parent": root_act_id, "w": wf_run_id, "m": mrun_id})
    c.execute(text(
        "INSERT INTO agent_acts (act_id, project_id, role_key, kind, status, parent_act_id, "
        " root_act_id, handed_by_kind, how, accountable, label, workflow_run_id, step_id, code_task_run_id) "
        "VALUES (:a, :p, 'software_engineer', 'step', 'active', :parent, :parent, 'boss', 'started', "
        " 'owner', 'Build', :w, 'build', :c)"),
        {"a": bld_act_id, "p": pid, "parent": root_act_id, "w": wf_run_id, "c": crun_id})
    c.execute(text(
        "INSERT INTO agent_acts (act_id, project_id, role_key, kind, status, parent_act_id, "
        " root_act_id, handed_by_kind, how, accountable, label, workflow_run_id, code_task_run_id) "
        "VALUES (:a, :p, 'software_engineer', 'stage', 'active', :parent, :root, 'boss', 'started', "
        " 'owner', 'Implement', :w, :c)"),
        {"a": stg_act_id, "p": pid, "parent": bld_act_id, "root": root_act_id,
         "w": wf_run_id, "c": crun_id})

    now = c.execute(text("SELECT now()")).scalar()
    for micros, included in build_calls:
        llm_call(c, pid, now, cost=micros, included=included, run_id=crun_id, stage="implement")
    c.execute(text("UPDATE llm_calls SET act_id = :a WHERE code_task_run_id = :r"),
              {"a": stg_act_id, "r": crun_id})
    return pid, wf_run_id, crun_id


def test_all_included_workflow_shows_zero_billed_everywhere_in_run_report_and_chain(pg):
    """Exact BOSSAPP-1020 scenario (wfr_8e29328778054a248433): three subscription-included
    builds + included discovery + included meeting -> $0 billed everywhere, included named."""
    with pg.begin() as c:
        pid, wf_run_id, crun_id = _seed_workflow_with_all_surfaces(
            c,
            build_calls=[(4_772_500, True), (5_040_400, True), (6_067_100, True)],
            discovery_micros=1_424_177,
            discovery_billing="subscription",
            meeting_micros=1_237_674,
            meeting_billing="subscription",
        )
        report = run_retrospective.assemble_workflow(c, pid, wf_run_id)
        single = run_retrospective.assemble(c, pid, crun_id)
        chain = agent_acts.chain(c, pid, wf_run_id)
        build_ribbon, build_cockpit, build_chain = _three(c, pid, crun_id)

    expected_build_inc = round((4_772_500 + 5_040_400 + 6_067_100) / 1e6, 6)
    expected_total_inc = round((4_772_500 + 5_040_400 + 6_067_100 + 1_424_177 + 1_237_674) / 1e6, 6)

    # Single build retrospective & ribbon/cockpit/chain all agree: $0 billed, $15.88 included.
    assert build_ribbon == build_cockpit == build_chain == _pair(0.0, expected_build_inc)
    assert single["efficiency"]["cost_usd"] == 0.0
    assert single["efficiency"]["included_usd"] == round(expected_build_inc, 2)

    # Whole workflow run report shows $0 billed and full included total named as included.
    assert report["ok"] is True
    assert report["total_usd"] == 0.0
    assert _pair(report["total_usd"], report["included_usd"]) == _pair(0.0, expected_total_inc)
    assert _pair(0.0, report["list_usd"]) == _pair(0.0, expected_total_inc)
    for step in report["steps"]:
        assert step["usd"] == 0.0
        assert step["included_usd"] > 0
    for who in report["contributors"]:
        assert who["usd"] == 0.0
        assert who["included_usd"] > 0

    # Markdown report names $0.00 billed and names the included amount as included.
    md = run_retrospective.workflow_markdown(report)
    assert "$0.00" in md and "included" in md

    # Chain panel also sees $0 billed for meeting + build (no misclassified $1.24 meeting!).
    assert chain is not None
    expected_chain_inc = round((4_772_500 + 5_040_400 + 6_067_100 + 1_237_674) / 1e6, 6)
    assert _pair(chain["totals"]["cost"], chain["totals"]["included"]) == _pair(0.0, expected_chain_inc)


def test_mixed_workflow_report_splits_billed_and_included_accurately(pg):
    with pg.begin() as c:
        pid, wf_run_id, crun_id = _seed_workflow_with_all_surfaces(
            c,
            build_calls=[(2_000_000, False), (10_000_000, True)],
            discovery_micros=1_500_000,
            discovery_billing="subscription",
            meeting_micros=750_000,
            meeting_billing="metered",
        )
        report = run_retrospective.assemble_workflow(c, pid, wf_run_id)
        build_step = next(s for s in report["steps"] if s["kind"] == "code_task")
        build_ribbon, build_cockpit, build_chain = _three(c, pid, crun_id)

    assert _pair(build_step["usd"], build_step["included_usd"]) == build_ribbon == build_cockpit == build_chain
    assert _pair(report["total_usd"], report["included_usd"]) == _pair(2.75, 11.5)
    assert _pair(0.0, report["list_usd"]) == _pair(0.0, 14.25)


def test_discovery_summary_and_meeting_save_phase_preserve_billing_marker(pg):
    from boss_engine.services.discovery_service import _row_to_summary
    from boss_engine.services.meeting_service import _save_phase

    row = {
        "id": "drun_1", "project_id": "p1", "status": "completed", "phase": "done",
        "title": "t", "seed": "s", "error": None,
        "usage": {"cost_usd_micros": 1_250_000, "model": "claude", "billing": "subscription"},
        "created_at": None, "updated_at": None, "state": {},
        "verdict": "pursue", "confidence": 0.9, "findings": 5, "outcomes": 2, "deep": False,
        "job_status": None, "job_error": None, "job_lease_owner": None,
        "job_run_after": None, "job_age": None,
    }
    summary = _row_to_summary(row)
    assert summary["usage"]["billing"] == "subscription"

    with pg.begin() as c:
        pid = project(c)
        mrun_id = f"mrun_{uuid4().hex[:10]}"
        c.execute(text(
            "INSERT INTO meeting_runs (id, project_id, status, phase, directive, url, "
            " constraints, role_keys, state, usage) "
            "VALUES (:id, :p, 'running', 'round1', 'd', '', '{}'::jsonb, '[]'::jsonb, "
            " '{}'::jsonb, '{}'::jsonb)"), {"id": mrun_id, "p": pid})
        _save_phase(c, mrun_id, {"round1": {}}, 1_237_674, "claude-sonnet-4-6", "synthesis",
                    billing="subscription")
        stored = c.execute(text("SELECT usage FROM meeting_runs WHERE id = :id"),
                           {"id": mrun_id}).scalar()
    assert stored["billing"] == "subscription"
    assert stored["cost_usd_micros"] == 1_237_674

