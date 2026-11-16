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
        c.execute(text("DELETE FROM finding_observations WHERE finding_id IN (SELECT id FROM "
                       "findings WHERE project_id = :p)"), {"p": pid})
        for t in ("findings", "work_item_links", "work_items", "project_members", "decisions",
                  "jobs", "events", "llm_calls", "quality_gates", "code_task_runs"):
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


# ── #168 review ───────────────────────────────────────────────────────────────────────────────

def test_a_failing_recheck_starts_nothing_and_raises_one_fix_card(eng, project, monkeypatch):  # noqa: F811
    """R1: a re-check never starts a build on its own. Its findings go to the ledger and ONE
    card, "Fix these (about $X)", starts the follow-up only when a person with steer presses it."""
    from boss_engine.identity import lineage
    from boss_engine.services import decisions as dmod
    from boss_engine.services.acceptance import ledger as al

    record = cf.collect([ag.GATE, "security_clean"])
    findings = cf.followup_findings(record) + [
        {"severity": "minor", "text": "(review_code) internal/proxy/handler.go:9 — stray log"}]
    try:
        with eng.begin() as c:
            c.execute(text(
                "INSERT INTO project_members (id, project_id, member_id, display_name, "
                "member_type, role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', "
                "'[\"ceo\"]'::jsonb, '[\"decision_resolve\"]'::jsonb) ON CONFLICT DO NOTHING"),
                {"i": f"pm_{project}_ceo", "p": project})
            root = _run(c, project)
            c.execute(text("UPDATE code_task_runs SET finalize_ctx = jsonb_build_object("
                           "'root_run_id', id) WHERE id = :i"), {"i": root})
            recheck = _run(c, project, fctx={"root_run_id": root, cr.KEY: True})
            before = c.execute(text("SELECT count(*) FROM code_task_runs WHERE project_id = :p"),
                               {"p": project}).scalar()
        card = cr.raise_fix_card(eng, run_id=recheck, project_id=project, task_id=None,
                                 title="Northgate gateway", spec={"owner": "acme", "repo": "gw"},
                                 base_branch=f"boss/code/{root}", parent_root=root,
                                 parent_origin=None, workflow_run_id=None, findings=findings,
                                 checks_failing=record)
        again = cr.raise_fix_card(eng, run_id=recheck, project_id=project, task_id=None,
                                  title="Northgate gateway", spec={}, base_branch="b",
                                  parent_root=root, parent_origin=None, workflow_run_id=None,
                                  findings=findings, checks_failing=record)
        assert card and again == card, "one card per re-check"
        with eng.begin() as c:
            row = dict(c.execute(text("SELECT * FROM decisions WHERE id = :d"),
                                 {"d": card}).mappings().one())
            after = c.execute(text("SELECT count(*) FROM code_task_runs WHERE project_id = :p"),
                              {"p": project}).scalar()
            rows = al.open_for(c, project_id=project)
        assert after == before, "nothing was built"
        assert row["question"].startswith("Fix these (about $")
        assert rows and all(r["status"] == "open" for r in rows), "recorded, held by no build"
        from boss_engine.services import decision_actions as da

        labels = {a["action"]: a["label"] for a in da.allowed_actions(row)}
        assert labels["approve"].startswith("Fix these (about $")
        # Without the steer permission the press is refused, and still nothing is built.
        monkeypatch.setattr(lineage, "request_enforced", lambda: True)
        monkeypatch.setattr(lineage, "current_context", lambda: None)
        with pytest.raises(dmod.InvalidDecisionTransitionError, match="Steer builds"), \
                eng.begin() as c:                              # refused: rolled back
            dmod.DecisionService(get_settings()).transition(c, card, action="approve",
                                                            actor="ceo", note="go")
        with eng.begin() as c:
            assert c.execute(text("SELECT count(*) FROM code_task_runs WHERE project_id = :p"),
                             {"p": project}).scalar() == before
        # With it, the press starts the one follow-up, on the change's branch.
        monkeypatch.setattr(lineage, "request_enforced", lambda: False)
        monkeypatch.setattr(dmod, "_spawn_as_this_request", lambda fn: fn())
        monkeypatch.setattr("boss_engine.db.get_engine", lambda: eng)
        svc = dmod.DecisionService(get_settings())
        svc.jobs = _Jobs()
        with eng.begin() as c:
            svc.transition(c, card, action="approve", actor="ceo", note="go")
        with eng.begin() as c:
            fix = c.execute(text("SELECT title, finalize_ctx FROM code_task_runs WHERE "
                                 "project_id = :p AND finalize_ctx->>'deferred_from_run' = :r"),
                            {"p": project, "r": recheck}).mappings().all()
        assert len(fix) == 1 and fix[0]["finalize_ctx"]["root_run_id"] == root
        assert fix[0]["title"].startswith("Follow-up: checks failing")
    finally:
        _cleanup(eng, project)


def test_the_pipeline_lets_a_recheck_start_no_build():
    src = inspect.getsource(g)
    spawn = src.index("        # A PR that opened with its checks failing always gets its follow-up")
    guard = src.index('_followup_ok, _followup_skip_reason = False, "a re-check never starts a build"')
    assert guard < spawn
    assert "_change_recheck.raise_fix_card(" in src[guard - 1500:guard]
    assert "ci_repair_run = (None if _recheck else" in src


def test_every_commit_goes_through_the_one_hold_and_a_recheck_holds_them_all():
    """R2: every commit or upsert in the pipeline is behind _push_held, and a re-check holds
    every label — a checkpoint after gate prep dirtied the tree (a lockfile, a formatter), the
    rescue, the partial PR, the preview declaration and the PRD record alike."""
    import re

    src = inspect.getsource(g)
    body = src[src.index("def _run_governed_code_task("):]
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if re.search(r"\b(commit_change|upsert_file|commit_files)\(", line) and "def " not in line:
            window = "\n".join(lines[max(0, i - 14):i])
            assert "_push_held(" in window, f"a commit not behind _push_held: {line.strip()}"
    labels = set(re.findall(r'_push_held\(f?"([^"{]+)', body))
    assert {"stage_commit", "declare_preview_app", "prd_record", "partial_pr",
            "checkpoint:", "repair_handoff"} <= labels | {"checkpoint:"}
    for label in labels:
        assert cr.push_hold_reason(True, None) == cr.PUSH_HOLD, label
    assert cr.push_hold_reason(False, None) is None
    assert cr.push_hold_reason(False, "template") == "template"
    assert "reason = _change_recheck.push_hold_reason(" in body
    assert 'if not _recheck and _push_held("pull_request"):' in body


def test_a_recheck_keeps_the_last_judged_table_when_its_own_does_not_judge():
    """M1: seeded from the run it re-checks; a refused or unrun check never replaces it."""
    judged_before = {"verdict": "not_met", "counts": {"met": 25, "partial": 5},
                     "items": [{"n": 1, "status": "met"}]}
    of_run = {"state": {"acceptance": judged_before,
                        "acceptance_history": [{"verdict": "not_met", "counts": {"met": 25}}]}}
    history: list = []
    holder: dict = {}
    cr.seed_acceptance(of_run, history, holder)
    assert holder["record"]["counts"]["met"] == 25 and len(history) == 1
    not_run = {"verdict": "not_run", "why": "1 of 30 items were not answered", "items": []}
    stood = ag.keep_history(history, not_run, holder, keep_on_not_run=True)
    assert stood["counts"]["met"] == 25 and "later_check_refused" in stood
    refused = {"verdict": "not_run", "refused": True, "why": "wrong folder", "items": []}
    assert ag.keep_history(history, refused, holder, keep_on_not_run=True)["counts"]["met"] == 25
    met = {"verdict": "met", "counts": {"met": 30}, "items": []}
    assert ag.keep_history(history, met, holder, keep_on_not_run=True) is met
    assert len(history) == 4
    # A refused table is never seeded as the judged one.
    holder2: dict = {}
    cr.seed_acceptance({"state": {"acceptance": refused}}, [], holder2)
    assert holder2 == {}


def test_a_retry_of_a_recheck_stays_a_recheck(eng, project):  # noqa: F811
    """M2: retrying a re-check never turns it into a coding attempt."""
    from boss_engine.services.code_task_service import retry_code_task

    try:
        with eng.begin() as c:
            root = _run(c, project)
            c.execute(text("UPDATE code_task_runs SET finalize_ctx = jsonb_build_object("
                           "'root_run_id', id) WHERE id = :i"), {"i": root})
            recheck = _run(c, project, status="failed",
                           fctx={"root_run_id": root, "attempt": 5, cr.KEY: True,
                                 cr.OF_KEY: root})
        out = retry_code_task(eng, get_settings(), _Jobs(), recheck)
        with eng.begin() as c:
            fctx = c.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id = :i"),
                             {"i": out["run_id"]}).scalar()
        assert fctx[cr.KEY] is True and "seed_workspace_from" not in fctx
    finally:
        _cleanup(eng, project)


def test_a_recheck_records_a_route_for_every_writing_stage_the_pipeline_reads():
    """Northgate round 8 (crun_69899a6067014b5883c3): a re-check crashed with
    KeyError: 'implement' because its no-write stage left stage_routes empty while the
    pipeline reads stage_routes["implement"] after the stage. Every writing stage the
    pipeline indexes must get a route recorded on the re-check path."""
    import inspect
    import re as _re

    from boss_engine.services import change_recheck as cr
    from boss_engine.services import governed_code_task_service as gm

    src = inspect.getsource(gm._run_governed_code_task)
    read = set(_re.findall(r'stage_routes\["([a-z_]+)"\]', src))
    writing_read = read & set(cr.WRITING_STAGES)
    assert writing_read, "the pipeline reads a writing stage's route"
    branch = src.split("if _recheck and step in _change_recheck.WRITING_STAGES:", 1)[1]
    branch = branch.split("return _change_recheck.NoWriteAgent(step), None", 1)[0]
    assert "stage_routes[step] =" in branch
