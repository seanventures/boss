"""Process Manager steward HTTP — tick, metrics, proposals, honest 503."""
from __future__ import annotations


class _FakeProcessManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def get(self, connection, project_id: str):
        return {
            "project_id": project_id,
            "steward": {"id": "pm_1", "status": "active"},
            "last_tick": {
                "at": "2026-08-14T12:00:00+00:00",
                "status": "ok",
                "operate": "observed",
                "tested": False,
                "validated": False,
            },
            "metrics": {
                "cycle_time_seconds": None,
                "revise_rate": None,
                "qa_verdicts": 0,
                "ux_verdicts": 0,
                "panel_verdicts": 0,
                "accept_rate": None,
            },
            "proposals": [],
        }

    def ensure(self, connection, project_id: str, actor: str = "executive_console"):
        self.calls.append(("ensure", project_id, actor))
        return {"project_id": project_id, "steward": {"id": "pm_1", "status": "active"}}

    def tick(self, connection, project_id: str, actor: str = "executive_console"):
        self.calls.append(("tick", project_id, actor))
        return {
            "project_id": project_id,
            "last_tick": {
                "at": "2026-08-14T12:01:00+00:00",
                "status": "ok",
                "operate": "observed",
                "tested": False,
                "validated": False,
            },
            "metrics": {
                "cycle_time_seconds": None,
                "revise_rate": None,
                "qa_verdicts": 0,
                "ux_verdicts": 0,
                "panel_verdicts": 0,
                "accept_rate": None,
            },
            "proposals": [],
        }

    def metrics(self, connection, project_id: str):
        return {
            "project_id": project_id,
            "metrics": {"qa_verdicts": 0, "ux_verdicts": 0, "panel_verdicts": 0},
            "last_tick": {"at": "2026-08-14T12:00:00+00:00", "status": "ok"},
        }

    def proposals(self, connection, project_id: str):
        return {"project_id": project_id, "proposals": []}


def test_tick_delegates_to_process_manager_service(client, monkeypatch):
    fake = _FakeProcessManager()
    monkeypatch.setattr(
        "apps.api.app.routers.process_manager._service",
        lambda: fake,
    )
    ticked = client.post(
        "/v1/process-manager/tick",
        json={"project_id": "boss-dogfood", "actor": "ceo"},
    )
    assert ticked.status_code == 200, ticked.text
    body = ticked.json()
    assert body["project_id"] == "boss-dogfood"
    assert body["last_tick"]["status"] == "ok"
    assert "5/5" not in ticked.text
    assert fake.calls == [("tick", "boss-dogfood", "ceo")]


def test_tick_is_honest_503_when_runtime_missing(client, monkeypatch):
    monkeypatch.setattr(
        "apps.api.app.routers.process_manager._load_service_class",
        lambda: None,
    )
    missing = client.post(
        "/v1/process-manager/tick",
        json={"project_id": "boss-dogfood", "actor": "ceo"},
    )
    assert missing.status_code == 503, missing.text
    detail = missing.json().get("detail", "")
    assert "unavailable" in str(detail).lower()
    assert "5/5" not in missing.text


def test_tick_requires_project_id(client, monkeypatch):
    monkeypatch.setattr(
        "apps.api.app.routers.process_manager._service",
        lambda: _FakeProcessManager(),
    )
    missing = client.post("/v1/process-manager/tick", json={"actor": "ceo"})
    assert missing.status_code == 422


def test_get_metrics_and_proposals_are_passthrough(client, monkeypatch):
    monkeypatch.setattr(
        "apps.api.app.routers.process_manager._service",
        lambda: _FakeProcessManager(),
    )
    snapshot = client.get("/v1/process-manager", params={"project_id": "boss-dogfood"})
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["steward"]["status"] == "active"

    metrics = client.get(
        "/v1/process-manager/metrics", params={"project_id": "boss-dogfood"}
    )
    assert metrics.status_code == 200, metrics.text
    assert "5/5" not in metrics.text
    assert metrics.json()["metrics"]["qa_verdicts"] == 0

    proposals = client.get(
        "/v1/process-manager/proposals", params={"project_id": "boss-dogfood"}
    )
    assert proposals.status_code == 200, proposals.text
    assert proposals.json()["proposals"] == []


def test_ensure_requires_agent_chat(client, monkeypatch):
    fake = _FakeProcessManager()
    monkeypatch.setattr(
        "apps.api.app.routers.process_manager._service",
        lambda: fake,
    )
    created = client.post(
        "/v1/process-manager/ensure",
        json={"project_id": "boss-dogfood", "actor": "ceo"},
    )
    assert created.status_code == 200, created.text
    assert fake.calls == [("ensure", "boss-dogfood", "ceo")]
    assert created.json()["steward"]["id"] == "pm_1"


def test_load_service_class_is_honest_without_runtime():
    from apps.api.app.routers import process_manager as router_mod

    # Sibling pm-runtime may not be merged yet. Import must not crash.
    cls = router_mod._load_service_class()
    assert cls is None or getattr(cls, "__name__", "") == "ProcessManagerService"
    assert router_mod.router is not None
