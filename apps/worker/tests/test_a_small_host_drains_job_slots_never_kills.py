"""Job slots on a 4-core / 8 GiB host (R3, BOSSAPP-1431; RCA gap 6).

The worker took its job slots from "Builds at once" and fixed them at start, so the partner's
small laptop ran ONE slot for everything: a review or an approval waited behind a two-hour
build. And nothing ever tested what happens when capacity is lowered while slots are busy.
Lowering capacity must drain: the job a slot holds runs to the end; the slot only stops
taking new work.
"""
from __future__ import annotations

import json
import threading

import pytest

import apps.worker.boss_worker.main as worker_main
from boss_engine.services import admission
from engine.tests._small_host import SMALL_HOST


class _Response:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        if isinstance(self._body, Exception):
            raise self._body

    def json(self):
        return self._body


class _HealthClient:
    """The control plane's /v1/system/health, answering with the small host's admission."""

    def __init__(self, admission_body):
        body = admission_body
        self._client = type("H", (), {"get": lambda _s, *_a, **_k: _Response(body)})()


class _Settings:
    def __init__(self, slots="auto"):
        self.worker_slots = slots
        self.worker_id = "w_small"
        self.control_plane_url = "http://control-plane.invalid"
        self.api_key = "k"

    def model_copy(self, update):
        copy = _Settings(self.worker_slots)
        for k, v in update.items():
            setattr(copy, k, v)
        return copy


def _small_host_admission(monkeypatch, saved_slots=None):
    """What the API answers on the small host (admission.snapshot on its numbers)."""
    state = {"agent_capacity": {"worker_slots": saved_slots}} if saved_slots else {}
    monkeypatch.setattr(admission, "_state", lambda _c, key: state.get(key, {}))
    monkeypatch.setattr(admission, "running_previews_mb", lambda *_: 0.0)
    monkeypatch.setattr(admission, "heavy_in_flight", lambda *_: 1)
    monkeypatch.setattr(admission, "current_level", lambda *_a, **_k: admission.L0)

    class Conn:
        def execute(self, *_a, **_k):
            return type("R", (), {"scalar": lambda self: 0})()

    return json.loads(json.dumps({"admission": admission.snapshot(Conn(), cap=SMALL_HOST)}))


def test_the_small_host_gets_its_saved_job_slots_not_its_build_limit(monkeypatch):
    body = _small_host_admission(monkeypatch, saved_slots=3)
    assert body["admission"]["builds_at_once"]["value"] == 2
    assert worker_main.worker_slots(_HealthClient(body), _Settings()) == 3
    # Until the operator saves slots, the old rule: one per build at once, so an upgrade
    # never raises concurrency by itself.
    body = _small_host_admission(monkeypatch)
    assert worker_main.worker_slots(_HealthClient(body), _Settings()) == 2


def test_job_slots_are_bounded_and_an_unreadable_api_means_one(monkeypatch):
    assert worker_main.worker_slots(_HealthClient({"admission": {"worker_slots": 9}}),
                                    _Settings()) == worker_main.MAX_WORKER_SLOTS
    # An older API without worker_slots: its build limit.
    assert worker_main.worker_slots(
        _HealthClient({"admission": {"builds_at_once": {"value": 2}}}), _Settings()) == 2
    assert worker_main.worker_slots(_HealthClient(RuntimeError("503")), _Settings()) == 1
    # A fixed BOSS_WORKER_SLOTS never asks the API.
    assert worker_main.worker_slots(_HealthClient(RuntimeError("unused")), _Settings("2")) == 2
    assert worker_main.worker_slots(_HealthClient(RuntimeError("unused")), _Settings("0")) == 1


def test_lowering_job_slots_drains_the_slot_and_never_cuts_its_job(monkeypatch):
    """Slot 1 is running a long job when the operator lowers the job slots to 1 (or the API now
    measures less room). The job finishes and reports; slot 1 then takes no new work."""
    slots = iter([2, 1, 1, 1, 1, 1])
    monkeypatch.setattr(worker_main, "worker_slots", lambda *_a: next(slots, 1))
    monkeypatch.setattr(worker_main, "SLOTS_RECHECK_SECONDS", 0.0)
    monkeypatch.setattr(worker_main, "_STOPPING", False)

    class _Client:
        def __init__(self, *_a, **_k):
            pass

        def close(self):
            pass

    monkeypatch.setattr(worker_main, "ControlPlaneClient", _Client)
    job_started, lowered, job_finished = threading.Event(), threading.Event(), threading.Event()
    cycles: list[str] = []

    def cycle(_client, slot_settings):
        cycles.append(slot_settings.worker_id)
        job_started.set()
        assert lowered.wait(5), "capacity was never lowered"
        job_finished.set()          # reached the end: the job was not interrupted
        return True

    drained_sleeps = []

    def sleep(seconds):
        drained_sleeps.append(seconds)
        if len(drained_sleeps) >= 3:
            worker_main._STOPPING = True

    monkeypatch.setattr(worker_main, "run_worker_cycle", cycle)
    monkeypatch.setattr(worker_main.time, "sleep", sleep)
    monkeypatch.setattr(worker_main, "idle_wait", lambda *_a, **_k: None)

    t = threading.Thread(target=worker_main._extra_lease_loop, args=(_Settings(), 1), daemon=True)
    t.start()
    assert job_started.wait(5)
    lowered.set()                   # slots lowered to 1 while slot 1 holds a job
    t.join(5)
    assert not t.is_alive()
    assert job_finished.is_set()
    assert cycles == ["w_small-s1"], "a drained slot must not lease again"
    assert len(drained_sleeps) >= 3 and worker_main._SLOTS["total"] == 1


@pytest.fixture(autouse=True)
def _restore_slots():
    before = dict(worker_main._SLOTS)
    yield
    worker_main._SLOTS.clear()
    worker_main._SLOTS.update(before)
