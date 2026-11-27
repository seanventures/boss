"""A project with SEVERAL apps never builds on the bare default branch (R4, BOSSAPP-1432).

The customer's projects hold several apps; ours held one. Two gaps came from that difference:
New work with no app named quietly built a fresh app on `main` (RCA #9), and a repair started
from a preview built on empty `main` instead of the preview's app (RCA #7). Every door that
starts a build is driven here on ONE fixture — two completed apps, an open change, a live
preview, a failed attempt — and each build must land on the chosen (or originating) app's branch:

* New work: with no app chosen BOSS asks (Choose an application / Create a new app); with an
  app chosen the build clones that app's branch.
* Repair from preview: the start door, with only the preview named, builds the preview's app.
* Retry: the failed attempt's retry clones its app's branch.
* The agent's create_code_task (MCP, Ask BOSS): nothing named asks; a preview, a build or a task
  each lands on its app's branch; only an explicit new_app starts fresh.
* One resolver (new_work.build_mode) says which change: an open change continues on its
  branch, a merged one becomes a new change of the same app, a closed one is refused.
* A panel's approved PRD (the meeting's auto-started build) and the agent's chat fallback
  (intent_router's create_code_task) ask the same questions (BOSSAPP-1455): naming no app beside
  several asks which and builds nothing; naming a merged app starts a new change of it.

Where a build lands is read the way the pipeline reads it: build_base_branch.resolve_build_base,
then lineage_clone (the clone governed_code_task_service makes). The only thing replaced is the
panel (it calls a model).
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.tests.test_a_feature_builds_onto_its_app import (  # noqa: F401 — fixture
    _start as _start_workflow, _tick_until_built, panel,
)

A_ROOT, A_RUN = "crun_ngwroot0001", "crun_ngwchange01"          # Northgate Gateway: open change
B_ROOT, B_FAILED = "crun_lumroot0001", "crun_lumretry001"       # Lumen Pay Ledger: live preview
B_TASK, B_PREVIEW = "task_lumenledger", "prev_lumenledger"
OWNER = "acme"
#: The branches the repository has: the default branch and each app's lineage branch.
BRANCHES = {"main", f"boss/code/{A_ROOT}", f"boss/code/{B_ROOT}"}


def _project() -> str:
    return get_settings().default_project_id


def _insert(engine, run_id, *, title, repo, fctx, spec=None, status="completed", pr=None,
            task_id=None):
    spec = {"instructions": f"Build {title}.", "acceptance_criteria": ["it works"],
            "owner": OWNER, "repo": repo, "base_branch": "main", **(spec or {})}
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO code_task_runs (id,project_id,task_id,status,phase,title,"
            "pull_request_url,pull_request_number,spec,state,finalize_ctx) VALUES "
            "(:id,:p,:task,:s,'done',:t,:pr,:n,CAST(:spec AS JSONB),'{}'::jsonb,"
            "CAST(:fctx AS JSONB))"),
            {"id": run_id, "p": _project(), "task": task_id, "s": status, "t": title,
             "pr": f"https://github.com/{OWNER}/{repo}/pull/{pr}" if pr else None, "n": pr,
             "spec": json.dumps(spec), "fctx": json.dumps(fctx)})


@pytest.fixture()
def apps(engine):
    """Northgate Gateway: its first build and a change on its branch, PR #4 open. Lumen Pay
    Ledger: one completed build (the lineage root, PR #7 open) with a live preview, and a later
    attempt that failed."""
    _insert(engine, A_ROOT, title="Northgate Gateway", repo="northgate-gateway", pr=4,
            fctx={"attempt": 1, "app_id": "app_northgate"})
    _insert(engine, A_RUN, title="Northgate Gateway", repo="northgate-gateway", pr=4,
            fctx={"attempt": 2, "root_run_id": A_ROOT, "parent_run_id": A_ROOT,
                  "is_followup": True, "app_id": "app_northgate"},
            spec={"base_branch": f"boss/code/{A_ROOT}"})
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO tasks (id, project_id, title, description, status, dedupe_key) VALUES "
            "(:t, :p, 'Lumen Pay ledger', 'The ledger app', 'in_progress', :t) "
            "ON CONFLICT (id) DO NOTHING"), {"t": B_TASK, "p": _project()})
    _insert(engine, B_ROOT, title="Lumen Pay Ledger", repo="lumen-pay-ledger", pr=7,
            task_id=B_TASK, fctx={"attempt": 1, "app_id": "app_lumen"})
    _insert(engine, B_FAILED, title="Lumen Pay Ledger", repo="lumen-pay-ledger",
            status="failed", task_id=B_TASK,
            fctx={"attempt": 2, "root_run_id": B_ROOT, "parent_run_id": B_ROOT,
                  "app_id": "app_lumen"},
            spec={"base_branch": "main"})
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO app_previews (id, project_id, run_id, repo, branch, status, url) "
            "VALUES (:i, :p, :r, :repo, :b, 'running', 'http://127.0.0.1:4100/')"),
            {"i": B_PREVIEW, "p": _project(), "r": B_ROOT, "repo": f"{OWNER}/lumen-pay-ledger",
             "b": f"boss/code/{B_ROOT}"})
    yield


def _start_from(client, run_input):
    """The start door (POST /v1/delivery-workflows/start) with the catalogue installed."""
    installed = client.post("/v1/delivery-workflows/catalogue/install",
                            json={"project_id": _project(), "actor": "ceo"})
    assert installed.status_code == 200, installed.text
    return _start_workflow(client, run_input)


def _run(engine, run_id) -> dict:
    with engine.begin() as connection:
        return dict(connection.execute(text(
            "SELECT id, project_id, status, spec, finalize_ctx FROM code_task_runs "
            "WHERE id = :i"), {"i": run_id}).mappings().one())


def _clones(engine, run_id) -> str:
    """The branch the pipeline would clone for this build (governed_code_task_service)."""
    from boss_engine.services.build_base_branch import lineage_clone, resolve_build_base

    run = _run(engine, run_id)
    spec = run["spec"] or {}
    with engine.begin() as connection:
        base = resolve_build_base(connection, project_id=run["project_id"], owner=spec["owner"],
                                  repo=spec["repo"], explicit=spec.get("base_branch"),
                                  host_default=lambda: "main", run_id=run_id)
    return lineage_clone(run_id, run["finalize_ctx"], base.clone, BRANCHES.__contains__)[1]


def _count(engine, table) -> int:
    with engine.begin() as connection:
        return connection.execute(text(f"SELECT count(*) FROM {table}")).scalar()  # noqa: S608


def _post_work(client, body):
    """POST …/work as the New work modal does, past what this test project lacks (a model, a
    host): what is tested is where the work goes."""
    client.post("/v1/delivery-workflows/catalogue/install",
                json={"project_id": _project(), "actor": "ceo"})
    path = f"/v1/projects/{_project()}/work"
    r = client.post(path, json=body)
    if r.status_code == 409 and isinstance(r.json().get("detail"), dict):
        detail = r.json()["detail"]
        ready = detail.get("readiness") if isinstance(detail.get("readiness"), dict) else detail
        missing = [m["key"] for m in (ready.get("missing") or [])]
        if missing:
            r = client.post(path, json={**body, "start_anyway": missing})
    return r


def _built_approving_gates(client, engine, workflow_run_id, ticks=12) -> list[dict]:
    """Tick the real runner as _tick_until_built does, approving (as the person, over HTTP) each
    gate this run raises on the way to its build: New work's feature path asks for the PRD."""
    for _ in range(ticks):
        with engine.begin() as connection:
            pending = connection.execute(text(
                "SELECT d.id FROM decisions d JOIN workflow_step_runs s ON s.decision_id = d.id "
                "WHERE s.workflow_run_id = :w AND d.status = 'pending'"),
                {"w": workflow_run_id}).scalars().all()
        for decision_id in pending:
            approved = client.post(f"/v1/decisions/{decision_id}/approve",
                                   json={"actor": "ceo", "note": "the PRD is right"})
            assert approved.status_code in (200, 202), approved.text
        try:
            return _tick_until_built(client, engine, workflow_run_id, ticks=1)
        except AssertionError:
            continue
    raise AssertionError("the build step never started a code task")


def _tool(engine, **args) -> dict:
    """create_code_task the way an MCP client or Ask BOSS's agent calls it."""
    from boss_engine.context import EngineContext
    from boss_engine.tools import build_default_registry

    ctx = EngineContext(settings=get_settings(), engine=engine)
    return build_default_registry().invoke("create_code_task", ctx, project_id=_project(),
                                           actor="ceo", args={"title": "Ledger fix", **args})


# ── New work ────────────────────────────────────────────────────────────────────────────────

def test_new_work_with_no_app_chosen_asks_which(client, engine, apps):
    plan = client.get(f"/v1/projects/{_project()}/work/plan", params={"kind": "feature"})
    assert plan.status_code == 200, plan.text
    assert plan.json()["anchor"] is None
    assert {c["run_id"] for c in plan.json()["app_choices"]} == {A_RUN, B_ROOT}
    assert "Create a new app" in plan.json()["anchor_error"]

    before = _count(engine, "workflow_runs")
    asked = _post_work(client, {"kind": "feature", "text": "Add an audit page.",
                                "infer_app": True})
    assert asked.status_code == 422, asked.text
    assert asked.json()["detail"]["error"] == "app_selection_required"
    message = asked.json()["detail"]["message"]
    assert "Northgate Gateway" in message and "Lumen Pay Ledger" in message
    assert _count(engine, "workflow_runs") == before


def test_new_work_for_the_chosen_app_builds_on_its_branch(client, engine, apps, panel):
    started = _post_work(client, {"kind": "feature", "app": B_ROOT, "text": "Add an audit page."})
    assert started.status_code == 200, started.text
    built = _built_approving_gates(client, engine, started.json()["run_id"])
    assert len(built) == 1
    assert _clones(engine, built[0]["id"]) == f"boss/code/{B_ROOT}"


# ── Repair from preview ─────────────────────────────────────────────────────────────────────

def test_a_repair_from_a_preview_builds_on_that_previews_app(client, engine, apps, panel):
    started = _start_from(client, {"title": "Repair the ledger preview",
                                       "directive": "The totals row is blank.",
                                       "preview_id": B_PREVIEW})
    assert started.status_code == 200, started.text
    assert started.json()["extends"]["run_id"] == B_ROOT
    assert started.json()["extends"]["branch"] == f"boss/code/{B_ROOT}"
    built = _tick_until_built(client, engine, started.json()["run"]["id"])
    assert len(built) == 1
    assert _clones(engine, built[0]["id"]) == f"boss/code/{B_ROOT}"
    assert f"/v1/previews/{B_PREVIEW}/web/" in built[0]["spec"]["instructions"]


# ── Retry ───────────────────────────────────────────────────────────────────────────────────

def test_a_retry_builds_on_its_apps_branch(client, engine, apps):
    retried = client.post(f"/v1/code-tasks/{B_FAILED}/retry", json={"actor": "ceo"})
    assert retried.status_code == 200, retried.text
    new_id = retried.json().get("run_id") or retried.json().get("new_run_id")
    assert new_id and new_id != B_FAILED, retried.json()
    assert _clones(engine, new_id) == f"boss/code/{B_ROOT}"


# ── The agent's create_code_task (MCP / Ask BOSS) ───────────────────────────────────────────

def test_the_agent_naming_no_app_is_asked_which(engine, apps):
    before = _count(engine, "code_task_runs")
    out = _tool(engine, instructions="Fix the totals row.")
    assert out.get("ok") is False and out.get("needs_app_choice") is True, out
    assert "Northgate Gateway" in out["error"] and "Lumen Pay Ledger" in out["error"]
    assert "new_app" in out["error"]
    assert {c["run_id"] for c in out["app_choices"]} == {A_RUN, B_ROOT}
    assert _count(engine, "code_task_runs") == before


@pytest.mark.parametrize("named, branch", [
    ({"preview_id": B_PREVIEW}, f"boss/code/{B_ROOT}"),
    ({"extends_run_id": A_RUN}, f"boss/code/{A_ROOT}"),
    # The task's app is its lineage ROOT (no root_run_id): the old lookup found no branch and
    # started a fresh build on main beside the app.
    ({"task_id": B_TASK}, f"boss/code/{B_ROOT}"),
])
def test_the_agent_naming_an_app_builds_on_that_apps_branch(engine, apps, named, branch):
    out = _tool(engine, instructions="Fix the totals row.", **named)
    assert out.get("status") == "queued", out
    assert out["extended_existing_app"] is True
    assert _clones(engine, out["run_id"]) == branch


def test_only_an_explicit_new_app_starts_fresh_beside_the_apps(engine, apps, monkeypatch):
    # The repository a project with no binding builds in (repo_registry's legacy fallback).
    monkeypatch.setattr(get_settings(), "target_github_owner", OWNER)
    monkeypatch.setattr(get_settings(), "target_github_repo", "payroll-export")
    out = _tool(engine, instructions="A new payroll export app.", new_app=True)
    assert out.get("status") == "queued", out
    assert not out["extended_existing_app"]
    both = _tool(engine, instructions="x", new_app=True, preview_id=B_PREVIEW)
    assert both.get("ok") is False and "not both" in both["error"]


# ── Which change: one resolver for every door ───────────────────────────────────────────────

def _merge(engine, run_id):
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE code_task_runs SET finalize_ctx = finalize_ctx || CAST(:m AS JSONB) "
            "WHERE id = :i"), {"i": run_id, "m": json.dumps({"merge": {"sha": "b" * 40}})})


def _close(engine, run_id):
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, status) "
            "VALUES ('dec_lumclosed01', :p, 'Merge the ledger change?', '{}'::jsonb, "
            "'rejected') ON CONFLICT (id) DO NOTHING"), {"p": _project()})
        connection.execute(text("UPDATE code_task_runs SET decision_id = 'dec_lumclosed01' "
                                "WHERE id = :i"), {"i": run_id})


def test_a_repair_of_a_merged_app_is_a_new_change_never_a_push_onto_merged_history(
        engine, apps):
    _merge(engine, B_ROOT)
    out = _tool(engine, instructions="Fix the totals row.", preview_id=B_PREVIEW)
    assert out.get("status") == "queued", out
    run = _run(engine, out["run_id"])
    fctx = run["finalize_ctx"]
    assert fctx.get("after_merged_run_id") == B_ROOT and fctx.get("app_id") == "app_lumen"
    assert _clones(engine, out["run_id"]) != f"boss/code/{B_ROOT}"


def test_a_repair_from_the_preview_of_a_merged_app_starts_a_new_change(
        client, engine, apps, panel):
    _merge(engine, B_ROOT)
    started = _start_from(client, {"title": "Repair the ledger preview",
                                       "directive": "The totals row is blank.",
                                       "preview_id": B_PREVIEW})
    assert started.status_code == 200, started.text
    assert started.json()["extends"]["branch"] == "main"      # the merged code is there
    built = _tick_until_built(client, engine, started.json()["run"]["id"])
    assert built[0]["finalize_ctx"].get("after_merged_run_id") == B_ROOT
    assert _clones(engine, built[0]["id"]) != f"boss/code/{B_ROOT}"


def test_a_repair_after_a_rejected_merge_keeps_the_apps_branch(client, engine, apps, panel):
    """"Not yet" on the merge card, then a change: the work continues the app's own branch —
    never main. (Only New work's modal refuses a closed change, offering Create a new app.)"""
    _close(engine, B_ROOT)
    out = _tool(engine, instructions="Fix the totals row.", preview_id=B_PREVIEW)
    assert out.get("status") == "queued", out
    assert _clones(engine, out["run_id"]) == f"boss/code/{B_ROOT}"


def test_an_incident_fix_is_not_rebased_at_the_start_door(client, engine, apps):
    """An incident's fix is cut from the serving commit at its build step; the door does not turn
    it into a new change of a merged app."""
    _merge(engine, B_ROOT)
    started = _start_from(client, {"title": "Fix the outage", "directive": "x",
                                   "extends_run_id": B_ROOT, "incident_id": "inc_nothere"})
    if started.status_code == 200:
        assert started.json()["extends"]["branch"] == f"boss/code/{B_ROOT}"
        with engine.begin() as connection:
            run_input = connection.execute(text("SELECT input FROM workflow_runs WHERE id = :r"),
                                           {"r": started.json()["run"]["id"]}).scalar()
        assert not run_input.get("extends_from_default")
    else:   # the door may refuse an unknown incident; it never re-bases it
        assert "extends_from_default" not in started.text


# ── A panel's approved PRD: the meeting's auto-started build (BOSSAPP-1455) ─────────────────

def _prd_follow_on(engine, monkeypatch, *, extends=None, task="task_auditpage") -> tuple:
    """Approve a panel's PRD-publish decision and run its follow-on (the auto-started build)
    as decisions does; the thread a fresh PRD build is launched on is recorded, not run."""
    from boss_engine.services import decisions

    launched: list = []
    monkeypatch.setattr(get_settings(), "auto_code_task_after_prd", True)
    monkeypatch.setattr(decisions, "_spawn_as_this_request", lambda fn: launched.append(fn))
    rec = {"action": "execute_agent_tool", "tool": "publish_document", "start_code_task": True,
           "args": {"title": "PRD: Audit page", "markdown": "# Audit page\nShow who did what.",
                    "repo_subdir": "docs/prd"},
           "task_id": task, **({"extends_run_id": extends} if extends else {})}
    decision_id = f"dec_prd_{task[-8:]}"
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO tasks (id, project_id, title, description, status, dedupe_key) VALUES "
            "(:t, :p, 'Audit page', 'An audit page', 'in_progress', :t) "
            "ON CONFLICT (id) DO NOTHING"), {"t": task, "p": _project()})
        connection.execute(text(
            "INSERT INTO decisions (id, project_id, task_id, question, recommendation, status) "
            "VALUES (:d, :p, :t, 'Publish this PRD?', CAST(:r AS JSONB), 'approved')"),
            {"d": decision_id, "p": _project(), "t": task, "r": json.dumps(rec)})
        row = dict(connection.execute(text("SELECT * FROM decisions WHERE id = :d"),
                                      {"d": decision_id}).mappings().one())
        decisions.DecisionService(get_settings())._start_code_task_after_prd(
            connection, row, "ceo", rec)
    return decision_id, launched


def test_a_panels_prd_that_names_no_app_never_builds_beside_several(engine, apps, monkeypatch):
    before = _count(engine, "code_task_runs")
    decision_id, launched = _prd_follow_on(engine, monkeypatch)
    assert launched == [], "a fresh build on the default branch was launched beside the apps"
    assert _count(engine, "code_task_runs") == before
    with engine.begin() as connection:
        said = connection.execute(text(
            "SELECT data FROM events WHERE event_type = 'code_task.not_started' "
            "AND data->>'decision_id' = :d"), {"d": decision_id}).scalar()
        note = connection.execute(text("SELECT resolution_note FROM decisions WHERE id = :d"),
                                  {"d": decision_id}).scalar()
    assert said["reason"] == "app_choice_needed"
    assert {c["run_id"] for c in said["app_choices"]} == {A_RUN, B_ROOT}
    assert "Northgate Gateway" in note and "Lumen Pay Ledger" in note
    assert "New work" in note and "Create a new app" in note


def test_a_panels_prd_for_a_merged_app_is_a_new_change_of_it(engine, apps, monkeypatch):
    _merge(engine, B_ROOT)
    decision_id, launched = _prd_follow_on(engine, monkeypatch, extends=B_ROOT,
                                           task="task_ledgeraudit")
    assert launched == []
    with engine.begin() as connection:
        run_id = connection.execute(text(
            "SELECT id FROM code_task_runs WHERE finalize_ctx->'origin'->>'decision_id' = :d"),
            {"d": decision_id}).scalar()
    assert run_id, "the PRD's build onto the app did not start"
    assert _run(engine, run_id)["finalize_ctx"].get("after_merged_run_id") == B_ROOT
    assert _clones(engine, run_id) != f"boss/code/{B_ROOT}"


def test_a_panels_prd_for_an_open_app_continues_its_branch(engine, apps, monkeypatch):
    decision_id, _ = _prd_follow_on(engine, monkeypatch, extends=B_ROOT, task="task_ledgeropen")
    with engine.begin() as connection:
        run_id = connection.execute(text(
            "SELECT id FROM code_task_runs WHERE finalize_ctx->'origin'->>'decision_id' = :d"),
            {"d": decision_id}).scalar()
    assert run_id and _clones(engine, run_id) == f"boss/code/{B_ROOT}"


# ── The agent's chat fallback: intent_router's create_code_task (BOSSAPP-1455) ──────────────

def _router_creates(engine, external_ref=None):
    from boss_engine.services.intent_router import IntentRouter

    with engine.begin() as connection:
        return IntentRouter(get_settings())._dispatch(
            connection, "create_code_task",
            {"title": "Totals row", "instructions": "Fix the totals row."},
            project_id=_project(), actor="executive_console", external_ref=external_ref)


def test_the_chat_fallback_naming_no_app_asks_which(engine, apps):
    before = _count(engine, "code_task_runs")
    turn = _router_creates(engine)
    assert "Northgate Gateway" in turn.reply and "Lumen Pay Ledger" in turn.reply
    assert turn.actions[0]["kind"] == "app_choice_needed"
    assert {c["run_id"] for c in turn.actions[0]["app_choices"]} == {A_RUN, B_ROOT}
    assert _count(engine, "code_task_runs") == before


def test_the_chat_fallback_scoped_to_an_app_builds_on_its_branch(engine, apps):
    turn = _router_creates(engine, external_ref=B_FAILED)
    assert turn.actions[0]["kind"] == "code_task_created"
    assert _clones(engine, turn.actions[0]["run_id"]) == f"boss/code/{B_ROOT}"


def test_the_chat_fallback_for_a_merged_app_starts_a_new_change(engine, apps):
    _merge(engine, B_ROOT)
    turn = _router_creates(engine, external_ref=B_ROOT)
    run = _run(engine, turn.actions[0]["run_id"])
    assert run["finalize_ctx"].get("after_merged_run_id") == B_ROOT
    assert _clones(engine, run["id"]) != f"boss/code/{B_ROOT}"
    assert "new change" in turn.reply


# ── Two apps in ONE repository (ENT-FEAT-01, ENT-REC-50) ────────────────────────────────────
#
# Recipe Box and Neighborhood Board share local_northgate/northgate; each change is open on its
# own lineage branch. A new app is said so all the way to its build (app_scope.NEW_APP_KEY, read
# by the empty-checkout guard), and every door that names one app lands on that app's branch.

R_ROOT, N_ROOT, N_FAILED = "crun_reciperoot01", "crun_boardroot001", "crun_boardretry01"
R_PREVIEW, N_PREVIEW = "prev_recipebox01", "prev_neighboard1"
SHARED = "northgate"


@pytest.fixture()
def one_repo(engine):
    _insert(engine, R_ROOT, title="Recipe Box", repo=SHARED, pr=1,
            fctx={"attempt": 1, "app_id": "app_recipebox"})
    _insert(engine, N_ROOT, title="Neighborhood Board", repo=SHARED, pr=2,
            fctx={"attempt": 1, "app_id": "app_neighborhood", "new_app": True})
    _insert(engine, N_FAILED, title="Neighborhood Board", repo=SHARED, status="failed",
            fctx={"attempt": 2, "root_run_id": N_ROOT, "parent_run_id": N_ROOT,
                  "app_id": "app_neighborhood", "new_app": True},
            spec={"base_branch": "main"})
    with engine.begin() as connection:
        for preview, run in ((R_PREVIEW, R_ROOT), (N_PREVIEW, N_ROOT)):
            connection.execute(text(
                "INSERT INTO app_previews (id, project_id, run_id, repo, branch, status, url) "
                "VALUES (:i, :p, :r, :repo, :b, 'running', 'http://127.0.0.1:4100/')"),
                {"i": preview, "p": _project(), "r": run, "repo": f"{OWNER}/{SHARED}",
                 "b": f"boss/code/{run}"})
    yield


def _clones_shared(engine, run_id) -> str:
    from boss_engine.services.build_base_branch import lineage_clone, resolve_build_base

    run = _run(engine, run_id)
    spec = run["spec"] or {}
    with engine.begin() as connection:
        base = resolve_build_base(connection, project_id=run["project_id"], owner=spec["owner"],
                                  repo=spec["repo"], explicit=spec.get("base_branch"),
                                  host_default=lambda: "main", run_id=run_id)
    branches = {"main", f"boss/code/{R_ROOT}", f"boss/code/{N_ROOT}"}
    return lineage_clone(run_id, run["finalize_ctx"], base.clone, branches.__contains__)[1]


@pytest.mark.parametrize("preview, branch, app_id", [
    (N_PREVIEW, f"boss/code/{N_ROOT}", "app_neighborhood"),
    (R_PREVIEW, f"boss/code/{R_ROOT}", "app_recipebox"),
])
def test_a_repair_from_a_preview_targets_that_previews_app_in_a_shared_repository(
        engine, one_repo, preview, branch, app_id):
    out = _tool(engine, instructions="The list is empty.", preview_id=preview)
    assert out.get("status") == "queued", out
    assert _clones_shared(engine, out["run_id"]) == branch
    assert _run(engine, out["run_id"])["finalize_ctx"].get("app_id") == app_id


def test_a_repair_from_a_preview_at_the_start_door_targets_that_app(client, engine, one_repo,
                                                                    panel):
    started = _start_from(client, {"title": "Repair the board preview",
                                   "directive": "The list is empty.",
                                   "preview_id": N_PREVIEW})
    assert started.status_code == 200, started.text
    assert started.json()["extends"]["run_id"] == N_ROOT
    built = _tick_until_built(client, engine, started.json()["run"]["id"])
    assert _clones_shared(engine, built[0]["id"]) == f"boss/code/{N_ROOT}"


def test_a_retry_in_a_shared_repository_stays_on_its_app(client, engine, one_repo):
    retried = client.post(f"/v1/code-tasks/{N_FAILED}/retry", json={"actor": "ceo"})
    assert retried.status_code == 200, retried.text
    new_id = retried.json().get("run_id") or retried.json().get("new_run_id")
    assert _clones_shared(engine, new_id) == f"boss/code/{N_ROOT}"
    fctx = _run(engine, new_id)["finalize_ctx"]
    assert fctx.get("app_id") == "app_neighborhood" and fctx.get("new_app") is True


def test_create_a_new_app_is_said_all_the_way_to_the_build(client, engine, one_repo, panel,
                                                           monkeypatch):
    """New work › Create a new app: the run's input and its build both say new_app, and the
    build clones the default branch (not either app's branch)."""
    monkeypatch.setattr(get_settings(), "target_github_owner", OWNER)
    monkeypatch.setattr(get_settings(), "target_github_repo", SHARED)
    started = _post_work(client, {"kind": "feature", "new_app": True,
                                  "text": "A parking rota for the street."})
    assert started.status_code == 200, started.text
    with engine.begin() as connection:
        run_input = connection.execute(text("SELECT input FROM workflow_runs WHERE id = :r"),
                                       {"r": started.json()["run_id"]}).scalar()
    assert run_input.get("new_app") is True and "extends_run_id" not in run_input
    built = _built_approving_gates(client, engine, started.json()["run_id"])
    assert built[0]["finalize_ctx"].get("new_app") is True
    assert _clones_shared(engine, built[0]["id"]) == "main"


def test_the_agents_new_app_is_recorded_on_the_build(engine, one_repo, monkeypatch):
    monkeypatch.setattr(get_settings(), "target_github_owner", OWNER)
    monkeypatch.setattr(get_settings(), "target_github_repo", SHARED)
    out = _tool(engine, instructions="A parking rota for the street.", new_app=True)
    assert out.get("status") == "queued", out
    assert _run(engine, out["run_id"])["finalize_ctx"].get("new_app") is True
    named = _tool(engine, instructions="Fix the list.", extends_run_id=N_ROOT)
    assert "new_app" not in _run(engine, named["run_id"])["finalize_ctx"]


def test_the_start_door_refuses_a_new_app_that_also_names_an_app(client, engine, one_repo):
    refused = _start_from(client, {"title": "Both", "directive": "x", "new_app": True,
                                   "extends_run_id": N_ROOT})
    assert refused.status_code == 422, refused.text
    assert "not both" in refused.text
