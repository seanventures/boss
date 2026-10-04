"""The old setup routes stay for one release as wrappers, and say so (`Deprecation`)."""
from apps.api.tests.setup_helpers import AUTH, PROJECT, as_ceo, fakes


def test_status_is_the_view_with_steps(client, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    response = client.get("/v1/setup/status", params={"project_id": PROJECT}, headers=AUTH)
    assert response.status_code == 200
    assert response.headers["Deprecation"] == "true"
    body = response.json()
    assert [s["id"] for s in body["steps"]] == ["github", "model"]
    assert body["steps"][0]["href"] == "/?area=setup&focus=repository"
    assert body["verdict"]["label"] == "2 to go"


def test_the_old_key_route_now_proves_the_key(client, monkeypatch):
    as_ceo(monkeypatch)
    calls: list = []
    fakes(monkeypatch, model_calls=calls)
    response = client.post("/v1/setup/model", headers=AUTH, json={
        "project_id": PROJECT, "actor": "ceo", "provider": "openrouter",
        "api_key": "sk-stub-ok", "model": "stub/small"})
    assert response.status_code == 201, response.text
    assert response.headers["Deprecation"] == "true"
    assert response.json()["verified"] is True and len(calls) == 1
    refused = client.post("/v1/setup/model", headers=AUTH, json={
        "project_id": PROJECT, "actor": "ceo", "provider": "openrouter",
        "api_key": "sk-stub-401", "model": "stub/small"})
    assert refused.status_code == 422
    sub = client.post("/v1/setup/model", headers=AUTH, json={
        "project_id": PROJECT, "provider": "claude_code", "api_key": "x"})
    assert sub.status_code == 409 and "is a subscription, not an API key" in sub.text


def test_the_other_reads_still_answer(client, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    for path in ("/v1/setup/model-providers", "/v1/setup/coding-agents",
                 "/v1/setup/decision-model"):
        response = client.get(path, params={"project_id": PROJECT}, headers=AUTH)
        assert response.status_code == 200, (path, response.text)
        assert response.headers["Deprecation"] == "true"
