"""New work builds onto the app its door named; a long brief is whole; a change keeps its profile.

AR-20: "+ Add a feature" on a build named the app (`app=crun_…`), and New work ignored it — the
start went to the project's default branch ("· onto main") as a fresh build. POST …/work now
takes `app` (a build's run id or the app's key), resolves it to the app's lineage and hands the
start door `extends_run_id`, the same continuation Change it / extend_code_task use (the app's
branch, its open PR); GET …/work/plan?app= says which app, branch and change that is. An app
that is not this project's is refused — never a quiet build on main.

AR-21: the brief's limit is the directive's (MAX_DIRECTIVE). A 2,080-character brief reaches the
run whole; past the limit the API refuses with a 422 instead of cutting.

AR-22: Change it's change ran "balanced" on a lean app. It now runs on the lineage's profile
unless the person picks another (lean / balanced / strict).
"""
from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.app.config import get_settings
from boss_engine.services.project_experience.add_project import MAX_DIRECTIVE

ROOT = "crun_ngroot01"
RUN = "crun_ng9a9a535c"
CHANGE_URL = "http://boss-local/{p}/northgate-gateway/changes/4"


def _project() -> str:
    return get_settings().default_project_id


def _insert(engine, run_id, *, fctx, spec=None, pr_url=None, pr_number=None,
            title="Northgate Gateway"):
    spec = {"instructions": "Build the gateway.", "acceptance_criteria": ["it routes"],
            "owner": "acme", "repo": "northgate-gateway", "base_branch": "main", **(spec or {})}
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO code_task_runs (id,project_id,status,phase,title,pull_request_url,"
            "pull_request_number,spec,state,finalize_ctx) VALUES (:id,:p,'completed','done',:t,"
            ":pr,:n,CAST(:spec AS JSONB),'{}'::jsonb,CAST(:fctx AS JSONB))"
        ), {"id": run_id, "p": _project(), "t": title, "pr": pr_url, "n": pr_number,
            "spec": json.dumps(spec), "fctx": json.dumps(fctx)})


def _the_app(engine, *, profile="lean") -> None:
    """A lean app: its first build opened change #4, and a later build on the same branch."""
    url = CHANGE_URL.format(p=_project())
    _insert(engine, ROOT, fctx={"attempt": 1, "app_id": "app_northgate"},
            spec={"profile": profile}, pr_url=url, pr_number=4)
    _insert(engine, RUN, fctx={"attempt": 2, "root_run_id": ROOT, "parent_run_id": ROOT,
                               "app_id": "app_northgate"},
            spec={"profile": profile, "base_branch": f"boss/code/{ROOT}"}, pr_url=url,
            pr_number=4)


def _start(client, body):
    """POST …/work, accepting whatever readiness this test project lacks (a model, a host):
    what is tested is where the work goes, not whether the project is set up."""
    installed = client.post("/v1/delivery-workflows/catalogue/install",
                            json={"project_id": _project(), "actor": "ceo"})
    assert installed.status_code == 200, installed.text
    path = f"/v1/projects/{_project()}/work"
    r = client.post(path, json=body)
    if r.status_code == 409 and isinstance(r.json().get("detail"), dict):
        detail = r.json()["detail"]
        ready = detail.get("readiness") if isinstance(detail.get("readiness"), dict) else detail
        missing = [m["key"] for m in (ready.get("missing") or [])]
        if missing:
            r = client.post(path, json={**body, "start_anyway": missing})
    return r


def _workflow_input(engine, run_id):
    with engine.begin() as connection:
        return connection.execute(text("SELECT input FROM workflow_runs WHERE id = :r"),
                                  {"r": run_id}).scalar()


# ── AR-20 ──────────────────────────────────────────────────────────────────────────────────────

def test_the_plan_says_which_app_branch_and_change_new_work_builds_onto(client, engine):
    _the_app(engine)
    plan = client.get(f"/v1/projects/{_project()}/work/plan",
                      params={"kind": "feature", "detail": "true", "app": RUN})
    assert plan.status_code == 200, plan.text
    assert plan.json()["anchor"] == {
        "run_id": RUN, "app": "Northgate Gateway", "branch": f"boss/code/{ROOT}",
        "pr_number": 4, "pr_url": CHANGE_URL.format(p=_project())}

    # By the app's key too (the Command Center's door names the app, not a build).
    from boss_engine.services.agent_assignment import app_key

    by_key = client.get(f"/v1/projects/{_project()}/work/plan",
                        params={"kind": "feature", "app": app_key("Northgate Gateway")})
    assert by_key.status_code == 200, by_key.text
    assert by_key.json()["anchor"]["branch"] == f"boss/code/{ROOT}"

    # No door, no anchor; an app that is not here says so instead of planning onto main.
    assert "anchor" not in client.get(f"/v1/projects/{_project()}/work/plan").json()
    nowhere = client.get(f"/v1/projects/{_project()}/work/plan", params={"app": "crun_elsewhere"})
    assert nowhere.status_code == 200
    assert nowhere.json()["anchor"] is None
    assert "not in this project" in nowhere.json()["anchor_error"]


def test_new_work_named_for_an_app_continues_that_apps_lineage(client, engine):
    _the_app(engine)
    started = _start(client, {"kind": "feature", "app": RUN,
                              "text": "Add a /healthz route that reports the upstream pool."})
    assert started.status_code == 200, started.text
    out = started.json()
    # The start door's own continuation: that app, its branch.
    assert out["extends"]["run_id"] == RUN
    assert out["extends"]["branch"] == f"boss/code/{ROOT}"
    assert _workflow_input(engine, out["run_id"])["extends_run_id"] == RUN


def test_new_work_named_for_an_app_that_is_not_here_starts_nothing(client, engine):
    _the_app(engine)
    with engine.begin() as connection:
        before = connection.execute(text("SELECT count(*) FROM workflow_runs")).scalar()
    refused = client.post(f"/v1/projects/{_project()}/work",
                          json={"kind": "feature", "app": "crun_elsewhere", "text": "Add a route."})
    assert refused.status_code == 404, refused.text
    assert refused.json()["detail"]["error"] == "unknown_app"
    with engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM workflow_runs")).scalar() == before


# ── AR-21 ──────────────────────────────────────────────────────────────────────────────────────

def test_a_brief_past_2000_characters_reaches_the_run_whole(client, engine):
    items = "\n".join(f"- item {i:02d}: the gateway keeps its last good config {i}"
                      for i in range(1, 41))
    words = (items + " more" * 200)[:2060] + "\n- LAST ITEM kept"
    assert len(words) > 2000
    started = _start(client, {"kind": "feature", "text": words})
    assert started.status_code == 200, started.text
    directive = _workflow_input(engine, started.json()["run_id"])["directive"]
    assert directive == words


def test_a_brief_past_the_limit_is_refused_never_cut(client):
    over = client.post(f"/v1/projects/{_project()}/work",
                       json={"kind": "feature", "text": "x" * (MAX_DIRECTIVE + 1)})
    assert over.status_code == 422, over.text
    assert "text" in json.dumps(over.json())


# ── AR-22 ──────────────────────────────────────────────────────────────────────────────────────

def _propose(client, **extra):
    r = client.post(f"/v1/code-tasks/{RUN}/feedback", json={
        "message": "Add a /healthz route that reports the upstream pool.",
        "expected": "GET /healthz answers 200 with the pool size.", **extra})
    return r


def test_a_change_runs_on_the_lineages_profile_by_default(client, engine):
    _the_app(engine, profile="lean")
    proposed = _propose(client)
    assert proposed.status_code == 200, proposed.text
    assert proposed.json()["proposal"]["profile"] == "lean"
    confirmed = client.post(f"/v1/feedback-threads/{proposed.json()['thread_id']}/confirm",
                            json={"actor": "ceo"})
    assert confirmed.status_code == 200, confirmed.text
    with engine.begin() as connection:
        spec = connection.execute(text("SELECT spec FROM code_task_runs WHERE id = :i"),
                                  {"i": confirmed.json()["run_id"]}).scalar()
    assert spec["profile"] == "lean"


def test_a_change_runs_on_the_profile_the_person_picks(client, engine):
    _the_app(engine, profile="lean")
    proposed = _propose(client, profile="strict")
    assert proposed.status_code == 200, proposed.text
    assert proposed.json()["proposal"]["profile"] == "strict"


def test_a_profile_that_is_not_one_is_refused(client, engine):
    _the_app(engine)
    refused = _propose(client, profile="turbo")
    assert refused.status_code == 422, refused.text
    assert "lean, balanced, strict" in refused.text


def test_a_lineage_with_no_profile_keeps_the_size_rule():
    from boss_engine.services.feedback_agent import change_profile

    assert change_profile({}, "scoped_tweak") == "balanced"
    assert change_profile({}, "full_rerun") == "strict"
    assert change_profile({"profile": "lean"}, "full_rerun") == "lean"
    assert change_profile({"profile": "lean"}, "scoped_tweak", "balanced") == "balanced"
