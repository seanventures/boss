"""P8a units that need no database: replica choice, the work inbox's protocol, the certificate
terminator allowlist, and the async waiters."""
from __future__ import annotations

import asyncio
import threading

from boss_engine.runtime.inbox import Inbox
from boss_engine.services import fleet_ca, run_plane


def test_the_replica_with_the_most_free_slots_wins_and_ties_take_turns():
    replicas = {"a": {"deployment_id": "d", "free": 1}, "b": {"deployment_id": "d", "free": 3},
                "c": {"deployment_id": "d", "free": 3}, "x": {"deployment_id": "e", "free": 9}}
    run_plane._ROUND_ROBIN.pop("d", None)
    picks = [run_plane._pick(replicas, "d") for _ in range(4)]
    assert picks == ["b", "c", "b", "c"]
    assert run_plane._pick({"a": {"deployment_id": "d", "free": 0}}, "d") is None


def test_the_inbox_accepts_only_with_a_free_slot_and_answers_its_slots():
    started = threading.Event()
    release = threading.Event()

    class Handle:
        def package(self):
            started.set()
            release.wait(5)
            raise RuntimeError("no run plane in this unit test")

    inbox = Inbox(lambda work: Handle(), total_slots=1)
    work = {"op": "run", "run_id": "r1", "job_id": "j1", "replica_id": "rep", "epoch": 1}
    assert inbox.handle({"op": "ping"}) == {"ok": True, "busy_slots": 0, "total_slots": 1}
    assert inbox.handle(work) == {"ok": True}
    assert started.wait(5)
    assert inbox.handle(work) == {"ok": False, "reason": "duplicate"}
    assert inbox.handle({**work, "run_id": "r2"}) == {"ok": False, "reason": "no_free_slot"}
    assert inbox.handle({"op": "run"}) == {"ok": False, "reason": "bad_request"}
    release.set()
    inbox.join(5)
    inbox.drain.set()
    assert inbox.handle({**work, "run_id": "r3"}) == {"ok": False, "reason": "draining"}
    assert inbox.handle({"op": "slots"})["total_slots"] == 0


def test_the_forwarded_certificate_is_believed_only_from_a_named_terminator(monkeypatch):
    monkeypatch.delenv(fleet_ca.TERMINATORS_ENV, raising=False)
    assert not fleet_ca.is_cert_terminator("10.0.0.5"), "none by default"
    monkeypatch.setenv(fleet_ca.TERMINATORS_ENV, "10.0.0.0/24, 192.0.2.7")
    assert fleet_ca.is_cert_terminator("10.0.0.5")
    assert fleet_ca.is_cert_terminator("192.0.2.7")
    assert not fleet_ca.is_cert_terminator("10.0.1.5")
    assert not fleet_ca.is_cert_terminator(None)


def test_a_waiter_is_woken_from_another_thread_and_times_out_otherwise():
    async def scenario():
        waiter = run_plane.Waiter({"deployment:d"})
        threading.Timer(0.05, waiter.set).start()
        assert await waiter.wait(2.0) is True
        other = run_plane.Waiter({"deployment:e"})
        assert await other.wait(0.05) is False

    asyncio.run(scenario())
