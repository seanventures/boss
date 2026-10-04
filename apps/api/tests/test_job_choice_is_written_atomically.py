"""Who does what: a job's engine and model are written together, or not at all."""
from sqlalchemy import text

from apps.api.tests.setup_helpers import AUTH, PROJECT, as_ceo, connect_key, fakes


def _knobs(engine):
    with engine.begin() as c:
        return dict(c.execute(text("SELECT key, value FROM project_settings WHERE project_id=:p "
                                   "AND key LIKE 'code_review_%'"), {"p": PROJECT}).all())


def test_one_request_writes_engine_and_model(client, engine, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    assert connect_key(client).status_code == 200
    saved = client.put("/v1/setup/jobs/review", headers=AUTH, json={
        "project_id": PROJECT, "choice": {"engine": "openrouter", "model": "stub/small"}})
    assert saved.status_code == 200, saved.text
    assert _knobs(engine) == {"code_review_engine": "openrouter",
                              "code_review_model": "stub/small"}
    review = next(j for j in saved.json()["jobs"] if j["job"] == "review")
    assert review["resolved"]["label"] == "OpenRouter · stub/small"
    assert review["metered"] is True

    auto = client.put("/v1/setup/jobs/review", headers=AUTH,
                      json={"project_id": PROJECT, "choice": None})
    assert _knobs(engine) == {}
    review = next(j for j in auto.json()["jobs"] if j["job"] == "review")
    assert review["choice"] is None and review["resolved"]["label"].startswith("Auto · ")


def test_a_choice_that_is_not_offered_writes_nothing(client, engine, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    assert connect_key(client).status_code == 200
    refused = client.put("/v1/setup/jobs/review", headers=AUTH, json={
        "project_id": PROJECT, "choice": {"engine": "codex", "model": "o3"}})
    assert refused.status_code == 422
    assert _knobs(engine) == {}


def test_removing_a_model_a_job_uses_asks_first_then_resets_the_job(client, engine, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    assert connect_key(client).status_code == 200
    client.put("/v1/setup/jobs/review", headers=AUTH, json={
        "project_id": PROJECT, "choice": {"engine": "openrouter", "model": "stub/small"}})
    asked = client.delete("/v1/setup/models/setup-openrouter", headers=AUTH,
                          params={"project_id": PROJECT})
    assert asked.status_code == 409 and asked.json()["detail"]["jobs"] == ["Review"]
    removed = client.delete("/v1/setup/models/setup-openrouter", headers=AUTH,
                            params={"project_id": PROJECT, "confirm": "true"})
    assert removed.status_code == 200
    assert removed.json()["models"] == [] and _knobs(engine) == {}


def test_the_budget_cap_is_the_one_govern_edits(client, engine, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    saved = client.put("/v1/setup/budget", headers=AUTH,
                       json={"project_id": PROJECT, "monthly_cap_usd": 150})
    assert saved.status_code == 200, saved.text
    with engine.begin() as c:
        cap = c.execute(text("SELECT monthly_cap_usd_micros FROM budget_policies "
                             "WHERE scope_type='project' AND scope_id=:p"),
                        {"p": PROJECT}).scalar()
    assert cap == 150_000_000
    assert saved.json()["budget"]["monthly_cap_usd"] == 150
