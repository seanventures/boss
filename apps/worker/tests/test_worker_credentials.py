"""Community worker calls authenticate and act as the seeded system member."""

from __future__ import annotations

from apps.worker.boss_worker.config import WorkerSettings
from apps.worker.boss_worker.control_plane import ControlPlaneClient


def test_worker_key_wins_when_owner_key_is_also_in_env(monkeypatch):
    monkeypatch.setenv("BOSS_API_KEY", "owner-key")
    monkeypatch.setenv("BOSS_WORKER_API_KEY", "worker-key")
    assert WorkerSettings(_env_file=None).api_key == "worker-key"


def test_periodic_worker_calls_claim_the_system_actor():
    calls: list[tuple[str, dict]] = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {}

    class RecordingClient:
        def post(self, path, json):
            calls.append((path, json))
            return Response()

        def close(self):
            pass

    client = ControlPlaneClient("http://api:8000", api_key="worker-key")
    client._client.close()
    client._client = RecordingClient()
    client.tick_metatron_watch("boss-dogfood")
    client.tick_focused_agents("boss-dogfood")
    client.tick_agent_processes("boss-dogfood")
    client.tick_process_manager("boss-dogfood")
    client.tick_factory_flow("boss-dogfood")
    assert len(calls) == 5
    assert all(body["actor"] == "system" for _, body in calls)
    client.close()
