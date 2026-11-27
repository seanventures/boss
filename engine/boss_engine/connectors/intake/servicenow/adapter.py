"""ServiceNow on the intake layer (P9b-3, BOSSAPP-1102; boss-eventing-lld.md §3.7, §4.1, §15 D13).

Push + Table API poll safety net:
  * verify     Standard Webhooks (``webhook-id``, ``webhook-timestamp``, ``webhook-signature``
               HMAC-SHA256 with +/- 5 min replay tolerance + rotation grace), plus BOSS v1
               HMAC fallback (``x-boss-signature`` / ``x-boss-timestamp``).
  * poll       Table API keyset polling on ``(sys_updated_on, sys_id)`` via ``TicketHttp``
               and ``intake_cursors``, supporting both ``mode='poll'`` and ``mode='push'``
               safety-net polling when ``instance_url`` is configured.
  * dedupe     Push and poll produce identical ``head_key`` (``servicenow:<table>:<sys_id>:<mod>``)
               and ``conversation_key`` (``servicenow:<table>:<sys_id>``) so ``event_intake``
               deduplicates cross-delivery arrivals cleanly.
  * loop       Self-update suppression via ``service_account`` / ``sys_updated_by`` and
               ``boss`` causality marker.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from ...tickets.contract import (
    AuthFailed as TicketAuthFailed,
    HostNotAllowed as TicketHostNotAllowed,
    RateLimited as TicketRateLimited,
    TicketConnection,
)
from ...tickets.http import TicketHttp
from ..base import IntakeAdapterBase
from ..contract import (
    AuthFailed,
    Cursor,
    ExternalActor,
    HostNotAllowed,
    IntakeConnection,
    IntakeError,
    IntakeEvent,
    ParseRefused,
    PersonHint,
    PollResult,
    RateLimited,
    RawRequest,
    StoredRaw,
    Verified,
    VerifyRefused,
)
from ..webhook.adapter import (
    BOSS_DELIVERY,
    BOSS_SIGNATURE,
    SW_ID,
    SW_SIGNATURE,
    WebhookIntakeAdapter,
)

DEFAULT_TABLES = ("incident", "change_request")
MAX_TEXT = 20000


def _body(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads((raw or b"{}").decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tables_of(conn: IntakeConnection) -> list[str]:
    raw = conn.get("tables")
    if isinstance(raw, list):
        items = [str(t).strip() for t in raw if str(t).strip()]
    else:
        items = [t.strip() for t in str(raw or "").split(",") if t.strip()]
    return items or list(DEFAULT_TABLES)


def _norm_ts(raw_ts: Any) -> str:
    val = str(raw_ts or "").strip()
    if not val:
        return ""
    if " " in val and "T" not in val:
        return val.replace(" ", "T") + ("Z" if not val.endswith(("Z", "+00:00")) else "")
    return val


def _sn_ts(iso_or_sn: str) -> str:
    """Format an ISO timestamp into ServiceNow `YYYY-MM-DD HH:MM:SS` query format."""
    val = str(iso_or_sn or "").strip()
    if not val:
        return ""
    if "T" in val:
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00")).astimezone(timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return val[:19].replace("T", " ")


def _field_val(rec: Mapping[str, Any], key: str) -> str:
    v = rec.get(key)
    if isinstance(v, dict):
        return str(v.get("value") or v.get("display_value") or "").strip()
    return str(v or "").strip()


class ServiceNowIntakeAdapter(IntakeAdapterBase):
    key = "servicenow"

    def wants_safety_net_poll(self, conn: IntakeConnection) -> bool:
        return bool(str(conn.get("instance_url") or "").strip()) and conn.get("poll_safety_net") is not False

    # -- PUSH -------------------------------------------------------------------------------------

    def verify(self, conn: IntakeConnection, req: RawRequest, now: float) -> Verified:
        sw_sig = req.header(SW_SIGNATURE).strip()
        boss_sig = req.header(BOSS_SIGNATURE).strip()
        if not sw_sig and not boss_sig:
            raise VerifyRefused("missing_signature", provider=self.key)
        try:
            verified = WebhookIntakeAdapter().verify(conn, req, now)
            return Verified(
                scheme=f"servicenow_{verified.scheme}",
                timestamp=verified.timestamp,
                retry=verified.retry,
            )
        except VerifyRefused as refused:
            raise VerifyRefused(refused.reason, provider=self.key) from None

    def dedupe_key(self, conn: IntakeConnection, req: RawRequest) -> str:
        msg_id = req.header(SW_ID).strip()[:200]
        if msg_id:
            return f"servicenow:{msg_id}"
        delivery = req.header(BOSS_DELIVERY).strip()[:200]
        if delivery:
            return f"servicenow:{delivery}"
        body = _body(req.body)
        table = str(body.get("table") or body.get("sys_class_name") or "record").strip()
        sys_id = str(body.get("sys_id") or "").strip()
        mod = str(body.get("sys_mod_count") or body.get("sys_updated_on") or "").strip()
        if sys_id and mod:
            return f"servicenow:{table}:{sys_id}:{mod}"[:500]
        return "servicenow:sha256:" + req.body_sha256()

    # -- POLL -------------------------------------------------------------------------------------

    def _http(self, conn: IntakeConnection) -> TicketHttp:
        base_url = str(conn.get("instance_url") or "").strip().rstrip("/")
        if not base_url:
            raise AuthFailed("ServiceNow instance_url is required for polling", provider=self.key)
        tconn = TicketConnection(
            "servicenow",
            {"instance_url": base_url},
            project_id=conn.project_id,
            db=conn.db,
            app_settings=conn.app_settings,
            credential_resolver=lambda: (
                conn.secret("api_token")
                or conn.secret("password")
                or ""
            ),
        )

        def auth(cred: Any) -> tuple[dict[str, str], Optional[str]]:
            tok = conn.secret("api_token")
            if tok:
                return {"Authorization": f"Bearer {tok}"}, None
            user = str(conn.get("username") or "").strip()
            pwd = conn.secret("password") or ""
            if user and pwd:
                encoded = base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
                return {"Authorization": f"Basic {encoded}"}, None
            raise AuthFailed("ServiceNow requires api_token or username/password to poll",
                             provider=self.key)

        return TicketHttp(
            tconn,
            base_url=base_url,
            auth=auth,
            default_hosts=getattr(getattr(self, "manifest", None), "default_hosts", ("*.service-now.com",)),
            base_url_fields=("instance_url",),
            guard_provider="servicenow",
        )

    def poll(self, conn: IntakeConnection, cursor: Cursor, limit: int) -> PollResult:
        try:
            state = json.loads(cursor.value) if cursor.value else {}
        except ValueError:
            state = {}
        if not isinstance(state, dict):
            state = {}
        default_since = _sn_ts(cursor.cursor_time or "")
        http = self._http(conn)
        deliveries: list[StoredRaw] = []
        next_state = dict(state)
        latest_ts = cursor.cursor_time or ""
        tables = _tables_of(conn)
        per_table = max(1, int(limit) // max(1, len(tables)))

        for table in tables:
            tcur = state.get(table) if isinstance(state.get(table), dict) else {}
            last_ts = str(tcur.get("sys_updated_on") or default_since or "").strip()
            last_id = str(tcur.get("sys_id") or "").strip()
            parts = []
            if last_ts:
                if last_id:
                    parts.append(
                        f"sys_updated_on>{last_ts}^OR(sys_updated_on={last_ts}^sys_id>{last_id})"
                    )
                else:
                    parts.append(f"sys_updated_on>{last_ts}")
            parts.extend(["ORDERBYsys_updated_on", "ORDERBYsys_id"])
            params = {
                "sysparm_query": "^".join(parts),
                "sysparm_limit": str(per_table),
                "sysparm_display_value": "false",
            }
            try:
                payload = http.get_json(f"/api/now/table/{table}", params=params)
            except TicketAuthFailed as err:
                raise AuthFailed(str(err), provider=self.key) from None
            except TicketRateLimited as err:
                raise RateLimited(str(err), retry_after=err.retry_after, provider=self.key) from None
            except TicketHostNotAllowed as err:
                raise HostNotAllowed(str(err), provider=self.key) from None
            except Exception as err:
                raise IntakeError(f"ServiceNow poll failed: {err}", provider=self.key) from None

            records = (payload or {}).get("result") if isinstance(payload, dict) else []
            if not isinstance(records, list):
                records = []
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                sys_id = _field_val(rec, "sys_id")
                updated = _field_val(rec, "sys_updated_on")
                mod = _field_val(rec, "sys_mod_count") or updated
                if not sys_id:
                    continue
                envelope_body = json.dumps({
                    "_servicenow_poll": True,
                    "table": table,
                    "record": rec,
                }, sort_keys=True).encode("utf-8")
                dk = f"servicenow:poll:{table}:{sys_id}:{mod}"[:500]
                deliveries.append(StoredRaw(
                    body=envelope_body,
                    headers={"content-type": "application/json", "x-servicenow-delivery": "poll"},
                    dedupe_key=dk,
                ))
                next_state[table] = {"sys_updated_on": updated or last_ts, "sys_id": sys_id}
                if updated:
                    latest_ts = _norm_ts(updated)

        return PollResult(
            events=tuple(
                ev for raw in deliveries for ev in self.parse(conn, raw)
            ),
            deliveries=tuple(deliveries),
            next_cursor=Cursor(
                stream_key=cursor.stream_key,
                value=json.dumps(next_state, sort_keys=True),
                cursor_time=latest_ts or cursor.cursor_time,
            ),
        )

    # -- PROCESSOR --------------------------------------------------------------------------------

    def parse(self, conn: IntakeConnection, raw: StoredRaw) -> list[IntakeEvent]:
        body = _body(raw.body)
        if not body:
            raise ParseRefused("ServiceNow body is not a JSON object", provider=self.key)
        is_poll = bool(body.get("_servicenow_poll"))
        rec = body.get("record") if is_poll and isinstance(body.get("record"), dict) else body
        table = str(body.get("table") or _field_val(rec, "sys_class_name") or "incident").strip().lower()
        sys_id = _field_val(rec, "sys_id") or str(body.get("sys_id") or "").strip()
        number = _field_val(rec, "number") or str(body.get("number") or "").strip()
        if not sys_id and not number:
            raise ParseRefused("ServiceNow payload missing sys_id and number", provider=self.key)

        updated_on = _field_val(rec, "sys_updated_on")
        mod_count = _field_val(rec, "sys_mod_count")
        updated_by = _field_val(rec, "sys_updated_by")
        created_by = _field_val(rec, "sys_created_by")
        op = str(body.get("operation") or "").strip().lower()
        comments = _field_val(rec, "comments") or _field_val(rec, "work_notes")
        if not op:
            if comments and mod_count not in ("", "0"):
                op = "commented"
            elif mod_count == "0":
                op = "created"
            else:
                op = "updated"

        if table in ("incident", "change_request"):
            ev_type = f"com.servicenow.{table}.{op}" if op in ("created", "updated", "commented") else f"com.servicenow.{table}.updated"
        else:
            ev_type = "com.servicenow.record.updated"

        head_version = mod_count or updated_on
        head_key = f"servicenow:{table}:{sys_id or number}:{head_version}" if head_version else ""
        conv_key = f"servicenow:{table}:{sys_id or number}"
        instance = str(conn.get("instance_url") or body.get("instance_url") or "servicenow").rstrip("/")

        actor_obj = body.get("actor") if isinstance(body.get("actor"), dict) else {}
        actor_id = str(actor_obj.get("user_name") or actor_obj.get("sys_id") or updated_by or created_by or "").strip()
        actor_name = str(actor_obj.get("name") or actor_id or "").strip()
        actor_email = str(actor_obj.get("email") or "").strip() or None
        actor = ExternalActor(
            kind="user" if actor_id else "unknown",
            external_id=actor_id,
            display=actor_name,
            email=actor_email,
        )

        short_desc = _field_val(rec, "short_description")
        desc = _field_val(rec, "description")
        data: dict[str, Any] = {
            "record_type": table,
            "record_id": sys_id or number,
            "record_key": number or sys_id,
            "table": table,
            "sys_id": sys_id or None,
            "number": number or None,
            "title": short_desc,
            "body": desc[:MAX_TEXT] if desc else "",
            "comment": comments[:MAX_TEXT] if comments else "",
            "state": _field_val(rec, "state") or None,
            "priority": _field_val(rec, "priority") or None,
            "sys_updated_on": updated_on or None,
            "sys_updated_by": updated_by or None,
            "sys_mod_count": mod_count or None,
            "operation": op,
            "delivered_by": "poll" if is_poll else "webhook",
            "head_key": head_key or None,
            "record_url": f"{instance}/nav_to.do?uri={table}.do?sys_id={sys_id}" if (instance.startswith("http") and sys_id) else None,
        }

        ev = IntakeEvent(
            id=str(raw.headers.get("webhook-id") or f"{table}:{sys_id}:{head_version or raw.dedupe_key}"),
            source=instance,
            type=ev_type,
            subject=number or f"{table}/{sys_id}",
            time=_norm_ts(updated_on) or None,
            data=data,
            bossactor=actor,
            bossconnection=conn.id,
            bossdedupe=raw.dedupe_key,
            bossconversation={"key": conv_key, "table": table, "sys_id": sys_id or number},
        )
        if isinstance(body.get("boss"), dict):
            ev.bosscausality = {
                "run_id": body["boss"].get("run_id"),
                "deployment_id": body["boss"].get("deployment_id"),
                "hops": int(body["boss"].get("hops") or 0),
                "endpoint_id": body["boss"].get("endpoint_id"),
            }
        return [ev]

    def is_self(self, conn: IntakeConnection, ev: IntakeEvent) -> bool:
        svc = str(conn.get("service_account") or "").strip().lower()
        if svc:
            upd = str((ev.data or {}).get("sys_updated_by") or "").strip().lower()
            ext = str(ev.bossactor.external_id or "").strip().lower()
            if upd == svc or ext == svc:
                return True
        return super().is_self(conn, ev)

    def resolve_person(self, conn: IntakeConnection, actor: ExternalActor) -> PersonHint:
        return PersonHint(email=actor.email, display=actor.display, is_bot=actor.kind == "bot")

    def conversation_key(self, conn: IntakeConnection, ev: IntakeEvent) -> Optional[str]:
        return (ev.bossconversation or {}).get("key") or f"servicenow:{ev.subject}"


__all__ = ["DEFAULT_TABLES", "ServiceNowIntakeAdapter"]
