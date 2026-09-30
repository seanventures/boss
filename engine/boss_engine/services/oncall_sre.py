"""BOSS-native on-call / SRE control plane.

Schedules + rotations, escalation policies, paging bridge from reliability
incidents, adapter catalog (PagerDuty / Opsgenie / Grafana OnCall / Slack /
Mattermost). External notify attempts are always receipted with a unique
``dedupe_key``. Without a webhook/token the path is dry-run (``simulated=true``);
``force_live=true`` without credentials fails closed with a clear error.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id, now_utc
from .knowledge_graph import KnowledgeGraphService
from .receipts import ReceiptService

ADAPTERS = (
    "pagerduty",
    "opsgenie",
    "grafana_oncall",
    "slack_webhook",
    "mattermost",
)

# Env vars that unlock live delivery for each adapter (any non-empty value).
_ADAPTER_TOKEN_ENV = {
    "pagerduty": "PAGERDUTY_TOKEN",
    "opsgenie": "OPSGENIE_API_KEY",
    "grafana_oncall": "GRAFANA_ONCALL_WEBHOOK",
    "slack_webhook": "ONCALL_SLACK_WEBHOOK",
    "mattermost": "ONCALL_MATTERMOST_WEBHOOK",
}
# Fall back to the reliability webhook for chat adapters.
_ADAPTER_FALLBACK_ENV = {
    "slack_webhook": "RELIABILITY_ONCALL_WEBHOOK",
    "mattermost": "RELIABILITY_ONCALL_WEBHOOK",
    "grafana_oncall": "RELIABILITY_ONCALL_WEBHOOK",
}

_PAGE_STATUSES = ("open", "acked", "resolved")
_SEVERITIES = ("info", "warning", "critical")

_DDL = """
CREATE TABLE IF NOT EXISTS oncall_schedules (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  name TEXT NOT NULL,
  timezone TEXT NOT NULL DEFAULT 'UTC',
  layers JSONB NOT NULL DEFAULT '[]'::jsonb,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_oncall_schedules_project
  ON oncall_schedules(project_id, enabled);

CREATE TABLE IF NOT EXISTS oncall_policies (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  name TEXT NOT NULL,
  schedule_id TEXT,
  steps JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_oncall_policies_project
  ON oncall_policies(project_id);

CREATE TABLE IF NOT EXISTS oncall_pages (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  reliability_incident_id TEXT,
  dedupe_key TEXT NOT NULL,
  title TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'warning',
  status TEXT NOT NULL DEFAULT 'open',
  schedule_id TEXT,
  policy_id TEXT,
  current_step INTEGER NOT NULL DEFAULT 0,
  assignee TEXT NOT NULL DEFAULT '',
  service TEXT NOT NULL DEFAULT '',
  kg_node_id TEXT,
  kg_node_key TEXT,
  adapter TEXT NOT NULL DEFAULT 'slack_webhook',
  notify_result JSONB NOT NULL DEFAULT '{}'::jsonb,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  acked_at TIMESTAMPTZ,
  resolved_at TIMESTAMPTZ,
  UNIQUE (project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_oncall_pages_project
  ON oncall_pages(project_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_oncall_pages_incident
  ON oncall_pages(project_id, reliability_incident_id);
"""


class OncallSreError(Exception):
    """Domain error with optional HTTP-ish code for the router."""

    def __init__(self, message: str, *, status_code: int = 400, payload: Optional[dict] = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


def adapter_credential(adapter: str) -> tuple[bool, str, str]:
    """Return (live, env_name, token_or_url). Empty token → dry-run."""
    primary = _ADAPTER_TOKEN_ENV.get(adapter, "")
    fallback = _ADAPTER_FALLBACK_ENV.get(adapter, "")
    for env_name in (primary, fallback):
        if not env_name:
            continue
        val = (os.environ.get(env_name) or "").strip()
        if val:
            return True, env_name, val
    return False, primary or "unset", ""


def _row_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
        elif hasattr(v, "__iter__") and not isinstance(v, (str, bytes, dict, list)):
            # asyncpg/psycopg may return custom JSON types already decoded
            pass
    return d


def _normalize_layers(layers: Any) -> list[dict[str, Any]]:
    if not layers:
        return []
    if not isinstance(layers, list):
        raise OncallSreError("layers must be a list of rotation layers")
    out = []
    for i, layer in enumerate(layers):
        if not isinstance(layer, dict):
            raise OncallSreError(f"layers[{i}] must be an object")
        participants = layer.get("participants") or []
        if not isinstance(participants, list):
            raise OncallSreError(f"layers[{i}].participants must be a list")
        handoff = int(layer.get("handoff_seconds") or layer.get("rotation_seconds") or 604800)
        out.append({
            "name": str(layer.get("name") or f"layer-{i + 1}"),
            "rotation_type": str(layer.get("rotation_type") or "weekly"),
            "handoff_seconds": max(60, handoff),
            "start_at": layer.get("start_at") or now_utc().isoformat(),
            "participants": [
                {
                    "user_id": str(p.get("user_id") or p.get("id") or p.get("name") or f"user-{j}"),
                    "contact": str(p.get("contact") or p.get("email") or ""),
                    "display_name": str(p.get("display_name") or p.get("name") or p.get("user_id") or ""),
                }
                for j, p in enumerate(participants)
                if isinstance(p, dict)
            ],
        })
    return out


def _normalize_steps(steps: Any) -> list[dict[str, Any]]:
    if not steps:
        return [{"wait_minutes": 0, "channels": [{"adapter": "slack_webhook", "target": "default"}]}]
    if not isinstance(steps, list):
        raise OncallSreError("steps must be a list")
    out = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise OncallSreError(f"steps[{i}] must be an object")
        channels = step.get("channels") or []
        if not channels and step.get("adapter"):
            channels = [{"adapter": step["adapter"], "target": step.get("target") or "default"}]
        if not isinstance(channels, list) or not channels:
            channels = [{"adapter": "slack_webhook", "target": "default"}]
        norm_ch = []
        for ch in channels:
            if not isinstance(ch, dict):
                continue
            adapter = str(ch.get("adapter") or "slack_webhook")
            if adapter not in ADAPTERS:
                raise OncallSreError(f"unknown adapter {adapter!r}; choose from {ADAPTERS}")
            norm_ch.append({
                "adapter": adapter,
                "target": str(ch.get("target") or "default"),
            })
        out.append({
            "wait_minutes": max(0, int(step.get("wait_minutes") or 0)),
            "channels": norm_ch or [{"adapter": "slack_webhook", "target": "default"}],
            "targets": [str(t) for t in (step.get("targets") or [])],
        })
    return out


class OncallSreService:
    def __init__(self, settings=None):
        self.settings = settings
        self.kg = KnowledgeGraphService(settings)
        self.receipts = ReceiptService()
        self._tables_ready = False

    def ensure_tables(self, connection: Connection) -> None:
        """Idempotent DDL so ad-hoc test DBs and pre-migration envs still work."""
        if self._tables_ready:
            return
        for stmt in _DDL.split(";"):
            s = stmt.strip()
            if s:
                connection.execute(text(s))
        self._tables_ready = True

    # ── schedules ──────────────────────────────────────────────────────────
    def create_schedule(
        self,
        connection: Connection,
        project_id: str,
        name: str,
        *,
        timezone: str = "UTC",
        layers: Optional[list] = None,
        enabled: bool = True,
        actor: str = "system",
    ) -> dict[str, Any]:
        self.ensure_tables(connection)
        name = (name or "").strip()
        if not name:
            raise OncallSreError("name is required")
        sid = make_id("ocs")
        norm_layers = _normalize_layers(layers)
        connection.execute(
            text(
                """
                INSERT INTO oncall_schedules
                  (id, project_id, name, timezone, layers, enabled, created_at, updated_at)
                VALUES
                  (:id, :p, :name, :tz, CAST(:layers AS JSONB), :en, :now, :now)
                """
            ),
            {
                "id": sid,
                "p": project_id,
                "name": name,
                "tz": timezone or "UTC",
                "layers": json.dumps(norm_layers),
                "en": bool(enabled),
                "now": now_utc(),
            },
        )
        emit_event(
            connection, project_id, "oncall.schedule.created", actor,
            "oncall_schedule", sid, {"name": name, "layers": len(norm_layers)},
        )
        return self.get_schedule(connection, project_id, sid)  # type: ignore[return-value]

    def list_schedules(self, connection: Connection, project_id: str) -> list[dict[str, Any]]:
        self.ensure_tables(connection)
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, name, timezone, layers, enabled, created_at, updated_at
                FROM oncall_schedules WHERE project_id = :p
                ORDER BY name
                """
            ),
            {"p": project_id},
        ).mappings().all()
        return [_row_to_dict(r) for r in rows]

    def get_schedule(
        self, connection: Connection, project_id: str, schedule_id: str
    ) -> Optional[dict[str, Any]]:
        self.ensure_tables(connection)
        row = connection.execute(
            text(
                """
                SELECT id, project_id, name, timezone, layers, enabled, created_at, updated_at
                FROM oncall_schedules WHERE project_id = :p AND id = :id
                """
            ),
            {"p": project_id, "id": schedule_id},
        ).mappings().first()
        return _row_to_dict(row) if row else None

    def current_oncall(
        self, connection: Connection, project_id: str, schedule_id: Optional[str] = None,
        *, at: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """Compute who is on-call now from rotation layers (deterministic)."""
        self.ensure_tables(connection)
        when = at or now_utc()
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        schedules = (
            [self.get_schedule(connection, project_id, schedule_id)]
            if schedule_id
            else self.list_schedules(connection, project_id)
        )
        results: list[dict[str, Any]] = []
        for sched in schedules:
            if not sched or not sched.get("enabled", True):
                continue
            layers = sched.get("layers") or []
            if isinstance(layers, str):
                layers = json.loads(layers)
            for layer in layers:
                participants = layer.get("participants") or []
                if not participants:
                    continue
                handoff = max(60, int(layer.get("handoff_seconds") or 604800))
                start_raw = layer.get("start_at") or when.isoformat()
                try:
                    start = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
                except ValueError:
                    start = when
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                elapsed = max(0, int((when - start).total_seconds()))
                idx = (elapsed // handoff) % len(participants)
                holder = participants[idx]
                results.append({
                    "schedule_id": sched["id"],
                    "schedule_name": sched["name"],
                    "layer": layer.get("name"),
                    "user_id": holder.get("user_id"),
                    "contact": holder.get("contact"),
                    "display_name": holder.get("display_name") or holder.get("user_id"),
                    "rotation_index": idx,
                    "handoff_seconds": handoff,
                })
        return results

    # ── policies ───────────────────────────────────────────────────────────
    def create_policy(
        self,
        connection: Connection,
        project_id: str,
        name: str,
        *,
        steps: Optional[list] = None,
        schedule_id: Optional[str] = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        self.ensure_tables(connection)
        name = (name or "").strip()
        if not name:
            raise OncallSreError("name is required")
        if schedule_id and not self.get_schedule(connection, project_id, schedule_id):
            raise OncallSreError(f"schedule {schedule_id} not found", status_code=404)
        pid = make_id("ocp")
        norm_steps = _normalize_steps(steps)
        connection.execute(
            text(
                """
                INSERT INTO oncall_policies
                  (id, project_id, name, schedule_id, steps, created_at, updated_at)
                VALUES
                  (:id, :p, :name, :sid, CAST(:steps AS JSONB), :now, :now)
                """
            ),
            {
                "id": pid,
                "p": project_id,
                "name": name,
                "sid": schedule_id,
                "steps": json.dumps(norm_steps),
                "now": now_utc(),
            },
        )
        emit_event(
            connection, project_id, "oncall.policy.created", actor,
            "oncall_policy", pid, {"name": name, "steps": len(norm_steps)},
        )
        return self.get_policy(connection, project_id, pid)  # type: ignore[return-value]

    def list_policies(self, connection: Connection, project_id: str) -> list[dict[str, Any]]:
        self.ensure_tables(connection)
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, name, schedule_id, steps, created_at, updated_at
                FROM oncall_policies WHERE project_id = :p ORDER BY name
                """
            ),
            {"p": project_id},
        ).mappings().all()
        return [_row_to_dict(r) for r in rows]

    def get_policy(
        self, connection: Connection, project_id: str, policy_id: str
    ) -> Optional[dict[str, Any]]:
        self.ensure_tables(connection)
        row = connection.execute(
            text(
                """
                SELECT id, project_id, name, schedule_id, steps, created_at, updated_at
                FROM oncall_policies WHERE project_id = :p AND id = :id
                """
            ),
            {"p": project_id, "id": policy_id},
        ).mappings().first()
        return _row_to_dict(row) if row else None

    # ── pages / incidents ──────────────────────────────────────────────────
    def get_page(
        self, connection: Connection, project_id: str, page_id: str
    ) -> Optional[dict[str, Any]]:
        self.ensure_tables(connection)
        row = connection.execute(
            text("SELECT * FROM oncall_pages WHERE project_id = :p AND id = :id"),
            {"p": project_id, "id": page_id},
        ).mappings().first()
        return _row_to_dict(row) if row else None

    def list_pages(
        self,
        connection: Connection,
        project_id: str,
        *,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.ensure_tables(connection)
        clause = " AND status = :status" if status else ""
        params: dict[str, Any] = {"p": project_id, "limit": limit}
        if status:
            params["status"] = status
        rows = connection.execute(
            text(
                f"""
                SELECT * FROM oncall_pages
                WHERE project_id = :p{clause}
                ORDER BY created_at DESC LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
        return [_row_to_dict(r) for r in rows]

    def _tenant(self, connection: Connection, project_id: str) -> dict[str, str]:
        row = connection.execute(
            text(
                """
                SELECT w.org_id, p.workspace_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if not row:
            raise OncallSreError("Project not found", status_code=404)
        return {"org_id": row["org_id"], "workspace_id": row["workspace_id"]}

    def page(
        self,
        connection: Connection,
        project_id: str,
        *,
        title: str,
        severity: str = "warning",
        reliability_incident_id: Optional[str] = None,
        service: str = "",
        schedule_id: Optional[str] = None,
        policy_id: Optional[str] = None,
        adapter: str = "slack_webhook",
        dedupe_key: Optional[str] = None,
        force_live: bool = False,
        payload: Optional[dict] = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Open (or reuse) a page and attempt notify through the adapter catalog."""
        self.ensure_tables(connection)
        title = (title or "").strip() or "On-call page"
        severity = severity if severity in _SEVERITIES else "warning"
        adapter = adapter if adapter in ADAPTERS else "slack_webhook"

        if reliability_incident_id:
            inc = connection.execute(
                text("SELECT id, title, service, severity, status FROM incidents "
                     "WHERE project_id = :p AND id = :id"),
                {"p": project_id, "id": reliability_incident_id},
            ).mappings().first()
            if not inc and not title:
                raise OncallSreError(
                    f"reliability incident {reliability_incident_id} not found",
                    status_code=404,
                )
            if inc:
                title = title or inc["title"]
                service = service or (inc.get("service") or "")
                severity = severity if severity != "warning" else (inc.get("severity") or severity)

        if schedule_id and not self.get_schedule(connection, project_id, schedule_id):
            raise OncallSreError(f"schedule {schedule_id} not found", status_code=404)
        policy = None
        if policy_id:
            policy = self.get_policy(connection, project_id, policy_id)
            if not policy:
                raise OncallSreError(f"policy {policy_id} not found", status_code=404)
            if not schedule_id:
                schedule_id = policy.get("schedule_id")

        oncall_now = self.current_oncall(connection, project_id, schedule_id)
        assignee = (oncall_now[0]["user_id"] if oncall_now else "") or ""

        key = (dedupe_key or "").strip() or (
            f"oncall.page:{project_id}:{reliability_incident_id or title}:{severity}"
        )
        existing = connection.execute(
            text("SELECT * FROM oncall_pages WHERE project_id = :p AND dedupe_key = :k"),
            {"p": project_id, "k": key},
        ).mappings().first()
        if existing:
            page = _row_to_dict(existing)
            if page["status"] != "resolved":
                return {
                    "page": page,
                    "reused": True,
                    "notify": page.get("notify_result") or {},
                    "simulated": bool((page.get("notify_result") or {}).get("simulated", True)),
                }

        page_id = make_id("ocpg")
        # Prefer first policy step channel adapter when present
        if policy:
            steps = policy.get("steps") or []
            if isinstance(steps, str):
                steps = json.loads(steps)
            if steps and steps[0].get("channels"):
                adapter = steps[0]["channels"][0].get("adapter") or adapter

        connection.execute(
            text(
                """
                INSERT INTO oncall_pages (
                  id, project_id, reliability_incident_id, dedupe_key, title, severity,
                  status, schedule_id, policy_id, current_step, assignee, service,
                  adapter, payload, created_at, updated_at
                ) VALUES (
                  :id, :p, :inc, :k, :title, :sev, 'open', :sid, :pid, 0, :assignee,
                  :svc, :adapter, CAST(:payload AS JSONB), :now, :now
                )
                """
            ),
            {
                "id": page_id,
                "p": project_id,
                "inc": reliability_incident_id,
                "k": key,
                "title": title,
                "sev": severity,
                "sid": schedule_id,
                "pid": policy_id,
                "assignee": assignee,
                "svc": service or "",
                "adapter": adapter,
                "payload": json.dumps(payload or {}),
                "now": now_utc(),
            },
        )

        # Ensure service + incident nodes exist on the knowledge graph
        if service:
            self.kg.upsert_node(
                connection, project_id, "service", service,
                name=service, source="oncall_sre",
            )
        self.kg.upsert_node(
            connection, project_id, "incident", page_id,
            name=title,
            attrs={
                "severity": severity,
                "reliability_incident_id": reliability_incident_id,
                "assignee": assignee,
            },
            layer="inferred",
            source="oncall_sre",
            confidence=1.0,
        )
        if service:
            svc_node = self.kg.get_node(connection, project_id, "service", service)
            inc_node = self.kg.get_node(connection, project_id, "incident", page_id)
            if svc_node and inc_node:
                self.kg.upsert_edge(
                    connection, project_id, inc_node["id"], svc_node["id"],
                    "correlates_with", layer="inferred", source="oncall_sre",
                )

        notify = self._notify(
            connection,
            project_id,
            page_id=page_id,
            title=title,
            severity=severity,
            service=service,
            assignee=assignee,
            adapter=adapter,
            dedupe_key=f"oncall.notify:{key}",
            force_live=force_live,
            actor=actor,
            extra={"reliability_incident_id": reliability_incident_id},
        )
        connection.execute(
            text(
                """
                UPDATE oncall_pages
                SET notify_result = CAST(:nr AS JSONB), updated_at = :now
                WHERE id = :id
                """
            ),
            {"nr": json.dumps(notify), "now": now_utc(), "id": page_id},
        )
        emit_event(
            connection, project_id, "oncall.page.opened", actor,
            "oncall_page", page_id,
            {
                "title": title,
                "severity": severity,
                "adapter": adapter,
                "simulated": notify.get("simulated", True),
                "assignee": assignee,
            },
        )
        page = self.get_page(connection, project_id, page_id)
        return {
            "page": page,
            "reused": False,
            "notify": notify,
            "simulated": bool(notify.get("simulated", True)),
            "oncall": oncall_now,
        }

    def page_reliability_incident(
        self,
        connection: Connection,
        project_id: str,
        incident_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Bridge: reliability incident → on-call page."""
        inc = connection.execute(
            text(
                """
                SELECT id, title, service, severity, status
                FROM incidents WHERE project_id = :p AND id = :id
                """
            ),
            {"p": project_id, "id": incident_id},
        ).mappings().first()
        if not inc:
            # Synthetic page allowed if caller supplies title via kwargs
            if not kwargs.get("title"):
                raise OncallSreError(
                    f"reliability incident {incident_id} not found",
                    status_code=404,
                )
            return self.page(
                connection, project_id,
                reliability_incident_id=incident_id,
                **kwargs,
            )
        return self.page(
            connection,
            project_id,
            title=kwargs.pop("title", None) or inc["title"],
            severity=kwargs.pop("severity", None) or inc["severity"],
            service=kwargs.pop("service", None) or (inc.get("service") or ""),
            reliability_incident_id=incident_id,
            **kwargs,
        )

    def ack(
        self,
        connection: Connection,
        project_id: str,
        page_id: str,
        *,
        actor: str = "system",
        note: str = "",
    ) -> dict[str, Any]:
        self.ensure_tables(connection)
        page = self.get_page(connection, project_id, page_id)
        if not page:
            raise OncallSreError(f"page {page_id} not found", status_code=404)
        if page["status"] == "resolved":
            raise OncallSreError("cannot ack a resolved page", status_code=409)
        if page["status"] == "acked":
            return {"page": page, "already": True}
        connection.execute(
            text(
                """
                UPDATE oncall_pages
                SET status = 'acked', acked_at = :now, updated_at = :now,
                    assignee = CASE WHEN assignee = '' THEN :actor ELSE assignee END
                WHERE id = :id
                """
            ),
            {"now": now_utc(), "id": page_id, "actor": actor},
        )
        emit_event(
            connection, project_id, "oncall.page.acked", actor,
            "oncall_page", page_id, {"note": note},
        )
        return {"page": self.get_page(connection, project_id, page_id), "already": False}

    def resolve(
        self,
        connection: Connection,
        project_id: str,
        page_id: str,
        *,
        actor: str = "system",
        note: str = "",
    ) -> dict[str, Any]:
        self.ensure_tables(connection)
        page = self.get_page(connection, project_id, page_id)
        if not page:
            raise OncallSreError(f"page {page_id} not found", status_code=404)
        if page["status"] == "resolved":
            return {"page": page, "already": True}
        connection.execute(
            text(
                """
                UPDATE oncall_pages
                SET status = 'resolved', resolved_at = :now, updated_at = :now
                WHERE id = :id
                """
            ),
            {"now": now_utc(), "id": page_id},
        )
        emit_event(
            connection, project_id, "oncall.page.resolved", actor,
            "oncall_page", page_id, {"note": note},
        )
        return {"page": self.get_page(connection, project_id, page_id), "already": False}

    def from_reliability(
        self,
        connection: Connection,
        project_id: str,
        *,
        limit: int = 20,
        policy_id: Optional[str] = None,
        schedule_id: Optional[str] = None,
        adapter: str = "slack_webhook",
        force_live: bool = False,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Pull latest open reliability incidents and open pages for each."""
        self.ensure_tables(connection)
        rows = connection.execute(
            text(
                """
                SELECT id, title, service, severity, status
                FROM incidents
                WHERE project_id = :p AND status IN ('open', 'triaged', 'diagnosing', 'remediating')
                ORDER BY last_seen DESC
                LIMIT :limit
                """
            ),
            {"p": project_id, "limit": limit},
        ).mappings().all()
        opened: list[dict[str, Any]] = []
        for inc in rows:
            result = self.page(
                connection,
                project_id,
                title=inc["title"],
                severity=inc["severity"],
                service=inc.get("service") or "",
                reliability_incident_id=inc["id"],
                policy_id=policy_id,
                schedule_id=schedule_id,
                adapter=adapter,
                force_live=force_live,
                actor=actor,
                dedupe_key=f"oncall.from_reliability:{project_id}:{inc['id']}",
            )
            opened.append(result)
        return {
            "project_id": project_id,
            "incidents_considered": len(rows),
            "pages": opened,
        }

    def graph_link(
        self,
        connection: Connection,
        project_id: str,
        *,
        page_id: Optional[str] = None,
        reliability_incident_id: Optional[str] = None,
        node_kind: str = "service",
        node_key: str = "",
        edge_kind: str = "correlates_with",
        actor: str = "system",
    ) -> dict[str, Any]:
        """Link an on-call page (or reliability incident) to a knowledge-graph node."""
        self.ensure_tables(connection)
        if not node_key:
            raise OncallSreError("node_key is required")
        if edge_kind not in ("correlates_with", "caused_by", "depends_on"):
            raise OncallSreError(f"unsupported edge_kind {edge_kind!r}")

        target_id = self.kg.upsert_node(
            connection, project_id, node_kind, node_key,
            name=node_key, source="oncall_sre",
        )

        page = None
        if page_id:
            page = self.get_page(connection, project_id, page_id)
            if not page:
                raise OncallSreError(f"page {page_id} not found", status_code=404)
        elif reliability_incident_id:
            pages = connection.execute(
                text(
                    """
                    SELECT * FROM oncall_pages
                    WHERE project_id = :p AND reliability_incident_id = :inc
                    ORDER BY created_at DESC LIMIT 1
                    """
                ),
                {"p": project_id, "inc": reliability_incident_id},
            ).mappings().first()
            if pages:
                page = _row_to_dict(pages)
                page_id = page["id"]

        if not page_id:
            # Link reliability incident node directly
            if not reliability_incident_id:
                raise OncallSreError("page_id or reliability_incident_id required")
            inc_node_id = self.kg.upsert_node(
                connection, project_id, "incident", reliability_incident_id,
                name=f"incident:{reliability_incident_id}",
                source="oncall_sre", layer="inferred",
            )
            edge_id = self.kg.upsert_edge(
                connection, project_id, inc_node_id, target_id, edge_kind,
                layer="inferred", source="oncall_sre",
            )
            emit_event(
                connection, project_id, "oncall.graph.linked", actor,
                "incident", reliability_incident_id,
                {"node_kind": node_kind, "node_key": node_key, "edge_id": edge_id},
            )
            return {
                "page_id": None,
                "reliability_incident_id": reliability_incident_id,
                "kg_node_id": target_id,
                "kg_node_key": node_key,
                "edge_id": edge_id,
                "edge_kind": edge_kind,
            }

        # Ensure page has an incident KG node
        page_node_id = self.kg.upsert_node(
            connection, project_id, "incident", page_id,
            name=(page or {}).get("title") or page_id,
            source="oncall_sre", layer="inferred",
            attrs={"service": (page or {}).get("service")},
        )
        edge_id = self.kg.upsert_edge(
            connection, project_id, page_node_id, target_id, edge_kind,
            layer="inferred", source="oncall_sre",
        )
        connection.execute(
            text(
                """
                UPDATE oncall_pages
                SET kg_node_id = :nid, kg_node_key = :nk, updated_at = :now
                WHERE id = :id
                """
            ),
            {"nid": target_id, "nk": f"{node_kind}:{node_key}", "now": now_utc(), "id": page_id},
        )
        emit_event(
            connection, project_id, "oncall.graph.linked", actor,
            "oncall_page", page_id,
            {"node_kind": node_kind, "node_key": node_key, "edge_id": edge_id},
        )
        return {
            "page_id": page_id,
            "reliability_incident_id": (page or {}).get("reliability_incident_id"),
            "kg_node_id": target_id,
            "kg_node_key": f"{node_kind}:{node_key}",
            "edge_id": edge_id,
            "edge_kind": edge_kind,
            "page": self.get_page(connection, project_id, page_id),
        }

    def status(self, connection: Connection, project_id: str) -> dict[str, Any]:
        self.ensure_tables(connection)
        open_pages = self.list_pages(connection, project_id, status="open", limit=100)
        acked_pages = self.list_pages(connection, project_id, status="acked", limit=100)
        schedules = self.list_schedules(connection, project_id)
        policies = self.list_policies(connection, project_id)
        oncall = self.current_oncall(connection, project_id)
        adapters = []
        for a in ADAPTERS:
            live, env_name, _ = adapter_credential(a)
            adapters.append({
                "adapter": a,
                "live_capable": live,
                "token_env": env_name,
            })
        return {
            "project_id": project_id,
            "open_pages": len(open_pages),
            "acked_pages": len(acked_pages),
            "pages_open": open_pages,
            "pages_acked": acked_pages,
            "schedules": schedules,
            "policies": policies,
            "current_oncall": oncall,
            "adapters": adapters,
            "as_of": now_utc().isoformat(),
        }

    # ── notify / adapters ──────────────────────────────────────────────────
    def _notify(
        self,
        connection: Connection,
        project_id: str,
        *,
        page_id: str,
        title: str,
        severity: str,
        service: str,
        assignee: str,
        adapter: str,
        dedupe_key: str,
        force_live: bool,
        actor: str,
        extra: Optional[dict] = None,
    ) -> dict[str, Any]:
        tenant = self._tenant(connection, project_id)
        live, env_name, token = adapter_credential(adapter)
        effect_payload = {
            "page_id": page_id,
            "title": title,
            "severity": severity,
            "service": service,
            "adapter": adapter,
            **(extra or {}),
        }
        receipt, created = self.receipts.create_or_reuse(
            connection,
            tenant["org_id"],
            tenant["workspace_id"],
            project_id,
            dedupe_key,
            "oncall_sre.notify",
            {"page_id": page_id, "adapter": adapter, "channel": env_name},
            effect_payload,
        )
        if not created and receipt.get("status") == "succeeded":
            result = receipt.get("result") or {"posted": True, "reused": True}
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except json.JSONDecodeError:
                    result = {"posted": True, "reused": True}
            result["receipt_id"] = receipt["id"]
            result["reused"] = True
            return result

        if not live:
            if force_live:
                err = {
                    "posted": False,
                    "simulated": False,
                    "reason": "credentials_missing",
                    "adapter": adapter,
                    "token_env": env_name,
                }
                self.receipts.mark_failed(connection, receipt["id"], err)
                raise OncallSreError(
                    f"adapter {adapter} has no credentials ({env_name}); refuse force_live",
                    status_code=422,
                    payload={**err, "receipt_id": receipt["id"], "simulated": True},
                )
            # Dry-run: receipt succeeds as simulated delivery
            sim = {
                "posted": False,
                "simulated": True,
                "adapter": adapter,
                "token_env": env_name,
                "assignee": assignee,
                "message": self._format_message(title, severity, service, assignee, page_id),
            }
            self.receipts.mark_succeeded(
                connection, receipt["id"],
                provider_ref=f"simulated:{adapter}",
                result=sim,
            )
            sim["receipt_id"] = receipt["id"]
            return sim

        # Live path
        try:
            posted = self._deliver(adapter, token, title, severity, service, assignee, page_id)
        except Exception as exc:  # noqa: BLE001 — surface as receipt failure
            err = {
                "posted": False,
                "simulated": False,
                "adapter": adapter,
                "error": str(exc)[:500],
            }
            self.receipts.mark_failed(connection, receipt["id"], err)
            return {**err, "receipt_id": receipt["id"]}

        result = {
            "posted": posted,
            "simulated": False,
            "adapter": adapter,
            "assignee": assignee,
        }
        if posted:
            self.receipts.mark_succeeded(
                connection, receipt["id"],
                provider_ref=f"live:{adapter}",
                result=result,
            )
        else:
            self.receipts.mark_failed(connection, receipt["id"], result)
        result["receipt_id"] = receipt["id"]
        return result

    def _format_message(
        self, title: str, severity: str, service: str, assignee: str, page_id: str
    ) -> str:
        emoji = {"critical": "🔴", "warning": "🟠", "info": "🔵"}.get(severity, "⚪")
        parts = [f"{emoji} **[{severity.upper()}] {title}**"]
        if service:
            parts.append(f"service `{service}`")
        if assignee:
            parts.append(f"assignee `{assignee}`")
        parts.append(f"`page {page_id}` · ack/resolve via `/v1/oncall-sre/incidents/{page_id}/…`")
        return " · ".join(parts) if len(parts) > 1 else parts[0]

    def _deliver(
        self,
        adapter: str,
        token: str,
        title: str,
        severity: str,
        service: str,
        assignee: str,
        page_id: str,
    ) -> bool:
        message = self._format_message(title, severity, service, assignee, page_id)
        if adapter in ("slack_webhook", "mattermost", "grafana_oncall"):
            return self._http_post_json(token, {"text": message})
        if adapter == "pagerduty":
            body = {
                "routing_key": token,
                "event_action": "trigger",
                "dedup_key": page_id,
                "payload": {
                    "summary": title[:1024],
                    "severity": "critical" if severity == "critical" else "warning",
                    "source": service or "boss-oncall",
                    "custom_details": {"page_id": page_id, "assignee": assignee},
                },
            }
            return self._http_post_json(
                "https://events.pagerduty.com/v2/enqueue",
                body,
                headers={"Authorization": f"Token token={token}"} if not token.startswith("R") else {},
            )
        if adapter == "opsgenie":
            body = {
                "message": title[:130],
                "priority": "P1" if severity == "critical" else "P3",
                "alias": page_id,
                "details": {"service": service, "assignee": assignee},
            }
            return self._http_post_json(
                "https://api.opsgenie.com/v2/alerts",
                body,
                headers={"Authorization": f"GenieKey {token}"},
            )
        return False

    @staticmethod
    def _http_post_json(
        url: str, body: dict[str, Any], headers: Optional[dict[str, str]] = None
    ) -> bool:
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST", headers=hdrs,
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
            return 200 <= exc.code < 300
        except Exception:
            return False


__all__ = [
    "ADAPTERS",
    "OncallSreError",
    "OncallSreService",
    "adapter_credential",
]
