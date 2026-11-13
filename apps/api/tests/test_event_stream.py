"""Realtime event push with per-event authorization (BOSSAPP-835).

  * a committed event is announced on `boss_events` (the trigger), a rolled-back one never is;
  * an event the viewer may not see never reaches their stream: another project's, or a kind
    their role does not read (an audit event for a viewer);
  * Last-Event-ID replays the durable events after that one, through the same filters;
  * interest filters (kinds, project) hold for live and replayed events;
  * a slow reader overflows into ONE resync marker, never an unbounded queue;
  * the route itself is declared: a person bound to one project cannot open another's stream.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.services import event_stream as es
from boss_engine.services.core import emit_event


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.viewer_a = w.person("stream-viewer-a")
    w.bind(w.viewer_a, "viewer", project=w.project_a)       # project A only
    w.owner = w.person("stream-owner")
    w.bind(w.owner, "owner")
    w.claimed()
    return w


def _emit(engine, project_id, event_type, entity_type="thing", data=None) -> str:
    with engine.begin() as c:
        return emit_event(c, project_id, event_type, "test", entity_type,
                          f"ent_{uuid.uuid4().hex[:8]}", data or {})


def _sse_events(body: str) -> list[dict]:
    out = []
    for block in body.split("\n\n"):
        lines = block.strip().splitlines()
        kind = next((ln[7:] for ln in lines if ln.startswith("event: ")), "message")
        data = next((ln[6:] for ln in lines if ln.startswith("data: ")), None)
        if data is not None:
            out.append({"kind": kind, **json.loads(data)})
    return out


async def _collect(engine, hub, viewer, interest, feed, *, last_event_id=None, seconds=0.6):
    """Run the real stream; `feed` publishes notifications once it is subscribed."""
    chunks: list[str] = []

    async def reader():
        async for chunk in es.stream(engine, hub, viewer, interest, last_event_id=last_event_id,
                                     max_seconds=seconds, heartbeat=0.1):
            chunks.append(chunk)

    task = asyncio.ensure_future(reader())
    for _ in range(50):
        if hub.count():
            break
        await asyncio.sleep(0.01)
    feed()
    await task
    return _sse_events("".join(chunks))


def _note(engine, event_id):
    ev = es.fetch_event(engine, event_id)
    return {k: ev[k] for k in ("id", "project_id", "event_type", "entity_type", "entity_id")}


# ---- units --------------------------------------------------------------------------------------

def test_interest_filters_kinds_by_exact_name_or_prefix_and_projects():
    i = es.Interest.parse("decision., code_task*, chat.decision_answered", "p1")
    assert i.matches({"project_id": "p1", "event_type": "decision.created"})
    assert i.matches({"project_id": "p1", "event_type": "code_task.started"})
    assert i.matches({"project_id": "p1", "event_type": "chat.decision_answered"})
    assert not i.matches({"project_id": "p1", "event_type": "chat.link_started"})
    assert not i.matches({"project_id": "p2", "event_type": "decision.created"})
    assert es.Interest().matches({"project_id": "x", "event_type": "anything"})
    exact = es.Interest.parse("workflow", None)
    assert exact.matches({"project_id": "x", "event_type": "workflow.run_started"})
    assert not exact.matches({"project_id": "x", "event_type": "workflows.other"})


def test_each_kind_names_the_permission_a_viewer_needs():
    assert es.permission_for("decision.created") == "decision:read"
    assert es.permission_for("identity.user_invited") == "audit:read"
    assert es.permission_for("secret.written", "secret_ref") == "secret:read_metadata"
    assert es.permission_for("chat.decision_answered") == "integration:read"
    assert es.permission_for("code_task.started") == "activity:read"


def test_the_legacy_viewer_never_answers_under_enforce(engine):
    assert es.LegacyViewer(None, False, engine).allowed("p", "activity:read") is True   # auth off
    assert es.LegacyViewer(None, True, engine).allowed("p", "activity:read") is False   # no member
    assert es.LegacyViewer("ceo", False, engine, enforced=True).allowed("p", "activity:read") \
        is False                                                                        # PDP only


def test_a_slow_reader_overflows_into_one_resync_marker():
    hub = es.Hub()
    sub = hub.subscribe(es.Interest(), loop=None, maxlen=3)
    for n in range(10):
        hub.publish({"id": f"e{n}", "project_id": "p", "event_type": "x"})
    items = []
    while (item := sub.pop()) is not None:
        items.append(item)
    assert items[0] is es.RESYNC and len(items) <= 3 and sub.overflowed >= 1
    hub.unsubscribe(sub)
    assert hub.count() == 0


# ---- NOTIFY ---------------------------------------------------------------------------------------

def test_a_committed_event_is_announced_and_a_rolled_back_one_never_is(engine, world):
    hub = es.Hub()
    got: list[dict] = []
    hub.publish = lambda note: got.append(note)          # type: ignore[assignment]
    listener = es.PgListener(es.libpq_dsn(engine), hub, poll=0.1)
    listener.start()
    try:
        assert listener.ready.wait(10)
        kept = _emit(engine, world.project_a, "stream.test_committed")
        with engine.connect() as c:
            tx = c.begin()
            emit_event(c, world.project_a, "stream.test_rolled_back", "test", "thing", "x", {})
            tx.rollback()
        deadline = time.time() + 10
        while time.time() < deadline and not any(n["id"] == kept for n in got):
            time.sleep(0.05)
        time.sleep(0.3)
    finally:
        listener.stop()
        listener.join(5)
    mine = [n for n in got if n.get("project_id") == world.project_a]
    assert [n["id"] for n in mine] == [kept]
    assert mine[0]["event_type"] == "stream.test_committed"


# ---- per-event authorization -------------------------------------------------------------------

def test_an_event_the_viewer_may_not_see_never_reaches_their_stream(engine, world):
    from boss_engine.identity.principal import AuthContext, AuthMethod

    visible = _emit(engine, world.project_a, "code_task.started")
    decision = _emit(engine, world.project_a, "decision.created", "decision")
    other_project = _emit(engine, world.project_b, "code_task.started")
    audit_kind = _emit(engine, world.project_a, "audit.exported")
    hub = es.Hub()
    viewer = es.IdentityViewer(AuthContext(world.viewer_a, AuthMethod.SESSION), engine)

    def feed():
        for eid in (visible, decision, other_project, audit_kind):
            hub.publish(_note(engine, eid))

    seen = asyncio.run(_collect(engine, hub, viewer, es.Interest(), feed))
    ids = [e["id"] for e in seen if e["kind"] == "message"]
    assert ids == [visible, decision]
    # the owner (org-wide, audit:read) sees every one
    owner = es.IdentityViewer(AuthContext(world.owner, AuthMethod.SESSION), engine)
    seen_owner = asyncio.run(_collect(engine, hub, owner, es.Interest(), feed))
    assert [e["id"] for e in seen_owner] == [visible, decision, other_project, audit_kind]


def test_last_event_id_replays_what_came_after_through_the_same_filters(engine, world):
    from boss_engine.identity.principal import AuthContext, AuthMethod

    anchor = _emit(engine, world.project_a, "decision.created", "decision")
    after_1 = _emit(engine, world.project_a, "decision.approved", "decision")
    skipped_kind = _emit(engine, world.project_a, "code_task.started")
    _emit(engine, world.project_b, "decision.created", "decision")
    after_2 = _emit(engine, world.project_a, "decision.rejected", "decision")
    live = []
    hub = es.Hub()
    viewer = es.IdentityViewer(AuthContext(world.viewer_a, AuthMethod.SESSION), engine)

    def feed():
        live.append(_emit(engine, world.project_a, "decision.created", "decision"))
        hub.publish(_note(engine, live[0]))
        hub.publish(_note(engine, after_2))        # already replayed: not sent twice

    seen = asyncio.run(_collect(engine, hub, viewer, es.Interest.parse("decision.", None), feed,
                                last_event_id=anchor))
    ids = [e["id"] for e in seen if e["kind"] == "message"]
    assert ids == [after_1, after_2, live[0]]
    assert skipped_kind not in ids
    unknown = asyncio.run(_collect(engine, es.Hub(), viewer, es.Interest(), lambda: None,
                                   last_event_id="evt_never", seconds=0.2))
    assert [e["kind"] for e in unknown] == ["resync"]


def test_the_route_streams_replay_and_is_declared_per_project(engine, world):
    client = TestClient(main.app, raise_server_exceptions=False)
    anchor = _emit(engine, world.project_a, "workflow.run_started")
    nxt = _emit(engine, world.project_a, "workflow.run_finished")
    with world.acting_as(world.ctx(world.viewer_a)):
        r = client.get("/v1/events/stream", params={"project_id": world.project_a,
                                                    "max_seconds": 1},
                       headers={"Last-Event-ID": anchor})
        other = client.get("/v1/events/stream", params={"project_id": world.project_b,
                                                        "max_seconds": 1})
        org_wide = client.get("/v1/events/stream", params={"max_seconds": 1})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    ids = [e["id"] for e in _sse_events(r.text) if e["kind"] == "message"]
    assert ids[0] == nxt and anchor not in ids
    assert other.status_code in (403, 404)
    assert org_wide.status_code in (403, 404)


def test_cleanup(engine, world):
    with engine.begin() as c:
        c.execute(text("DELETE FROM events WHERE project_id IN (:a, :b)"),
                  {"a": world.project_a, "b": world.project_b})
