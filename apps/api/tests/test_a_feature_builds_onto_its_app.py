"""A feature started on a workflow for an app that exists builds ONTO that app.

"Add a feature to an existing app" had one honest route — the quick change — and a workflow
started for the same feature built a NEW app: its build branched from main, scaffolded, and the
Factory showed a second app beside the first. `extends_run_id` names the app's build; every
build step of the run then continues that app (its branch, repository and lineage), the panel is
told it is specifying a change and not a product, and the Factory counts the result as the app's
next build.

Everything goes through the HTTP API on the real database. The panel is the one thing replaced,
because convening one calls a model: the stand-in lands a finished meeting with a PRD, exactly
where the runner looks for the real one.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app.config import get_settings

APP_ROOT = "crun_scoreroot"
APP_RUN = "crun_scorelatest"


def _project() -> str:
    return get_settings().default_project_id


def _insert_run(engine, run_id, *, title="Scorecard", fctx=None, spec=None, status="completed"):
    spec = {"instructions": "Build a scorecard.", "acceptance_criteria": ["it scores"],
            "test_commands": ["python3 -m pytest"], "owner": "acme", "repo": "scorecard",
            "base_branch": "main", **(spec or {})}
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO code_task_runs (id,project_id,status,phase,title,pull_request_url,"
            "spec,state,finalize_ctx) VALUES (:id,:p,:s,'done',:t,:pr,"
            "CAST(:spec AS JSONB),'{}'::jsonb,CAST(:fctx AS JSONB))"
        ), {"id": run_id, "p": _project(), "s": status, "t": title,
            "pr": f"https://github.com/acme/scorecard/pull/{len(run_id)}",
            "spec": json.dumps(spec), "fctx": json.dumps(fctx or {})})


def _the_app(engine) -> None:
    """An app with two builds already: the root and a change on its branch."""
    _insert_run(engine, APP_ROOT, fctx={"attempt": 1, "app_id": "app_scorecard"})
    _insert_run(engine, APP_RUN, title="Scorecard",
                fctx={"attempt": 1, "root_run_id": APP_ROOT, "parent_run_id": APP_ROOT,
                      "is_followup": True, "app_id": "app_scorecard"},
                spec={"base_branch": f"boss/code/{APP_ROOT}"})


def _start(client, run_input, **extra):
    body = {"project_id": _project(), "actor": "ceo", "workflow_key": "minor_feature",
            "input": run_input, **extra}
    started = client.post("/v1/delivery-workflows/start", json=body)
    if started.status_code == 409 and isinstance(started.json().get("detail"), dict):
        # "Start anyway" on whatever this test project lacks (a connected model, a host):
        # what is being tested is where the build goes, not whether the project is set up.
        missing = [m["key"] for m in started.json()["detail"]["readiness"]["missing"]]
        started = client.post("/v1/delivery-workflows/start",
                              json={**body, "start_anyway": missing})
    return started


@pytest.fixture()
def panel(monkeypatch, engine):
    """The meeting step's tool, replaced by a finished meeting with a PRD in it."""
    from boss_engine.services import workflow_capability_runner as runner

    real = runner._invoke_tool
    asked: list[dict] = []

    def invoke(ctx, tool_name, project_id, actor, args):
        if tool_name != "request_meeting":
            return real(ctx, tool_name, project_id, actor, args)
        asked.append(dict(args))
        meeting_id = f"mrun_{uuid4().hex[:10]}"
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO meeting_runs (id,project_id,status,phase,directive,state) "
                "VALUES (:id,:p,'completed','done',:d,CAST(:s AS JSONB))"
            ), {"id": meeting_id, "p": project_id, "d": str(args.get("directive") or ""),
                "s": json.dumps({"prd_markdown": "# PRD\nA Download CSV button on the "
                                                 "approvals page."})})
        return {"status": "started", "meeting_run_id": meeting_id}

    monkeypatch.setattr(runner, "_invoke_tool", invoke)
    return asked


def _tick_until_built(client, engine, workflow_run_id, ticks=8):
    for _ in range(ticks):
        ticked = client.post("/v1/delivery-workflows/run-capabilities",
                             json={"project_id": _project()})
        assert ticked.status_code == 200, ticked.text
        with engine.begin() as connection:
            built = connection.execute(text(
                "SELECT id, title, spec, finalize_ctx FROM code_task_runs "
                "WHERE finalize_ctx->>'workflow_run_id' = :w"
            ), {"w": workflow_run_id}).mappings().all()
        if built:
            return [dict(b) for b in built]
    raise AssertionError("the build step never started a code task")


def test_a_workflow_started_for_an_app_builds_onto_it_and_is_its_next_build(
        client, engine, panel):
    _the_app(engine)
    installed = client.post("/v1/delivery-workflows/catalogue/install",
                            json={"project_id": _project(), "actor": "ceo"})
    assert installed.status_code == 200, installed.text
    with engine.begin() as connection:
        runs_before = connection.execute(text("SELECT count(*) FROM workflow_runs")).scalar()

    started = _start(client, {
        "title": "Export swaps as CSV",
        "directive": "Add a Download CSV button to the approvals page.",
        "acceptance_criteria": ["The button downloads this month's approved swaps."],
        "extends_run_id": APP_RUN,
    })
    assert started.status_code == 200, started.text
    body = started.json()
    workflow_run_id = body["run"]["id"]
    assert body["extends"] == {"run_id": APP_RUN, "app": "Scorecard",
                               "branch": f"boss/code/{APP_ROOT}"}

    built = _tick_until_built(client, engine, workflow_run_id)

    # The panel was told it is specifying a change to a working app, not a product.
    assert panel, "the meeting step never ran"
    objective = panel[0].get("objective") or ""
    assert "EXISTING, working app" in objective and f"boss/code/{APP_ROOT}" in objective

    # ONE build, on the app's own branch and repository, in the app's lineage.
    assert len(built) == 1
    run = built[0]
    spec, fctx = run["spec"], run["finalize_ctx"]
    assert spec["base_branch"] == f"boss/code/{APP_ROOT}"
    assert (spec["owner"], spec["repo"]) == ("acme", "scorecard")
    assert fctx["app_id"] == "app_scorecard"
    assert fctx["root_run_id"] == APP_ROOT
    assert fctx["parent_run_id"] == APP_RUN
    assert fctx["is_followup"] is True
    assert fctx["workflow_run_id"] == workflow_run_id
    assert "The button downloads this month's approved swaps." in spec["acceptance_criteria"]
    assert any("existing tests still pass" in c for c in spec["acceptance_criteria"])
    assert "A Download CSV button" in spec["instructions"]  # the panel's PRD is the brief

    # No second workflow run was opened for the build the workflow itself started.
    with engine.begin() as connection:
        runs_after = connection.execute(text("SELECT count(*) FROM workflow_runs")).scalar()
    assert runs_after == runs_before + 1

    # The Factory shows it as the app's third build — not as a new app.
    apps = client.get("/v1/factory/apps", params={"project_id": _project()})
    assert apps.status_code == 200, apps.text
    rows = [a for a in apps.json()["apps"]
            if {APP_ROOT, APP_RUN, run["id"]} & set(a["run_ids"])]
    assert len(rows) == 1, rows
    assert rows[0]["builds"] == 3
    assert set(rows[0]["run_ids"]) == {APP_ROOT, APP_RUN, run["id"]}
    assert rows[0]["app"] == "Scorecard"
    # ONE APP, ITS THREE HANDLES IN ONE ROW (tester P2, "one app under 3 keys"): its newest build,
    # its id (App › Security) and its key (templates, secrets, Setup, links) — so no surface
    # derives one from another, or shows a key where the name belongs.
    from boss_engine.services.agent_assignment import app_key
    assert rows[0]["app_id"] == "app_scorecard"
    assert rows[0]["app_key"] == app_key("Scorecard")


def test_a_lone_app_gets_its_second_build(client, engine, panel):
    """The plainest case: one build, no lineage yet. The new build is build two of it."""
    _insert_run(engine, "crun_lonely", title="Lonely Notes", fctx={"attempt": 1})
    client.post("/v1/delivery-workflows/catalogue/install",
                json={"project_id": _project(), "actor": "ceo"})
    started = _start(client, {"title": "Pin a note", "directive": "Let a note be pinned.",
                              "extends_run_id": "crun_lonely"})
    assert started.status_code == 200, started.text
    built = _tick_until_built(client, engine, started.json()["run"]["id"])
    assert built[0]["spec"]["base_branch"] == "boss/code/crun_lonely"
    rows = [a for a in client.get("/v1/factory/apps",
                                  params={"project_id": _project()}).json()["apps"]
            if "crun_lonely" in a["run_ids"]]
    assert len(rows) == 1 and rows[0]["builds"] == 2
    assert built[0]["id"] in rows[0]["run_ids"]


def test_an_app_that_is_not_in_the_project_is_refused_before_anything_starts(client, engine):
    client.post("/v1/delivery-workflows/catalogue/install",
                json={"project_id": _project(), "actor": "ceo"})
    with engine.begin() as connection:
        before = connection.execute(text("SELECT count(*) FROM workflow_runs")).scalar()
    refused = _start(client, {"title": "x", "directive": "x", "extends_run_id": "crun_nowhere"})
    assert refused.status_code == 404
    assert refused.json()["detail"] == "that app is not in this project"

    _insert_run(engine, "crun_fixture", status="simulated",
                spec={"simulated": True})
    refused = _start(client, {"title": "x", "directive": "x", "extends_run_id": "crun_fixture"})
    assert refused.status_code == 422
    with engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM workflow_runs")).scalar() == before


def test_a_blank_extends_run_id_is_a_new_app_as_before(client, engine):
    client.post("/v1/delivery-workflows/catalogue/install",
                json={"project_id": _project(), "actor": "ceo"})
    started = _start(client, {"title": "New thing", "directive": "Build a thing.",
                              "extends_run_id": "  "})
    assert started.status_code == 200, started.text
    assert "extends" not in started.json()
    with engine.begin() as connection:
        stored = connection.execute(text("SELECT input FROM workflow_runs WHERE id = :r"),
                                    {"r": started.json()["run"]["id"]}).scalar()
    assert "extends_run_id" not in stored


# ── What each workflow has cost here ──────────────────────────────────────────────────────────


def _call(engine, run_id, usd, *, included=False):
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO llm_calls (id,project_id,code_task_run_id,stage,cost_usd_micros,"
            "included_in_subscription) VALUES (:id,:p,:r,'implement',:m,:i)"
        ), {"id": f"llm_{uuid4().hex[:10]}", "p": _project(), "r": run_id,
            "m": int(usd * 1_000_000), "i": included})


def _workflow_run(client, engine, title, *, status="completed"):
    """A run of Minor Feature; finished, because only a finished run is the price of one."""
    started = _start(client, {"title": title, "directive": title})
    assert started.status_code == 200, started.text
    run_id = started.json()["run"]["id"]
    with engine.begin() as connection:
        connection.execute(text("UPDATE workflow_runs SET status = :s WHERE id = :r"),
                           {"s": status, "r": run_id})
    return run_id


def test_costs_are_a_range_from_this_projects_own_runs(client, engine):
    client.post("/v1/delivery-workflows/catalogue/install",
                json={"project_id": _project(), "actor": "ceo"})
    # Five Minor Feature runs costing 1..5 dollars, one build each; one run of $3 split over
    # two builds; one run that never spent anything (left out).
    for n in range(1, 6):
        wfr = _workflow_run(client, engine, f"Feature {n}")
        _insert_run(engine, f"crun_cost{n}", fctx={"workflow_run_id": wfr})
        _call(engine, f"crun_cost{n}", n * 0.6, included=True)
        _call(engine, f"crun_cost{n}", n * 0.4, included=(n != 3))
    _workflow_run(client, engine, "Never built")

    # Quick changes: a follow-up and a feedback retry with no workflow; a plain retry and a
    # first build are not changes.
    _insert_run(engine, "crun_chg1", fctx={"is_followup": True, "parent_run_id": "crun_cost1"})
    _call(engine, "crun_chg1", 0.5, included=True)
    _insert_run(engine, "crun_chg2", fctx={"attempt": 2, "feedback_history": [
        {"fix_note": "make the button blue"}]})
    _call(engine, "crun_chg2", 1.5, included=True)
    _insert_run(engine, "crun_rerun", fctx={"attempt": 2, "feedback_history": [
        {"fix_note": ""}]})
    _call(engine, "crun_rerun", 9.0)
    _insert_run(engine, "crun_first", fctx={"attempt": 1})
    _call(engine, "crun_first", 9.0)

    costs = client.get("/v1/delivery-workflows/costs", params={"project_id": _project()})
    assert costs.status_code == 200, costs.text
    body = costs.json()
    assert body["project_id"] == _project()
    minor = body["costs"]["minor_feature"]
    assert minor["runs"] == 5
    assert minor["low_usd"] == pytest.approx(1.8)   # p20 of 1,2,3,4,5
    assert minor["high_usd"] == pytest.approx(4.2)  # p80
    assert minor["metered"] is True                 # run 3 billed an API call

    change = body["costs"]["@change"]
    assert change["runs"] == 2
    assert change["low_usd"] == pytest.approx(0.7)
    assert change["high_usd"] == pytest.approx(1.3)
    assert change["metered"] is False  # subscription-only: still an API-equivalent value

    assert set(body["costs"]) == {"minor_feature", "@change"}  # no history, no key
