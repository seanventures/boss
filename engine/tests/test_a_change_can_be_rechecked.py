"""Re-check this change (BOSSAPP-1326).

THE CASE: northgate-gateway's change #2 (crun_95aca9c7) kept a false acceptance table — 0 met /
30 missing from a reviewer that never saw the tree (BOSSAPP-1323) — on its PR and merge card.
After the fix there was no way to run its checks again: "Review this PR again" runs only the
review seat, and Retry writes code.

A re-check is a new attempt of the lineage, marked `recheck_only`: every gate on the branch
head, every writing stage on a NoWriteAgent, no design, no repair round, nothing committed. Its
merge card is the change's verdict and the older cards for the PR are withdrawn.
"""
from __future__ import annotations

import inspect
import json
import subprocess
import uuid

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import acceptance_gate as ag
from boss_engine.services import change_recheck as cr
from boss_engine.services import checks_failing as cf
from boss_engine.services import governed_code_task_service as g
from boss_engine.services import run_decisions

from .test_a_resumed_build_runs_as_the_person import _Jobs
from .test_workflow_capability_runner import eng, project  # noqa: F401 - fixtures used by name

PR = "http://boss-local/local_northgate-gateway/northgate-gateway/pull/2"
CRITERIA = ["`make run-staging` starts the gateway", "A Go module (go.mod) at the root"]


def _run(c, pid, *, status="completed", fctx=None, state=None, pr=PR):
    rid = f"crun_{uuid.uuid4().hex[:16]}"
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, "
        "finalize_ctx, pull_request_url, created_at) VALUES (:i, :p, :s, 'done', "
        "'KAN-9 Northgate gateway', CAST(:sp AS JSONB), CAST(:st AS JSONB), CAST(:f AS JSONB), "
        ":u, clock_timestamp())"),
        {"i": rid, "p": pid, "s": status, "u": pr,
         "sp": json.dumps({"instructions": "Close the KAN-9 must-haves.", "owner": "local",
                           "repo": "northgate-gateway", "acceptance_criteria": CRITERIA}),
         "st": json.dumps(state or {}), "f": json.dumps(fctx or {})})
    return rid


def _cleanup(eng, pid):  # noqa: F811
    with eng.begin() as c:
        for t in ("decisions", "jobs", "events", "llm_calls", "quality_gates", "code_task_runs"):
            c.execute(text(f"DELETE FROM {t} WHERE project_id = :p"), {"p": pid})


def _items():
    return ag.acceptance_items({"acceptance_criteria": CRITERIA})


# ── the request: a new attempt of the lineage, marked, with its cost ──────────────────────────

def test_a_recheck_is_a_marked_attempt_on_the_branch_head(eng, project):  # noqa: F811
    try:
        with eng.begin() as c:
            root = _run(c, project)
            c.execute(text("UPDATE code_task_runs SET finalize_ctx = jsonb_build_object("
                           "'root_run_id', id) WHERE id = :i"), {"i": root})
            false_table = _run(c, project, fctx={"root_run_id": root, "attempt": 4})
            c.execute(text(
                "INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, engine, model, "
                "cost_usd_micros, ok) VALUES (:i, :p, :r, 'acceptance_check', 'claude_code', "
                "'m', 1750000, true), (:j, :p, :r, 'review_panel', 'claude_code', 'm', 1660000, "
                "true), (:k, :p, :r, 'implement', 'claude_code', 'm', 9000000, true)"),
                {"i": f"llm_{uuid.uuid4().hex[:10]}", "j": f"llm_{uuid.uuid4().hex[:10]}",
                 "k": f"llm_{uuid.uuid4().hex[:10]}", "p": project, "r": false_table})
            seen = cr.preview(c, false_table, project)
        assert seen["can"] and seen["gates"] == list(cr.GATES)
        assert seen["estimate"]["usd"] == 3.41, "the checks' seats, never the coding"
        out = cr.request(eng, get_settings(), _Jobs(), false_table, project_id=project,
                         actor="ceo")
        with eng.begin() as c:
            new = c.execute(text("SELECT status, finalize_ctx, spec FROM code_task_runs "
                                 "WHERE id = :i"), {"i": out["run_id"]}).mappings().one()
            kinds = [r[0] for r in c.execute(text(
                "SELECT event_type FROM events WHERE entity_id = :i"), {"i": out["run_id"]})]
        fctx = new["finalize_ctx"]
        assert fctx[cr.KEY] is True and fctx[cr.OF_KEY] == false_table
        assert fctx["root_run_id"] == root and fctx["parent_run_id"] == false_table
        assert "seed_workspace_from" not in fctx, "the branch head, never a saved workspace"
        assert "code_task.recheck_requested" in kinds
        assert out["estimate"]["usd"] == 3.41
    finally:
        _cleanup(eng, project)


@pytest.mark.parametrize("change,why", [
    ({"status": "running"}, "still running"),
    ({"pr": None}, "no pull request"),
    ({"fctx": {"merge": {"sha": "abc123"}}}, "already merged"),
])
def test_what_cannot_be_rechecked_says_why(eng, project, change, why):  # noqa: F811
    try:
        with eng.begin() as c:
            rid = _run(c, project, status=change.get("status", "completed"),
                       fctx=change.get("fctx"), pr=change.get("pr", PR))
        with pytest.raises(cr.RecheckRefused, match=why):
            cr.request(eng, get_settings(), _Jobs(), rid, project_id=project, actor="ceo")
    finally:
        _cleanup(eng, project)


# ── no code is written ────────────────────────────────────────────────────────────────────────

def test_the_agent_of_every_writing_stage_writes_nothing(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "main.go").write_text("package main\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@x",
                    "commit", "-qm", "head"], check=True)
    agent = cr.NoWriteAgent("implement")
    out = agent.run_role("Fix every failing must-have. Write the code now.", tmp_path,
                         model="m", parallel_agents=2, resume_session_id="s", max_turns=40,
                         max_budget_usd=5.0)
    assert out["cost_usd_micros"] == 0 and "no code is written" in out["summary"]
    status = subprocess.run(["git", "-C", str(tmp_path), "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    assert status == ""
    assert agent.budget_ceiling is None                        # knobs read on an agent: none


def test_the_pipeline_runs_every_gate_and_writes_nothing():
    """Wiring, at the source (the pipeline is too large to drive here)."""
    src = inspect.getsource(g)
    routed = src[src.index("    def _routed_agent(step: str, decision):"):]
    routed = routed[:routed.index("    def _route(step: str):")]
    assert "if _recheck and step in _change_recheck.WRITING_STAGES:" in routed
    assert "return _change_recheck.NoWriteAgent(step), None" in routed
    assert "and not _seeded and not _recheck" in src                     # no design
    assert "if _recheck:\n            # A re-check writes no tests" in src
    assert "max_repair = 0 if _recheck else" in src                      # no implement retries
    assert "inrun_max = 0          # a re-check reports" in src           # no fix rounds
    assert 'if _recheck and action == "repair":' in src                  # no repair attempt
    assert "_pr_files = []" in src                                       # nothing committed
    assert "use_microvm = _resolve_use_microvm(settings, spec) and not _recheck" in src
    # The writing stages are the coder's and the test author's; the seats are not among them.
    assert cr.WRITING_STAGES >= {"implement", "write_tests"}
    assert not cr.WRITING_STAGES & {"review_code", "security_check", "consistency_check",
                                    "acceptance_check", "plan"}


# ── the re-check's verdict replaces the false table on the PR ─────────────────────────────────

def test_a_recheck_after_a_fix_replaces_the_false_table(eng, project):  # noqa: F811
    blind = ag.judge(_items(), "ITEM 1: MISSING — no Makefile exists anywhere in the tree\n"
                               "ITEM 2: MISSING — no go.mod exists anywhere in the tree\n"
                               "ACCEPTANCE: NOT MET", {}, root=None)
    good = ag.judge(_items(), "ITEM 1: MET — Makefile\nITEM 2: MET — go.mod\nACCEPTANCE: MET",
                    {}, root=None)
    for i in good["items"]:
        i["status"] = "met"
    good = {**good, "verdict": "met", "missing": [], "partial": [], "met": [1, 2],
            "counts": {"met": 2, "partial": 0, "missing": 0, "unjudged": 0}}
    try:
        with eng.begin() as c:
            root = _run(c, project)
            c.execute(text("UPDATE code_task_runs SET finalize_ctx = jsonb_build_object("
                           "'root_run_id', id) WHERE id = :i"), {"i": root})
            record = cf.collect([ag.GATE])
            false_table = _run(c, project, fctx={"root_run_id": root,
                                                 "checks_failing": record},
                               state={ag.KEY: blind})
            old_card = f"dec_{uuid.uuid4().hex[:10]}"
            c.execute(text("INSERT INTO decisions (id, project_id, question, recommendation, "
                           "status) VALUES (:i, :p, 'Checks failing — decide the merge', "
                           "CAST(:r AS JSONB), 'pending')"),
                      {"i": old_card, "p": project, "r": json.dumps(
                          {"action": "review_pull_request", "pull_request_url": PR,
                           "run_id": false_table, "checks_failing": record})})
            card, acc = ag.merge_card_checks(c, run_id=false_table, evidence={ag.KEY: blind},
                                             checks_failing=record, pr_url=PR)
            assert card is not None and acc["verdict"] == "not_met"
            # The re-check ran after the fix, as an attempt of the lineage, and judged the tree.
            recheck = _run(c, project, fctx={"root_run_id": root, "parent_run_id": false_table,
                                             cr.KEY: True, cr.OF_KEY: false_table},
                           state={ag.KEY: good})
            card2, acc2 = ag.merge_card_checks(c, run_id=recheck, evidence={ag.KEY: good},
                                               checks_failing=None, pr_url=PR)
            assert card2 is None, "the change's card no longer says its must-haves are missing"
            assert acc2["verdict"] == "met"
            # Its merge card supersedes the old one (#158's rule, run when the card is made).
            new_card = f"dec_{uuid.uuid4().hex[:10]}"
            c.execute(text("INSERT INTO decisions (id, project_id, question, recommendation, "
                           "status, created_at) VALUES (:i, :p, 'Approve code change', "
                           "CAST(:r AS JSONB), 'pending', clock_timestamp())"),
                      {"i": new_card, "p": project, "r": json.dumps(
                          {"action": "review_pull_request", "pull_request_url": PR,
                           "run_id": recheck})})
            gone = run_decisions.withdraw_superseded_merge_cards(c, project, PR, new_card,
                                                                 run_id=recheck)
            assert gone == [old_card]
    finally:
        _cleanup(eng, project)


def test_every_door_needs_the_steer_permission():
    from pathlib import Path

    root = Path(g.__file__).resolve().parents[3]
    api = (root / "apps/api/app/main.py").read_text(encoding="utf-8")
    for route in ('@app.get("/v1/code-tasks/{run_id}/recheck")',
                  '@app.post("/v1/code-tasks/{run_id}/recheck")'):
        at = api.index(route)
        assert "@permit(P.CODE_TASK_STEER" in api[at:at + 200], route
    tools = (root / "engine/boss_engine/tools/builtin.py").read_text(encoding="utf-8")
    at = tools.index('"recheck_change",')
    assert 'permission="code_task:steer"' in tools[at:at + 1500]
    web = (root / "apps/web/app/factory/cockpit-actions.ts").read_text(encoding="utf-8")
    assert 'declareAction(recheckChange, "code_task:steer");' in web
