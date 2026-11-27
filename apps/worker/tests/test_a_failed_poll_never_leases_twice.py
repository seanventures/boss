"""P9a-5 review: whether the worker sends `wait_ms` is decided from the client's signature,
never by catching a TypeError from the call — a TypeError raised after the API leased a job must
not re-poll and lease a second one."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.worker.boss_worker import runner


def _settings(**kw):
    return SimpleNamespace(worker_id="w", lease_ttl_seconds=60, poll_interval_seconds=15,
                           long_poll=True, long_poll_wait_ms=15000, **kw)


class NewClient:
    def __init__(self):
        self.calls = []

    def poll(self, worker_id, capabilities, lease_ttl_seconds, wait_ms=0):
        self.calls.append(wait_ms)
        raise TypeError("the leased job did not parse")       # after the lease, server side


class OldClient:
    def __init__(self):
        self.calls = 0

    def poll(self, worker_id, capabilities, lease_ttl_seconds):
        self.calls += 1
        return None


def test_a_type_error_after_the_lease_is_raised_not_re_polled(monkeypatch):
    monkeypatch.setattr(runner, "_tick_ai_sre_overwatch", lambda settings: None)
    client = NewClient()
    with pytest.raises(TypeError):
        runner.run_worker_cycle(client, _settings())
    assert client.calls == [15000], "exactly one poll, with the long-poll wait"


def test_an_older_client_gets_the_plain_poll(monkeypatch):
    monkeypatch.setattr(runner, "_tick_ai_sre_overwatch", lambda settings: None)
    client = OldClient()
    assert runner.run_worker_cycle(client, _settings()) is False
    assert client.calls == 1


def test_the_wait_never_exceeds_the_poll_interval_or_the_api_ceiling():
    assert runner.poll_wait_ms(_settings()) == 15000
    assert runner.poll_wait_ms(SimpleNamespace(long_poll=True, long_poll_wait_ms=60000,
                                               poll_interval_seconds=60)) == 25000
    assert runner.poll_wait_ms(SimpleNamespace(long_poll=False, long_poll_wait_ms=5000,
                                               poll_interval_seconds=15)) == 0


def test_extra_slot_waits_after_a_failed_cycle_then_resumes(monkeypatch):
    from apps.worker.boss_worker import main
    import httpx

    settings = _settings(control_plane_url="http://qa.invalid", api_key=None)
    settings.model_copy = lambda update: SimpleNamespace(**{**vars(settings), **update})
    client = SimpleNamespace(long_polled=True, close=lambda: None)
    monkeypatch.setattr(main, "ControlPlaneClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(main, "worker_slots", lambda *args: 2)
    monkeypatch.setattr(main, "SLOTS_RECHECK_SECONDS", 0)
    monkeypatch.setattr(main, "_SLOTS", {"total": 2})
    monkeypatch.setattr(main, "_STOPPING", False)
    calls = []
    waits = []

    def cycle(_client, _settings):
        calls.append(_settings.worker_id)
        if len(calls) == 1:
            raise httpx.ConnectError("API unavailable")
        if len(calls) == 3:
            main._STOPPING = True
            return True
        return False  # A successful long poll already waited.

    monkeypatch.setattr(main, "run_worker_cycle", cycle)
    monkeypatch.setattr(main, "idle_wait", waits.append)
    main._extra_lease_loop(settings, 1)
    assert calls == ["w-s1"] * 3
    assert waits == [15, 0]


def test_a_failed_poll_forgets_the_earlier_long_poll():
    """The flag the idle wait trusts belongs to the poll that just ran: a poll that raised
    never answered, so the loop gets its full poll-interval sleep, whoever caught the error."""
    import httpx
    from apps.worker.boss_worker import main
    from apps.worker.boss_worker.control_plane import ControlPlaneClient

    answers = iter([httpx.Response(200, json={"status": "idle", "long_poll": True}),
                    httpx.ConnectError("API unavailable")])

    def handler(request):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    client = ControlPlaneClient("http://qa.invalid")
    client._client = httpx.Client(base_url="http://qa.invalid",
                                  transport=httpx.MockTransport(handler))
    settings = _settings()
    assert client.poll("w", [], 60, wait_ms=15000) is None
    assert main.idle_seconds(client, settings) == 0.0
    with pytest.raises(httpx.ConnectError):
        client.poll("w", [], 60, wait_ms=15000)
    assert main.idle_seconds(client, settings) == 15.0
