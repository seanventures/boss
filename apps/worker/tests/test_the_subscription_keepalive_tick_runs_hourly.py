"""P0: the worker ticks the control-plane subscription keep-alive sweep on an hourly gate, not
on every main-loop pass — mirroring `_tick_setup_checks`'s existing `time.monotonic()` gate, so
a dying Codex login is caught within the hour without hammering the control plane every tick.

Pure call-site check (same shape as test_the_wake_tick_covers_every_live_project.py): no
database, no model call, no real sleeping — `time.monotonic` is monkeypatched.
"""
from __future__ import annotations

from types import SimpleNamespace

import apps.worker.boss_worker.main as worker_main


class _RecordingControlPlaneClient:
    def __init__(self):
        self.calls = []

    def tick_subscription_keepalive(self, project_id):
        self.calls.append(project_id)
        return {"checked": 1}


def test_the_tick_fires_once_per_hour_not_every_pass(monkeypatch):
    client = _RecordingControlPlaneClient()
    settings = SimpleNamespace(default_project_id="boss-dogfood")
    worker_main._last_subscription_keepalive_monotonic = 0.0
    clock = {"t": 1_000.0}
    monkeypatch.setattr(worker_main.time, "monotonic", lambda: clock["t"])

    worker_main._tick_subscription_keepalive(client, settings)
    worker_main._tick_subscription_keepalive(client, settings)  # same instant: must not re-fire

    assert client.calls == ["boss-dogfood"]

    clock["t"] += worker_main.SUBSCRIPTION_KEEPALIVE_INTERVAL_SECONDS + 1
    worker_main._tick_subscription_keepalive(client, settings)

    assert client.calls == ["boss-dogfood", "boss-dogfood"], (
        "once the hourly interval has elapsed, the tick must fire again")
