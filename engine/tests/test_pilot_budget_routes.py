"""BOSSAPP-1295 / 1292 routes on a bare app: the pilot preset, teams, the month forecast, the pilot
metrics report, its CSV/JSON export and the baseline."""
from __future__ import annotations

import csv
import io
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL or "test" not in URL.lower(),
                                reason="needs BOSS_TEST_DATABASE_URL naming a disposable database")


@pytest.fixture(scope="module")
def eng():
    from boss_engine.db import ensure_schema

    engine = create_engine(URL, future=True)
    ensure_schema(engine)
    yield engine
    engine.dispose()


def _apply_scope_check(eng) -> None:
    """This tree's scope check, as ensure_schema runs it: the shared test server also serves
    older trees whose boots put back a check without `team`."""
    from boss_engine.config import get_settings
    from boss_engine.db import split_statements

    sql = get_settings().schema_path.read_text(encoding="utf-8")
    block = next(s for s in split_statements(sql) if "scoped_budget_policies_scope_type_check" in s)
    raw = eng.raw_connection()
    try:
        with raw.cursor() as cursor:
            cursor.execute("SET lock_timeout = '10s'")
            cursor.execute(block)
        raw.commit()
    finally:
        raw.close()


@pytest.fixture()
def world(eng):
    from boss_engine.services import scoped_budgets

    _apply_scope_check(eng)
    scoped_budgets.reset_for_tests()
    sfx = uuid4().hex[:8]
    w = {"org": f"org_pr_{sfx}", "ws": f"ws_pr_{sfx}", "p": f"p_pr_{sfx}", "team": f"grp_pr_{sfx}"}
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'o')"), {"o": w["org"]})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'w')"),
                  {"w": w["ws"], "o": w["org"]})
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, 'Gateway', :p)"),
                  {"p": w["p"], "w": w["ws"]})
        c.execute(text("INSERT INTO identity_groups (id, org_id, key, name, source) VALUES "
                       "(:g, :o, 'preview', 'Preview team', 'manual')"), {"g": w["team"], "o": w["org"]})
        c.execute(text("INSERT INTO identity_role_bindings (id, org_id, subject_type, subject_id, role_id, "
                       "scope_type, scope_id, granted_by) VALUES (:i, :o, 'group', :g, 'arl_operator', "
                       "'project', :p, 'test')"),
                  {"i": "bnd_" + uuid4().hex[:16], "o": w["org"], "g": w["team"], "p": w["p"]})
        c.execute(text("INSERT INTO llm_calls (id, project_id, code_task_run_id, cost_usd_micros) "
                       "VALUES (:i, :p, 'ctr_route', 12000000)"), {"i": f"llmc_{sfx}", "p": w["p"]})
    yield w
    with eng.begin() as c:   # a team row left behind would fail an older schema's check rewrite
        c.execute(text("DELETE FROM scoped_budget_policies WHERE org_id = :o"), {"o": w["org"]})
    scoped_budgets.reset_for_tests()


@pytest.fixture()
def client(eng):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.app.routes_pilot_metrics import build_pilot_metrics_router
    from apps.api.app.routes_scoped_budgets import build_scoped_budgets_router

    app = FastAPI()
    app.include_router(build_scoped_budgets_router(engine_getter=lambda: eng))
    app.include_router(build_pilot_metrics_router(engine_getter=lambda: eng))
    return TestClient(app)


def test_pilot_preset_makes_a_team_budget_and_the_forecast_reads_it(client, world):
    teams = client.get("/v1/budget-policies/scoped/teams", params={"project_id": world["p"]}).json()
    assert [t["name"] for t in teams["teams"]] == ["Preview team"]
    assert teams["teams"][0]["projects"] == [world["p"]]
    assert any(p["key"] == "pilot_2000" for p in teams["presets"])
    made = client.post("/v1/budget-policies/scoped", json={
        "project_id": world["p"], "preset": "pilot_2000", "scope_id": world["team"]})
    assert made.status_code == 201, made.text
    body = made.json()
    assert (body["scope_type"], body["cap_usd_micros"], body["period"], body["hard_stop"]) == \
        ("team", 2_000_000_000, "month", True)
    got = client.get("/v1/budget-policies/scoped/forecast", params={
        "project_id": world["p"], "scope_type": "team", "scope_id": world["team"]}).json()
    assert got["spent_usd_micros"] == 12_000_000 and got["cap_usd_micros"] == 2_000_000_000
    assert got["runs"][0]["run_id"] == "ctr_route"
    bad = client.get("/v1/budget-policies/scoped/forecast", params={
        "project_id": world["p"], "scope_type": "team", "scope_id": "grp_other"})
    assert bad.status_code == 422


def test_pilot_metrics_report_export_and_baseline(client, world):
    q = {"project_id": world["p"], "days": 30}
    saved = client.post("/v1/pilot-metrics/baseline", json={
        "project_id": world["p"], "values": {"review_hours_median": 26}, "note": "pre-preview"})
    assert saved.status_code == 200 and saved.json()["values"] == {"review_hours_median": 26.0}
    assert client.post("/v1/pilot-metrics/baseline", json={"project_id": world["p"],
                                                             "values": {"nope": 1}}).status_code == 422
    report = client.get("/v1/pilot-metrics", params=q).json()
    assert report["baseline"]["values"]["review_hours_median"] == 26.0
    assert set(report) >= {"review", "regressions", "post_deploy", "after"}
    as_csv = client.get("/v1/pilot-metrics/export", params={**q, "format": "csv"})
    assert as_csv.status_code == 200 and as_csv.headers["content-type"].startswith("text/csv")
    assert "attachment" in as_csv.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(as_csv.text)))
    assert rows[0]["section"] == "summary" and rows[0]["before"] == "26.0"
    as_json = client.get("/v1/pilot-metrics/export", params={**q, "format": "json"})
    assert as_json.json()["days"] == 30
    assert client.get("/v1/pilot-metrics", params={**q, "days": 11}).status_code == 422
