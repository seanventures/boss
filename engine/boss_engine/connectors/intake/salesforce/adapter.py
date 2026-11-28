"""Salesforce Pub/Sub API intake adapter (P9c-2, BOSSAPP-1106; EVENTING_LLD.md §3.6, §3.7, §13).

First consumer of the `subscribe` + `stream` half of the intake adapter contract (proving the slot
for Kafka/SQS/Google Pub/Sub/Service Bus in BOSSAPP-1108):
  * `open_stream(conn, cursor)` opens a Pub/Sub subscriber positioned at `cursor.value` (base64
    replay ID with `CUSTOM` preset) or `LATEST` when no cursor exists.
  * `consume(handle, max_events, max_wait_ms)` clamps `num_requested <= 100`, fetches CDC and
    Platform Events, normalizes them to CloudEvents 1.0 `IntakeEvent`s with replay-id dedupe
    (`salesforce:<org_id>:<replay_id_b64>`), and translates expired replay IDs (> 72 h) into
    :class:`CursorExpired`.
  * `commit(handle, upto)` records the committed replay ID cursor after Postgres commit.
  * `poll(conn, cursor, limit)` runs a bounded SOQL `LastModifiedDate` catch-up query when a
    replay ID expires past 72-hour retention.
  * `subscribe` / `renew` / `unsubscribe` manage the logical subscription record in
    `intake_subscriptions` so Fleet doctor monitors and restarts lost subscriptions uniformly.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote, urlencode

from ..base import IntakeAdapterBase
from ..contract import (
    AuthFailed,
    Cursor,
    CursorExpired,
    ExternalActor,
    Health,
    IntakeConnection,
    IntakeError,
    IntakeEvent,
    ParseRefused,
    PersonHint,
    PollResult,
    RateLimited,
    StoredRaw,
    StreamHandle,
    StreamRecord,
    Subscription,
    SubscriptionSpec,
)
from . import pubsub

CDC_OPERATIONS = {
    "CREATE": "create",
    "UPDATE": "update",
    "DELETE": "delete",
    "UNDELETE": "undelete",
    "GAP_CREATE": "create",
    "GAP_UPDATE": "update",
    "GAP_DELETE": "delete",
    "GAP_UNDELETE": "undelete",
    "GAP_OVERFLOW": "update",
}
_SOBJECT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,80}$")
SUBSCRIPTION_LEASE_HOURS = 24


def _default_http_transport(method: str, url: str, headers: Mapping[str, str],
                            body: Optional[bytes], purpose: str) -> tuple[int, Any, dict[str, str]]:
    from ....identity.http import fetch

    resp = fetch(method, url, headers=dict(headers), data=body, purpose=purpose)
    try:
        data = resp.json()
    except ValueError:
        data = None
    return resp.status, data, {str(k).lower(): str(v) for k, v in (resp.headers or {}).items()}


#: REST transport seam for OAuth client-credentials and SOQL 72h catch-up queries.
TRANSPORT: Callable[..., tuple[int, Any, dict[str, str]]] = _default_http_transport


def _call_http(method: str, url: str, headers: Mapping[str, str],
               body: Optional[bytes], purpose: str) -> tuple[int, Any, dict[str, str]]:
    got = TRANSPORT(method, url, headers, body, purpose)
    if len(got) == 2:
        return int(got[0]), got[1], {}
    return int(got[0]), got[1], {str(k).lower(): str(v) for k, v in (got[2] or {}).items()}


def _instance_url(conn: IntakeConnection) -> str:
    url = str(conn.get("instance_url") or "").strip().rstrip("/")
    if not url:
        raise ParseRefused("Salesforce connection requires 'instance_url'", provider="salesforce")
    return url


def _topic(conn: IntakeConnection, override: str = "") -> str:
    topic = str(override or conn.get("topic") or "").strip()
    if not topic:
        raise ParseRefused("Salesforce connection requires 'topic'", provider="salesforce")
    return topic


def _org_id(conn: IntakeConnection) -> str:
    return str(conn.get("org_id") or conn.id)


def _access_token(conn: IntakeConnection) -> str:
    token = conn.secret("access_token")
    if token:
        return token
    cid = str(conn.get("client_id") or "").strip()
    csec = conn.secret("client_secret")
    if cid and csec:
        url = f"{_instance_url(conn)}/services/oauth2/token"
        body = urlencode({
            "grant_type": "client_credentials",
            "client_id": cid,
            "client_secret": csec,
        }).encode("utf-8")
        status, data, _ = _call_http(
            "POST", url, {"Content-Type": "application/x-www-form-urlencoded"}, body,
            "intake.salesforce.token",
        )
        if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
            raise AuthFailed(f"Salesforce OAuth token exchange failed ({status})",
                             provider="salesforce")
        return str(data["access_token"])
    raise AuthFailed("Salesforce access_token (or client_id + client_secret) is not configured",
                     provider="salesforce")


def _normalize_event(conn: IntakeConnection, *, topic: str, payload: Mapping[str, Any],
                     replay_id_b64: str, event_id: str = "",
                     source_kind: str = "pubsub") -> IntakeEvent:
    org = _org_id(conn)
    header = payload.get("ChangeEventHeader")
    if isinstance(header, Mapping):
        entity = str(header.get("entityName") or conn.get("sobject") or "Record")
        change_type = str(header.get("changeType") or "UPDATE").upper()
        op = CDC_OPERATIONS.get(change_type, "update")
        rec_ids = [str(r) for r in (header.get("recordIds") or []) if r]
        rec_id = rec_ids[0] if rec_ids else str(payload.get("Id") or event_id)
        commit_user = str(header.get("commitUser") or payload.get("LastModifiedById") or "")
        commit_ts = header.get("commitTimestamp")
        time_iso = None
        if isinstance(commit_ts, (int, float)):
            sec = commit_ts / 1000.0 if commit_ts > 1e12 else float(commit_ts)
            time_iso = datetime.fromtimestamp(sec, tz=timezone.utc).isoformat()
        elif isinstance(commit_ts, str) and commit_ts:
            time_iso = commit_ts
        changed_fields = [str(f) for f in (header.get("changedFields") or []) if f]
        tx_key = str(header.get("transactionKey") or "")
        seq_num = header.get("sequenceNumber")
        ev_id = replay_id_b64 or (f"{tx_key}:{seq_num}" if tx_key else event_id or rec_id)
        dedupe = f"salesforce:{org}:{replay_id_b64}" if replay_id_b64 else f"salesforce:{org}:{ev_id}"
        fields = {k: v for k, v in payload.items() if k != "ChangeEventHeader"}
        causality = None
        if isinstance(payload.get("BOSS_Causality__c"), Mapping):
            c = payload["BOSS_Causality__c"]
            causality = {"run_id": c.get("run_id"), "deployment_id": c.get("deployment_id"),
                         "hops": int(c.get("hops") or 0)}
        return IntakeEvent(
            id=ev_id,
            source=f"salesforce://{org}",
            type=f"com.salesforce.cdc.{op}",
            subject=f"{entity}/{rec_id}" if rec_id else entity,
            time=time_iso,
            data={
                "record_type": entity,
                "record_id": rec_id,
                "record_ids": rec_ids,
                "operation": {"create": "created", "update": "updated",
                              "delete": "deleted", "undelete": "updated"}.get(op, op),
                "changed_fields": changed_fields,
                "fields": fields,
                "topic": topic,
                "replay_id": replay_id_b64,
                "commit_number": header.get("commitNumber"),
                "transaction_key": tx_key,
                "delivered_by": source_kind,
            },
            bossconnection=conn.id,
            bossconversation={"key": f"{entity}/{rec_id}"} if rec_id else None,
            bossactor=ExternalActor(
                kind="user" if commit_user else "service",
                external_id=commit_user,
                display=commit_user,
            ),
            bosscausality=causality,
            bossdedupe=dedupe,
        )

    # Platform Event (/event/*) or SOQL catch-up record.
    rec_id = str(payload.get("Id") or payload.get("EventUuid") or event_id or replay_id_b64)
    sobject = str(payload.get("_sobject") or conn.get("sobject") or topic.rsplit("/", 1)[-1])
    actor_id = str(payload.get("LastModifiedById") or payload.get("CreatedById") or "")
    ev_id = replay_id_b64 or event_id or rec_id
    dedupe = (f"salesforce:{org}:{replay_id_b64}" if replay_id_b64
              else f"salesforce:{org}:soql:{sobject}:{rec_id}:{payload.get('LastModifiedDate') or ''}")
    ev_type = ("com.salesforce.cdc.update" if source_kind == "soql_catchup"
               else "com.salesforce.platform_event")
    return IntakeEvent(
        id=ev_id,
        source=f"salesforce://{org}",
        type=ev_type,
        subject=f"{sobject}/{rec_id}",
        time=str(payload.get("LastModifiedDate") or payload.get("CreatedDate") or "") or None,
        data={
            "record_type": sobject,
            "record_id": rec_id,
            "operation": "updated" if source_kind == "soql_catchup" else "created",
            "changed_fields": [],
            "fields": {k: v for k, v in payload.items() if not str(k).startswith("_")},
            "topic": topic,
            "replay_id": replay_id_b64,
            "delivered_by": source_kind,
        },
        bossconnection=conn.id,
        bossconversation={"key": f"{sobject}/{rec_id}"},
        bossactor=ExternalActor(
            kind="user" if actor_id else "service",
            external_id=actor_id,
            display=actor_id,
        ),
        bossdedupe=dedupe,
    )


class SalesforceIntakeAdapter(IntakeAdapterBase):
    key = "salesforce"

    # -- STREAM (first consumer of the stream contract slot) --------------------------------------

    def open_stream(self, conn: IntakeConnection, cursor: Cursor) -> StreamHandle:
        token = _access_token(conn)
        topic = _topic(conn, cursor.stream_key if cursor.stream_key.startswith("/") else "")
        endpoint = str(conn.get("pubsub_endpoint") or pubsub.DEFAULT_PUBSUB_ENDPOINT).strip()
        client = pubsub.CLIENT_FACTORY(conn, endpoint, token)
        return StreamHandle(
            stream_key=topic,
            position=Cursor(stream_key=topic, value=cursor.value, cursor_time=cursor.cursor_time),
            native={"client": client, "conn": conn, "topic": topic, "committed": cursor.value},
        )

    def consume(self, h: StreamHandle, max_events: int, max_wait_ms: int) -> list[StreamRecord]:
        native = h.native or {}
        client: pubsub.PubSubClient = native["client"]
        conn: IntakeConnection = native["conn"]
        topic = str(native["topic"])
        replay_bytes = pubsub.decode_replay_id(h.position.value)
        preset = pubsub.ReplayPreset.CUSTOM if replay_bytes else pubsub.ReplayPreset.LATEST
        req = pubsub.FetchRequest(
            topic_name=topic,
            replay_preset=preset,
            replay_id=replay_bytes,
            num_requested=max(1, min(int(max_events or 1), pubsub.MAX_NUM_REQUESTED)),
        )
        try:
            resp = client.fetch(req, timeout_ms=max(50, int(max_wait_ms or 1000)))
        except pubsub.ReplayIdExpiredError as exc:
            raise CursorExpired(str(exc), provider=self.key) from exc
        records: list[StreamRecord] = []
        now_iso = datetime.now(timezone.utc).isoformat()
        for item in resp.events:
            replay_b64 = pubsub.encode_replay_id(item.replay_id)
            ev = _normalize_event(conn, topic=topic, payload=item.payload,
                                  replay_id_b64=replay_b64, event_id=item.event_id,
                                  source_kind="pubsub")
            pos = Cursor(stream_key=topic, value=replay_b64, cursor_time=ev.time or now_iso)
            records.append(StreamRecord(event=ev, position=pos))
            h.position = pos
        if not records and resp.latest_replay_id:
            h.position = Cursor(stream_key=topic,
                                value=pubsub.encode_replay_id(resp.latest_replay_id),
                                cursor_time=now_iso)
        return records

    def commit(self, h: StreamHandle, upto: Cursor) -> None:
        h.position = upto
        if isinstance(h.native, dict):
            h.native["committed"] = upto.value

    # -- SUBSCRIBE / RENEW / UNSUBSCRIBE ----------------------------------------------------------

    def subscribe(self, conn: IntakeConnection, spec: SubscriptionSpec) -> Subscription:
        token = _access_token(conn)
        topic = _topic(conn, spec.resource)
        endpoint = str(conn.get("pubsub_endpoint") or pubsub.DEFAULT_PUBSUB_ENDPOINT).strip()
        client = pubsub.CLIENT_FACTORY(conn, endpoint, token)
        try:
            info = client.topic_info(topic)
            if isinstance(info, dict) and info.get("can_subscribe") is False:
                raise AuthFailed(f"Salesforce topic {topic} is not subscribable", provider=self.key)
        finally:
            client.close()
        exp = datetime.now(timezone.utc) + timedelta(hours=SUBSCRIPTION_LEASE_HOURS)
        return Subscription(
            provider_subscription_id=f"sf-pubsub:{_org_id(conn)}:{topic}",
            resource=topic,
            expires_at=exp.isoformat(),
        )

    def renew(self, conn: IntakeConnection, sub: Subscription) -> Subscription:
        return self.subscribe(conn, SubscriptionSpec(resource=sub.resource))

    def unsubscribe(self, conn: IntakeConnection, sub: Subscription) -> None:
        # Salesforce Pub/Sub subscriptions end when the stream closes; no server-side delete RPC.
        return None

    # -- POLL (SOQL 72-hour catch-up after ReplayId expiry) ---------------------------------------

    def poll(self, conn: IntakeConnection, cursor: Cursor, limit: int) -> PollResult:
        token = _access_token(conn)
        instance = _instance_url(conn)
        topic = _topic(conn)
        sobject = str(conn.get("sobject") or "").strip()
        if not sobject:
            # Infer from `/data/AccountChangeEvent` -> `Account`.
            tail = topic.rsplit("/", 1)[-1]
            sobject = tail[:-len("ChangeEvent")] if tail.endswith("ChangeEvent") else tail
        if not _SOBJECT_RE.match(sobject):
            raise ParseRefused(f"Invalid Salesforce SObject name: {sobject!r}", provider=self.key)
        since_dt = datetime.now(timezone.utc) - timedelta(hours=pubsub.RETENTION_HOURS)
        if cursor.cursor_time:
            try:
                parsed = datetime.fromisoformat(str(cursor.cursor_time).replace("Z", "+00:00"))
                since_dt = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        cap = max(1, min(int(limit or 50), 200))
        soql = (f"SELECT Id, Name, LastModifiedDate, LastModifiedById FROM {sobject} "
                f"WHERE LastModifiedDate >= {since_iso} ORDER BY LastModifiedDate ASC LIMIT {cap}")
        url = f"{instance}/services/data/v61.0/query?q={quote(soql, safe='')}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        status, data, resp_headers = _call_http("GET", url, headers, None,
                                                "intake.salesforce.soql_catchup")
        if status in (401, 403):
            raise AuthFailed(f"Salesforce SOQL query refused ({status})", provider=self.key)
        if status == 429:
            raise RateLimited("Salesforce API rate limited", provider=self.key)
        if status != 200 or not isinstance(data, dict):
            raise IntakeError(f"Salesforce SOQL query answered {status}", provider=self.key)

        deliveries: list[StoredRaw] = []
        events: list[IntakeEvent] = []
        last_time = since_dt.isoformat()
        for rec in data.get("records") or []:
            if not isinstance(rec, dict) or not rec.get("Id"):
                continue
            item = {**rec, "_sobject": sobject}
            ev = _normalize_event(conn, topic=topic, payload=item, replay_id_b64="",
                                  event_id=str(rec["Id"]), source_kind="soql_catchup")
            wrapped = {"_boss_kind": "salesforce_event", "topic": topic, "replay_id": "",
                       "source_kind": "soql_catchup", "payload": item}
            raw_bytes = json.dumps(wrapped, sort_keys=True, default=str).encode("utf-8")
            deliveries.append(StoredRaw(body=raw_bytes,
                                        headers={"content-type": "application/json"},
                                        dedupe_key=ev.bossdedupe))
            events.append(ev)
            if ev.time:
                last_time = ev.time
        return PollResult(
            events=tuple(events),
            deliveries=tuple(deliveries),
            next_cursor=Cursor(stream_key=cursor.stream_key, value=cursor.value,
                               cursor_time=last_time),
            more=not bool(data.get("done", True)),
        )

    # -- PROCESSOR --------------------------------------------------------------------------------

    def parse(self, conn: IntakeConnection, raw: StoredRaw) -> list[IntakeEvent]:
        try:
            body = json.loads(raw.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ParseRefused(f"Salesforce body is not JSON: {type(exc).__name__}",
                               provider=self.key) from None
        if not isinstance(body, dict):
            raise ParseRefused("Salesforce body is not an object", provider=self.key)
        if body.get("_boss_kind") == "salesforce_event" and isinstance(body.get("payload"), dict):
            return [_normalize_event(
                conn,
                topic=str(body.get("topic") or conn.get("topic") or ""),
                payload=body["payload"],
                replay_id_b64=str(body.get("replay_id") or ""),
                event_id=str(body.get("event_id") or ""),
                source_kind=str(body.get("source_kind") or "pubsub"),
            )]
        # Raw CloudEvents / direct CDC payload
        return [_normalize_event(conn, topic=str(conn.get("topic") or ""), payload=body,
                                 replay_id_b64=str(body.get("replay_id") or ""),
                                 source_kind="pubsub")]

    def is_self(self, conn: IntakeConnection, ev: IntakeEvent) -> bool:
        ours = str(conn.get("integration_user_id") or "").strip()
        if ours and ev.bossactor.external_id and ev.bossactor.external_id == ours:
            return True
        if (ev.bosscausality or {}).get("run_id"):
            return True
        return False

    def conversation_key(self, conn: IntakeConnection, ev: IntakeEvent) -> Optional[str]:
        if ev.bossconversation and ev.bossconversation.get("key"):
            return str(ev.bossconversation["key"])
        return ev.subject

    def resolve_person(self, conn: IntakeConnection, actor: ExternalActor) -> PersonHint:
        return PersonHint(email=actor.email, display=actor.display)

    def health(self, conn: IntakeConnection) -> Health:
        try:
            token = _access_token(conn)
            topic = _topic(conn)
            endpoint = str(conn.get("pubsub_endpoint") or pubsub.DEFAULT_PUBSUB_ENDPOINT).strip()
            client = pubsub.CLIENT_FACTORY(conn, endpoint, token)
            try:
                info = client.topic_info(topic)
            finally:
                client.close()
            return Health(ok=True, detail="Salesforce Pub/Sub topic reachable",
                          checks={"topic": topic, "can_subscribe": bool(info.get("can_subscribe", True))})
        except Exception as exc:  # noqa: BLE001 — never expose secrets
            return Health(ok=False, detail=f"{type(exc).__name__}: {exc}"[:200])


__all__ = [
    "CDC_OPERATIONS",
    "SalesforceIntakeAdapter",
    "SUBSCRIPTION_LEASE_HOURS",
    "TRANSPORT",
]
