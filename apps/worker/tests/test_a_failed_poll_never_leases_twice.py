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
