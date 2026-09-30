"""The console coordination snapshot is an ordinary project-scoped API read."""

from uuid import uuid4

from apps.api.app import main as api_main


def test_coordination_snapshot_is_typed_and_project_scoped(client, monkeypatch):
    suffix = uuid4().hex[:10]
    allowed_key = f"coord-reader-{suffix}"
    denied_key = f"coord-outsider-{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", f"{allowed_key},{denied_key}")
    monkeypatch.setattr(
        api_main.settings,
        "api_key_principals",
        f"{allowed_key}:ceo,{denied_key}:coord_outsider_{suffix}",
    )

    path = "/v1/coordination?project_id=boss-dogfood"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": f"Bearer {denied_key}"}).status_code == 403
    assert client.get("/v1/coordination", headers={"Authorization": f"Bearer {allowed_key}"}).status_code == 422
    assert client.get(
        "/v1/coordination?project_id=does-not-exist",
        headers={"Authorization": f"Bearer {allowed_key}"},
    ).status_code == 404

    response = client.get(path, headers={"Authorization": f"Bearer {allowed_key}"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"roster", "inFlight", "observedAt"}
    assert isinstance(payload["roster"], list)
    assert isinstance(payload["inFlight"], list)
    assert payload["observedAt"].endswith("Z")

    operation = client.get("/openapi.json").json()["paths"]["/v1/coordination"]["get"]
    assert any(param["name"] == "project_id" and param["required"]
               for param in operation["parameters"])
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]
