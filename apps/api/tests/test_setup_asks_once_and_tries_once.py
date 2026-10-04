"""Two Repo and models journeys at the API: the database fallback is asked once, before
anything is stored, and Try it turns one sentence into a governed build on the default repo."""
from sqlalchemy import text

from apps.api.tests.setup_helpers import (
    AUTH, PROJECT, as_ceo, connect_key, connect_repository, fakes, view,
)


def test_the_database_fallback_is_asked_before_submit_and_only_once(client, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    monkeypatch.delenv("BOSS_COMMUNITY_SECRET_STORE", raising=False)   # nobody chose a store
    assert view(client)["secrets"] == {"backend": "database", "needs_consent": True}
    refused = connect_key(client)
    assert refused.status_code == 409
    assert refused.json()["detail"]["resend_with"] == {"accept_database_fallback": True}
    assert view(client)["models"] == []
    agreed = connect_key(client, accept_database_fallback=True)
    assert agreed.status_code == 200, agreed.text
    assert agreed.json()["secrets"]["needs_consent"] is False
    # Remembered: the next credential form does not ask.
    again = connect_key(client, key="sk-stub-ok", provider="gemini", model="stub/small")
    assert again.status_code == 200, again.text


def test_try_it_starts_one_run_on_the_default_repository(client, engine, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    before = client.post("/v1/setup/try", headers=AUTH,
                         json={"project_id": PROJECT, "directive": "Fix the README typo"})
    assert before.status_code == 409                      # not ready: nothing to build on
    connect_repository(client)
    assert connect_key(client).status_code == 200
    started = client.post("/v1/setup/try", headers=AUTH, json={
        "project_id": PROJECT, "directive": "Fix the typo in README",
        "idempotency_key": "try-1"})
    assert started.status_code == 200, started.text
    last = started.json()["try"]["last_run"]
    assert last and last["pill"]["label"] in ("Planning…", "Building…")
    again = client.post("/v1/setup/try", headers=AUTH, json={
        "project_id": PROJECT, "directive": "Fix the typo in README",
        "idempotency_key": "try-1"})
    assert again.json()["try"]["last_run"]["run_id"] == last["run_id"]
    with engine.begin() as c:
        run = c.execute(text("SELECT r.input, d.namespace FROM workflow_runs r "
                             "JOIN workflow_versions v ON v.id = r.workflow_version_id "
                             "JOIN workflow_definitions d ON d.id = v.workflow_definition_id "
                             "WHERE r.id = :r"), {"r": last["run_id"]}).mappings().one()
        runs = c.execute(text("SELECT count(*) FROM workflow_runs WHERE project_id = :p"),
                         {"p": PROJECT}).scalar()
    assert run["input"]["repository"] == "e2e/app"
    assert run["input"]["directive"] == "Fix the typo in README"
    # Product namespace: the capability runner advances product runs only (a system-namespace
    # Try it sat "ready" forever and never opened a PR) — and hidden from the Library.
    assert run["namespace"] == "product"
    assert runs == 1
    library = client.get("/v1/delivery-workflows", params={"project_id": PROJECT},
                         headers=AUTH).json()["items"]
    assert "setup_first_pr" not in {item["workflow_key"] for item in library}

    from boss_engine.context import EngineContext
    from boss_engine.services import workflow_capability_runner

    with engine.begin() as c:
        workflow_capability_runner.tick(c, EngineContext.create(), project_id=PROJECT)
    with engine.begin() as c:
        build = c.execute(text("SELECT status, output FROM workflow_step_runs "
                               "WHERE workflow_run_id = :r AND step_id = 'build'"),
                          {"r": last["run_id"]}).mappings().one()
    assert build["status"] != "ready", "the capability runner never picked up the Try it build"
