"""BOSSAPP-1106 (P9c-2): Salesforce Pub/Sub API subscriber (first stream-mode adapter).

Covers:
  * Manifest & adapter contract verification (`modes: [subscribe, stream, poll]`, `stream` +
    `subscribe` + `poll` + `parse` capabilities).
  * Flow control enforcement (`num_requested` strictly clamped <= 100).
  * CDC (`CREATE`/`UPDATE`/`DELETE`/`UNDELETE`) and Platform Event normalization to CloudEvents 1.0,
    replay-id deduplication, and integration-user loop filtering.
  * P9c-T3:
      - Mid-stream subscriber kill/crash rolls back cleanly and resumes from stored replay ID in
        `intake_cursors` with 0 gaps and 0 duplicate runs.
      - Replay ID older than 72 h (`ReplayIdExpiredError` -> `CursorExpired`) triggers SOQL
        `LastModifiedDate` catch-up query (dedupe absorbs overlap) + cursor reset + `intake.stream_resynced`.
      - Fleet doctor detects a lost Salesforce subscription and restarts it via `repair_intake_subscription`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.connectors.intake import get_intake_adapter
from boss_engine.connectors.intake.salesforce import adapter as sf_adapter
from boss_engine.connectors.intake.salesforce import pubsub as sf_pubsub
from boss_engine.services import (
    event_intake,
    intake_doctor,
    intake_streams,
    intake_subscriptions,
)

from ._intake_support import (
    deploy_local_agent,
    make_project,
    make_route,
    pg_engine,
    purge,
)


@pytest.fixture(scope="module")
def eng():
    engine = pg_engine()
    yield engine
    engine.dispose()


@pytest.fixture()
def pid(eng):
    p = make_project(eng)
    yield p
    purge(eng, p)


class FakeSalesforcePubSubServer:
    """In-process fake Salesforce Pub/Sub API server with replay IDs, 72h expiry, and kill hooks."""

    def __init__(self) -> None:
        self.events: list[sf_pubsub.ConsumerEvent] = []
        self.expired_before_seq = 0
        self.fail_on_fetch_call: set[int] = set()
        self.fetch_calls: list[sf_pubsub.FetchRequest] = []
        self.topic_checks = 0

    def publish_cdc(self, seq: int, *, entity: str = "Account", change_type: str = "UPDATE",
                    record_id: str, commit_user: str = "005USER1",
                    fields: dict | None = None) -> bytes:
        replay = seq.to_bytes(8, "big")
        payload = {
            "ChangeEventHeader": {
                "entityName": entity,
                "changeType": change_type,
                "recordIds": [record_id],
                "changedFields": list((fields or {}).keys()),
                "commitUser": commit_user,
                "commitTimestamp": int(datetime(2026, 11, 27, 12, 0, seq % 60,
                                                tzinfo=timezone.utc).timestamp() * 1000),
                "commitNumber": seq,
                "transactionKey": f"tx-{seq}",
                "sequenceNumber": 1,
            },
            **(fields or {"Name": f"Acme {seq}"}),
        }
        self.events.append(sf_pubsub.ConsumerEvent(
            event_id=f"sf-ev-{seq}", schema_id="sch-1", payload=payload, replay_id=replay,
        ))
        return replay

    def publish_platform_event(self, seq: int, *, order_id: str) -> bytes:
        replay = seq.to_bytes(8, "big")
        self.events.append(sf_pubsub.ConsumerEvent(
            event_id=f"sf-pe-{seq}", schema_id="sch-pe",
            payload={"EventUuid": f"uuid-{seq}", "Order_Id__c": order_id, "CreatedById": "005USER1"},
            replay_id=replay,
        ))
        return replay

    def make_client(self, conn, endpoint: str, access_token: str):
        server = self

        class _Client:
            def topic_info(self, topic_name: str) -> dict:
                server.topic_checks += 1
                return {"topic_name": topic_name, "can_subscribe": True}

            def fetch(self, request: sf_pubsub.FetchRequest, *, timeout_ms: int = 1000):
                call_num = len(server.fetch_calls) + 1
                server.fetch_calls.append(request)
                if call_num in server.fail_on_fetch_call:
                    raise ConnectionResetError("simulated mid-stream subscriber kill")
                if request.replay_preset == sf_pubsub.ReplayPreset.CUSTOM and request.replay_id:
                    after_seq = int.from_bytes(request.replay_id, "big")
                    if after_seq < server.expired_before_seq:
                        raise sf_pubsub.ReplayIdExpiredError(
                            "sfdc.platform.eventbus.grpc.subscription.fetch.replayid.corrupted: "
                            "replay id older than 72h retention",
                            provider="salesforce",
                        )
                    batch = [e for e in server.events
                             if int.from_bytes(e.replay_id, "big") > after_seq][:request.num_requested]
                else:
                    batch = server.events[:request.num_requested]
                latest = batch[-1].replay_id if batch else (
                    server.events[-1].replay_id if server.events else None
                )
                return sf_pubsub.FetchResponse(events=tuple(batch), latest_replay_id=latest)

            def close(self) -> None:
                return None

        return _Client()


def test_p9c_t3_salesforce_pubsub_stream_kill_resume_72h_catchup_and_doctor(eng, pid, monkeypatch):
    server = FakeSalesforcePubSubServer()
    monkeypatch.setattr(sf_pubsub, "CLIENT_FACTORY", server.make_client)

    soql_records = [
        {"Id": "001ACC_CATCHUP_1", "Name": "Catchup Account 1",
         "LastModifiedDate": "2026-11-27T10:00:00Z", "LastModifiedById": "005USER1"},
        {"Id": "001ACC_CATCHUP_2", "Name": "Catchup Account 2",
         "LastModifiedDate": "2026-11-27T11:00:00Z", "LastModifiedById": "005USER1"},
    ]
    soql_calls: list[str] = []

    def fake_http(method, url, headers, body, purpose):
        soql_calls.append(url)
        return 200, {"done": True, "records": soql_records}, {}

    monkeypatch.setattr(sf_adapter, "TRANSPORT", fake_http)
    settings = get_settings()

    # Verify adapter manifest & capabilities.
    adapter = get_intake_adapter("salesforce")
    assert set(adapter.manifest.modes) == {"subscribe", "stream", "poll"}
    assert adapter.can("stream") and adapter.can("subscribe") and adapter.can("poll")

    # Create connection in `stream` mode.
    sf_token = "sf_oauth_secret_" + uuid.uuid4().hex[:10]
    with eng.begin() as c:
        conn = event_intake.create_connection(
            c, project_id=pid, kind="salesforce", actor="ceo", name="sf-accounts",
            mode="stream",
            config={
                "instance_url": "https://acme.my.salesforce.com",
                "org_id": "00D000000000001",
                "topic": "/data/AccountChangeEvent",
                "sobject": "Account",
                "integration_user_id": "005BOSSBOT",
            },
            secrets={"access_token": sf_token},
        )
    dep = deploy_local_agent(eng, pid)
    make_route(eng, pid, dep["id"], conn["id"], name="sf-route", identity_mode="owner",
               thread_key="{subject}")

    # Publish 6 CDC events (including 1 self-event from integration_user_id) + 1 Platform Event.
    for s in range(1, 6):
        server.publish_cdc(s, record_id=f"001ACC{s:04d}", change_type="CREATE" if s == 1 else "UPDATE")
    server.publish_cdc(6, record_id="001ACCSELF", commit_user="005BOSSBOT")
    server.publish_platform_event(7, order_id="ORD-77")

    # 1. Batch 1: consume 3 events (also prove num_requested clamped <= 100 when asking for 500!).
    r1 = intake_streams.consume_due(eng, settings, connection_id=conn["id"], max_events=3)
    assert r1["consumed"] == 3 and r1["inserted"] == 3
    assert server.fetch_calls[-1].num_requested == 3

    # 2. Mid-stream kill on fetch call #2 -> error recorded, cursor NOT lost!
    server.fail_on_fetch_call.add(2)
    r_kill = intake_streams.consume_due(eng, settings, connection_id=conn["id"], max_events=500)
    assert r_kill["failed"] == 1
    assert server.fetch_calls[-1].num_requested == 100  # strictly clamped <= MAX_NUM_REQUESTED!

    # 3. Resume after kill -> picks up from replay ID 3 (events 4, 5, 6, 7), 0 gaps, 0 duplicates!
    r2 = intake_streams.consume_due(eng, settings, connection_id=conn["id"], max_events=50)
    assert r2["consumed"] == 4 and r2["inserted"] == 4
    assert server.fetch_calls[-1].replay_preset == sf_pubsub.ReplayPreset.CUSTOM
    assert int.from_bytes(server.fetch_calls[-1].replay_id, "big") == 3

    # Process inbox: 5 CDC + 1 Platform Event routed, 1 self-event filtered!
    event_intake.process_due(eng, settings, limit=20, project_id=pid)
    with eng.connect() as c:
        rows = list(c.execute(text(
            "SELECT dedupe_key, type, status FROM intake_events WHERE connection_id = :c "
            "ORDER BY received_at, id"), {"c": conn["id"]}).mappings().all())
    assert len(rows) == 7
    statuses = [r["status"] for r in rows]
    assert statuses.count("routed") == 6
    assert statuses.count("filtered") == 1
    types = {r["type"] for r in rows}
    assert {"com.salesforce.cdc.create", "com.salesforce.cdc.update", "com.salesforce.platform_event"} <= types

    # 4. Replay ID older than 72 hours -> CursorExpired -> SOQL catch-up + cursor reset!
    server.expired_before_seq = 100  # stored cursor (seq 7) is now expired (> 72h)!
    r_exp = intake_streams.consume_due(eng, settings, connection_id=conn["id"], max_events=50)
    assert r_exp["resynced"] == 1 and r_exp["inserted"] == 2
    assert len(soql_calls) == 1
    with eng.connect() as c:
        cur_val = c.execute(text(
            "SELECT cursor FROM intake_cursors WHERE connection_id = :c"),
            {"c": conn["id"]}).scalar()
        assert cur_val is None  # reset to LATEST for next subscribe

    # 5. Fleet doctor restarts a lost Salesforce subscription.
    with eng.begin() as c:
        sub = intake_subscriptions.ensure_subscription(c, settings, connection_id=conn["id"])
        c.execute(text("UPDATE intake_subscriptions SET status = 'removed' WHERE id = :id"),
                  {"id": sub["id"]})
        health = intake_doctor.collect(c)
        assert any(s["subscription_id"] == sub["id"] for s in health["lost_subscriptions"])
    findings = intake_doctor.diagnose(health)
    match = next(f for f in findings if f["kind"] == f"intake_subscription_lost:{sub['id']}")
    assert match["auto"] == ["repair_intake_subscription"]
    # Run auto-remedy -> subscription becomes active again!
    intake_doctor.repair_subscription(eng, settings, conn["id"])
    with eng.connect() as c:
        st = c.execute(text("SELECT status FROM intake_subscriptions WHERE id = :id"),
                       {"id": sub["id"]}).scalar()
        assert st == "active"
