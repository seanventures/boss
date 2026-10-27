"""P0: the worker's wake tick must cover every live project, not only its own default one.

wake_26186a08c079429e9e6d (boss-platform) and wake_07440b82d6954b48a45d (northpeak-bank-b) sat
unticked because `_tick_agent_wakeups` passed the worker's resolved default project id into
`tick_agent_wakeups` instead of "" (every live project — the existing meaning `due()` already
gives an empty project filter). This is a pure call-site check: no database, no model call.
"""
from __future__ import annotations

from types import SimpleNamespace

from apps.worker.boss_worker.main import _tick_agent_wakeups


class _RecordingControlPlaneClient:
    def __init__(self):
        self.calls = []

    def tick_agent_wakeups(self, project_id=""):
        self.calls.append(project_id)
        return {"due": 0, "ran": []}


def test_the_tick_asks_for_every_live_project_not_just_the_default():
    client = _RecordingControlPlaneClient()
    settings = SimpleNamespace(default_project_id="boss-dogfood")

    _tick_agent_wakeups(client, settings)

    assert client.calls == [""], (
        f"the tick must call tick_agent_wakeups('') so due() sweeps every live project, "
        f"got {client.calls!r}"
    )
