"""Persistent preview sandboxes (BOSS-11 / Workload B).

Durable control-plane records for long-lived, reservable preview apps that run
on the sandbox host (not as children of boss-api). Fail closed when the host is
unconfigured or unreachable, unless ``simulate=True`` (tests / local demos).

Host contract (sandbox-agent Workload B):
  POST   /v1/sandbox/apps              → create/reserve
  GET    /v1/sandbox/apps/{app_id}     → status
  POST   /v1/sandbox/apps/{app_id}/heartbeat
  DELETE /v1/sandbox/apps/{app_id}     → release

BOSS owns the durable ledger (``preview_sandboxes``); the host owns the process.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ..config import Settings
from .core import emit_event, make_id, now_utc

# Env override used by tests to force simulation without a host.
SIMULATE_ENV = "BOSS_PREVIEW_SANDBOX_SIMULATE"


def _ensure_table(connection: Connection) -> None:
    """Idempotent schema for durable preview sandbox records."""
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS preview_sandboxes (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              app_id TEXT NOT NULL,
              run_id TEXT,
              repo TEXT,
              branch TEXT,
              image_or_build TEXT,
              run_cmd TEXT,
              env_json JSONB NOT NULL DEFAULT '{}'::jsonb,
              port INTEGER,
              status TEXT NOT NULL DEFAULT 'pending',
              host_ref TEXT,
              url TEXT,
              internal_url TEXT,
              error TEXT,
              simulate BOOLEAN NOT NULL DEFAULT false,
              ttl_seconds INTEGER NOT NULL DEFAULT 3600,
              expires_at TIMESTAMPTZ,
              last_heartbeat_at TIMESTAMPTZ,
              reserved_by TEXT,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              UNIQUE (project_id, app_id)
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_preview_sandboxes_project
              ON preview_sandboxes (project_id, created_at DESC)
            """
        )
    )


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _serialize(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key in ("expires_at", "last_heartbeat_at", "created_at", "updated_at"):
        if data.get(key) is not None:
            data[key] = _iso(data[key])
    env = data.pop("env_json", None)
    if env is not None:
        data["env"] = env if isinstance(env, dict) else {}
    return data


class PreviewSandboxService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _host_base(self) -> str:
        return (self.settings.sandbox_agent_url or "").rstrip("/")

    def _host_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        token = self.settings.sandbox_token
        if token:
            headers["X-Sandbox-Token"] = token
        return headers

    def _simulate_enabled(self, simulate: bool) -> bool:
        if simulate:
            return True
        env = (os.environ.get(SIMULATE_ENV) or "").strip().lower()
        return env in {"1", "true", "yes", "on"}

    def _host_request(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
        *,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        base = self._host_base()
        if not base:
            raise RuntimeError(
                "sandbox host not configured (BOSS_SANDBOX_AGENT_URL); "
                "fail closed — pass simulate=true for local/test only"
            )
        url = f"{base}{path}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = Request(url, data=data, method=method, headers=self._host_headers())
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")[:400]
            raise RuntimeError(
                f"sandbox host {method} {path} failed: HTTP {exc.code} {detail}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(
                f"sandbox host unreachable at {base}: {exc.reason}"
            ) from exc

    def reserve(
        self,
        connection: Connection,
        *,
        project_id: str,
        app_id: str,
        actor: str,
        run_id: Optional[str] = None,
        repo: Optional[str] = None,
        branch: Optional[str] = None,
        image_or_build: Optional[str] = None,
        run_cmd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        port: Optional[int] = None,
        ttl_seconds: int = 3600,
        simulate: bool = False,
    ) -> dict[str, Any]:
        """Create or refresh a durable preview sandbox reservation."""
        _ensure_table(connection)
        project_id = (project_id or "").strip()
        app_id = (app_id or "").strip()
        actor = (actor or "").strip()
        if not project_id or not app_id or not actor:
            raise ValueError("project_id, app_id, and actor are required.")
        ttl_seconds = max(60, min(int(ttl_seconds), 7 * 24 * 3600))
        use_sim = self._simulate_enabled(simulate)
        now = now_utc()
        expires = now + timedelta(seconds=ttl_seconds)
        preview_id = make_id("psb")

        host_payload: dict[str, Any] = {}
        status = "running"
        url: Optional[str] = None
        internal_url: Optional[str] = None
        host_ref: Optional[str] = None
        error: Optional[str] = None

        if use_sim:
            host_ref = f"sim:{app_id}"
            url = f"/v1/preview-sandboxes/{preview_id}/web/"
            internal_url = f"http://127.0.0.1:sim/{app_id}"
            host_payload = {
                "simulated": True,
                "app_id": app_id,
                "url": url,
            }
        else:
            try:
                host_payload = self._host_request(
                    "POST",
                    "/v1/sandbox/apps",
                    {
                        "app_id": app_id,
                        "image_or_build": image_or_build or "",
                        "run_cmd": run_cmd or "",
                        "env": env or {},
                        "port": port,
                        "ttl_seconds": ttl_seconds,
                    },
                )
                host_ref = str(
                    host_payload.get("host_ref")
                    or host_payload.get("sandbox_task_id")
                    or host_payload.get("id")
                    or app_id
                )
                url = host_payload.get("url") or host_payload.get("proxy_url")
                internal_url = host_payload.get("internal_url")
                status = str(host_payload.get("status") or "running")
            except RuntimeError as exc:
                # Fail closed: durable failed record + raise so the caller sees 503/422.
                error = str(exc)[:500]
                status = "failed"
                connection.execute(
                    text(
                        """
                        INSERT INTO preview_sandboxes (
                          id, project_id, app_id, run_id, repo, branch,
                          image_or_build, run_cmd, env_json, port, status,
                          host_ref, url, internal_url, error, simulate,
                          ttl_seconds, expires_at, last_heartbeat_at, reserved_by,
                          updated_at
                        ) VALUES (
                          :id, :project_id, :app_id, :run_id, :repo, :branch,
                          :image_or_build, :run_cmd, CAST(:env_json AS JSONB),
                          :port, :status, :host_ref, :url, :internal_url, :error,
                          false, :ttl_seconds, :expires_at, :last_heartbeat_at,
                          :reserved_by, :updated_at
                        )
                        ON CONFLICT (project_id, app_id) DO UPDATE SET
                          status = EXCLUDED.status,
                          error = EXCLUDED.error,
                          updated_at = EXCLUDED.updated_at
                        """
                    ),
                    {
                        "id": preview_id,
                        "project_id": project_id,
                        "app_id": app_id,
                        "run_id": run_id,
                        "repo": repo,
                        "branch": branch,
                        "image_or_build": image_or_build,
                        "run_cmd": run_cmd,
                        "env_json": json.dumps(env or {}),
                        "port": port,
                        "status": status,
                        "host_ref": host_ref,
                        "url": url,
                        "internal_url": internal_url,
                        "error": error,
                        "ttl_seconds": ttl_seconds,
                        "expires_at": expires,
                        "last_heartbeat_at": now,
                        "reserved_by": actor,
                        "updated_at": now,
                    },
                )
                emit_event(
                    connection,
                    project_id,
                    "preview_sandbox.failed",
                    actor,
                    "preview_sandbox",
                    preview_id,
                    {"app_id": app_id, "error": error},
                )
                raise

        # Upsert durable record (unique on project_id + app_id).
        row = connection.execute(
            text(
                """
                INSERT INTO preview_sandboxes (
                  id, project_id, app_id, run_id, repo, branch,
                  image_or_build, run_cmd, env_json, port, status,
                  host_ref, url, internal_url, error, simulate,
                  ttl_seconds, expires_at, last_heartbeat_at, reserved_by,
                  updated_at
                ) VALUES (
                  :id, :project_id, :app_id, :run_id, :repo, :branch,
                  :image_or_build, :run_cmd, CAST(:env_json AS JSONB),
                  :port, :status, :host_ref, :url, :internal_url, :error,
                  :simulate, :ttl_seconds, :expires_at, :last_heartbeat_at,
                  :reserved_by, :updated_at
                )
                ON CONFLICT (project_id, app_id) DO UPDATE SET
                  run_id = COALESCE(EXCLUDED.run_id, preview_sandboxes.run_id),
                  repo = COALESCE(EXCLUDED.repo, preview_sandboxes.repo),
                  branch = COALESCE(EXCLUDED.branch, preview_sandboxes.branch),
                  image_or_build = COALESCE(
                    EXCLUDED.image_or_build, preview_sandboxes.image_or_build
                  ),
                  run_cmd = COALESCE(EXCLUDED.run_cmd, preview_sandboxes.run_cmd),
                  env_json = EXCLUDED.env_json,
                  port = COALESCE(EXCLUDED.port, preview_sandboxes.port),
                  status = EXCLUDED.status,
                  host_ref = COALESCE(EXCLUDED.host_ref, preview_sandboxes.host_ref),
                  url = COALESCE(EXCLUDED.url, preview_sandboxes.url),
                  internal_url = COALESCE(
                    EXCLUDED.internal_url, preview_sandboxes.internal_url
                  ),
                  error = NULL,
                  simulate = EXCLUDED.simulate,
                  ttl_seconds = EXCLUDED.ttl_seconds,
                  expires_at = EXCLUDED.expires_at,
                  last_heartbeat_at = EXCLUDED.last_heartbeat_at,
                  reserved_by = EXCLUDED.reserved_by,
                  updated_at = EXCLUDED.updated_at
                RETURNING *
                """
            ),
            {
                "id": preview_id,
                "project_id": project_id,
                "app_id": app_id,
                "run_id": run_id,
                "repo": repo,
                "branch": branch,
                "image_or_build": image_or_build,
                "run_cmd": run_cmd,
                "env_json": json.dumps(env or {}),
                "port": port,
                "status": status,
                "host_ref": host_ref,
                "url": url,
                "internal_url": internal_url,
                "error": None,
                "simulate": use_sim,
                "ttl_seconds": ttl_seconds,
                "expires_at": expires,
                "last_heartbeat_at": now,
                "reserved_by": actor,
                "updated_at": now,
            },
        ).mappings().one()

        emit_event(
            connection,
            project_id,
            "preview_sandbox.reserved",
            actor,
            "preview_sandbox",
            row["id"],
            {
                "app_id": app_id,
                "status": status,
                "simulate": use_sim,
                "ttl_seconds": ttl_seconds,
                "url": url,
            },
        )
        return _serialize(row)

    def list(
        self,
        connection: Connection,
        project_id: str,
        *,
        limit: int = 50,
        include_expired: bool = False,
    ) -> list[dict[str, Any]]:
        _ensure_table(connection)
        self._reap_expired(connection, project_id)
        limit = max(1, min(200, int(limit)))
        filters = ["project_id = :project_id"]
        if not include_expired:
            filters.append("status NOT IN ('expired', 'released')")
        rows = connection.execute(
            text(
                f"""
                SELECT * FROM preview_sandboxes
                WHERE {' AND '.join(filters)}
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [_serialize(row) for row in rows]

    def get(
        self,
        connection: Connection,
        preview_id: str,
        *,
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        _ensure_table(connection)
        row = connection.execute(
            text("SELECT * FROM preview_sandboxes WHERE id = :id"),
            {"id": preview_id},
        ).mappings().first()
        if row is None:
            raise ValueError("preview sandbox not found")
        if project_id and row["project_id"] != project_id:
            raise ValueError("preview sandbox not found")
        # Soft-expire if past TTL.
        if (
            row["status"] == "running"
            and row.get("expires_at") is not None
            and row["expires_at"] <= now_utc()
        ):
            connection.execute(
                text(
                    """
                    UPDATE preview_sandboxes
                    SET status = 'expired', updated_at = :now
                    WHERE id = :id AND status = 'running'
                    """
                ),
                {"id": preview_id, "now": now_utc()},
            )
            row = connection.execute(
                text("SELECT * FROM preview_sandboxes WHERE id = :id"),
                {"id": preview_id},
            ).mappings().one()
        return _serialize(row)

    def heartbeat(
        self,
        connection: Connection,
        preview_id: str,
        *,
        actor: str,
        ttl_seconds: Optional[int] = None,
    ) -> dict[str, Any]:
        """Extend TTL / signal liveness. Fail closed if record missing or released."""
        _ensure_table(connection)
        actor = (actor or "").strip()
        if not actor:
            raise ValueError("actor is required.")
        row = connection.execute(
            text("SELECT * FROM preview_sandboxes WHERE id = :id FOR UPDATE"),
            {"id": preview_id},
        ).mappings().first()
        if row is None:
            raise ValueError("preview sandbox not found")
        if row["status"] in {"released", "failed"}:
            raise ValueError(
                f"preview sandbox cannot heartbeat in status={row['status']}"
            )

        ttl = int(ttl_seconds) if ttl_seconds is not None else int(row["ttl_seconds"])
        ttl = max(60, min(ttl, 7 * 24 * 3600))
        now = now_utc()
        expires = now + timedelta(seconds=ttl)

        if not row["simulate"]:
            app_id = row["app_id"]
            try:
                self._host_request(
                    "POST",
                    f"/v1/sandbox/apps/{app_id}/heartbeat",
                    {"ttl_seconds": ttl},
                )
            except RuntimeError as exc:
                connection.execute(
                    text(
                        """
                        UPDATE preview_sandboxes
                        SET status = 'failed', error = :error, updated_at = :now
                        WHERE id = :id
                        """
                    ),
                    {"id": preview_id, "error": str(exc)[:500], "now": now},
                )
                raise

        updated = connection.execute(
            text(
                """
                UPDATE preview_sandboxes
                SET status = 'running',
                    ttl_seconds = :ttl,
                    expires_at = :expires,
                    last_heartbeat_at = :now,
                    error = NULL,
                    updated_at = :now
                WHERE id = :id
                RETURNING *
                """
            ),
            {"id": preview_id, "ttl": ttl, "expires": expires, "now": now},
        ).mappings().one()

        emit_event(
            connection,
            updated["project_id"],
            "preview_sandbox.heartbeat",
            actor,
            "preview_sandbox",
            preview_id,
            {"ttl_seconds": ttl, "expires_at": _iso(expires)},
        )
        return _serialize(updated)

    def release(
        self,
        connection: Connection,
        preview_id: str,
        *,
        actor: str,
    ) -> dict[str, Any]:
        _ensure_table(connection)
        actor = (actor or "").strip()
        if not actor:
            raise ValueError("actor is required.")
        row = connection.execute(
            text("SELECT * FROM preview_sandboxes WHERE id = :id FOR UPDATE"),
            {"id": preview_id},
        ).mappings().first()
        if row is None:
            raise ValueError("preview sandbox not found")
        if row["status"] == "released":
            return _serialize(row)

        if not row["simulate"]:
            try:
                self._host_request(
                    "DELETE", f"/v1/sandbox/apps/{row['app_id']}"
                )
            except RuntimeError:
                # Best-effort host teardown; still mark released locally.
                pass

        now = now_utc()
        updated = connection.execute(
            text(
                """
                UPDATE preview_sandboxes
                SET status = 'released', updated_at = :now, expires_at = :now
                WHERE id = :id
                RETURNING *
                """
            ),
            {"id": preview_id, "now": now},
        ).mappings().one()
        emit_event(
            connection,
            updated["project_id"],
            "preview_sandbox.released",
            actor,
            "preview_sandbox",
            preview_id,
            {"app_id": updated["app_id"]},
        )
        return _serialize(updated)

    def _reap_expired(self, connection: Connection, project_id: str) -> int:
        result = connection.execute(
            text(
                """
                UPDATE preview_sandboxes
                SET status = 'expired', updated_at = :now
                WHERE project_id = :project_id
                  AND status = 'running'
                  AND expires_at IS NOT NULL
                  AND expires_at < :now
                """
            ),
            {"project_id": project_id, "now": now_utc()},
        )
        return int(result.rowcount or 0)


def ensure_preview_sandbox_schema(engine: Engine) -> None:
    """Apply schema outside a request (boot / tests)."""
    from ..db import begin as _begin

    with _begin(engine) as connection:
        _ensure_table(connection)
