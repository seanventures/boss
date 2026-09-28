"""Outbound Plane REST client — the BOSS→Plane half of the integration (Phases 2 & 4).

Phase 1 was inbound (Plane webhook → BOSS code task). This is the write-back: move a ticket through
its states, comment the PR link / gate Decision, and close it on merge. All calls are **best-effort
and fail-safe** — a Plane API hiccup must never break BOSS's pipeline, so every method logs and returns
False/None on error rather than raising.

Auth + base URL come from the same config as the inbound service (`PLANE_API_KEY` token,
`PLANE_BASE_URL`/`PLANE_WORKSPACE`, deriving base+workspace from `PLANE_PROJECT_URL` when unset).

Token resolution is SecretRef-first: if ``plane_api_secret_ref_id`` is set, or the configured
key looks like ``sref_...``, the value comes from ``resolve_secret_ref``. Raw
``settings.plane_api_token`` / ``PLANE_API_KEY`` is last-resort only when no ref is bound.
The token is never logged.
"""
from __future__ import annotations

import json
import logging
import os
import re
import ssl
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)


def plane_ssl_context(settings) -> ssl.SSLContext:
    """Build the shared Plane REST TLS policy for writes and webhook label reads."""
    if getattr(settings, "plane_tls_insecure_skip_verify", False):
        logger.warning("Plane TLS certificate verification explicitly disabled by operator")
        return ssl._create_unverified_context()
    ca_file = str(getattr(settings, "plane_tls_ca_file", None) or "").strip()
    return ssl.create_default_context(cafile=ca_file or None)


def _looks_like_secret_ref(value: object) -> bool:
    return str(value or "").strip().startswith("sref_")


class PlaneClient:
    def __init__(self, settings) -> None:
        self.settings = settings
        self._state_cache: dict[str, dict[str, str]] = {}   # project_id -> {state_name_lower: id}
        self._ctx_cls = plane_ssl_context(settings)
        self._connection = None

    # --- connection ----------------------------------------------------------
    def _secret_ref_id(self) -> str:
        """Bound SecretRef id, or empty when the client should use last-resort env/token."""
        explicit = str(getattr(self.settings, "plane_api_secret_ref_id", None) or "").strip()
        if explicit:
            return explicit
        for candidate in (
            getattr(self.settings, "plane_api_key", None),
            getattr(self.settings, "plane_api_token", None),
            os.environ.get("PLANE_API_KEY"),
            os.environ.get("PLANE_API_TOKEN"),
        ):
            value = str(candidate or "").strip()
            if _looks_like_secret_ref(value):
                return value
        return ""

    def _resolve_secret_ref(self, secret_ref_id: str) -> str:
        from .secret_backends import resolve_secret_ref

        conn = self._connection
        if conn is not None:
            return resolve_secret_ref(conn, secret_ref_id, self.settings)
        from ..db import get_engine

        with get_engine(self.settings).connect() as opened:
            return resolve_secret_ref(opened, secret_ref_id, self.settings)

    def _token(self) -> str:
        """Outbound Plane API token. SecretRef first; env/token only if no ref is bound."""
        ref_id = self._secret_ref_id()
        if ref_id:
            try:
                return str(self._resolve_secret_ref(ref_id) or "")
            except Exception as exc:  # noqa: BLE001 — outbound Plane calls are best-effort
                logger.warning("plane_client: secret ref resolution failed: %s", exc)
                return ""
        token = str(getattr(self.settings, "plane_api_token", None) or "").strip()
        if token:
            return token
        return str(
            os.environ.get("PLANE_API_KEY") or os.environ.get("PLANE_API_TOKEN") or ""
        ).strip()

    def _ctx(self) -> tuple[str, str, str]:
        """(base_url, workspace, token) — mirrors PlaneWebhookService._api_ctx."""
        base = (self.settings.plane_base_url or "").rstrip("/")
        ws = getattr(self.settings, "plane_workspace", "") or ""
        proj_url = self.settings.plane_project_url or ""
        if (not base or not ws) and proj_url:
            m = re.match(r"(https?://[^/]+)/([^/]+)/", proj_url)
            if m:
                base = base or m.group(1)
                ws = ws or m.group(2)
        return base, (ws or "default"), self._token()

    def enabled(self) -> bool:
        base, _ws, token = self._ctx()
        return bool(base and token)

    def _request(self, method: str, project_id: str, path: str,
                 body: Optional[dict] = None) -> Optional[Any]:
        base, ws, token = self._ctx()
        if not (base and token and project_id):
            return None
        url = f"{base}/api/v1/workspaces/{ws}/projects/{project_id}/{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"X-API-Key": token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10, context=self._ctx_cls) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw.strip() else {"__ok__": r.status}
        except Exception as exc:  # noqa: BLE001 — outbound Plane calls are best-effort
            logger.warning("plane_client %s %s failed: %s", method, path, exc)
            return None

    # --- states --------------------------------------------------------------
    def _states(self, project_id: str, *, force: bool = False) -> dict[str, str]:
        """{state_name_lower: id} for a project, cached."""
        if not force and project_id in self._state_cache:
            return self._state_cache[project_id]
        data = self._request("GET", project_id, "states/")
        rows = (data.get("results", data) if isinstance(data, dict) else data) or []
        m = {str(x["name"]).lower(): str(x["id"]) for x in rows
             if isinstance(x, dict) and "name" in x and "id" in x}
        if m:
            self._state_cache[project_id] = m
        return m

    def set_state(self, project_id: str, issue_id: str, state_name: str) -> bool:
        """Move an issue to a named state (e.g. 'In Progress', 'Done'). No-op if already there isn't
        cheaply knowable, so it always PATCHes; a redundant PATCH is harmless."""
        state_id = self._states(project_id).get((state_name or "").lower())
        if not state_id:                        # a new/renamed state -> refetch once
            state_id = self._states(project_id, force=True).get((state_name or "").lower())
        if not state_id:
            logger.warning("plane_client: no state %r in project %s", state_name, project_id)
            return False
        return self._request("PATCH", project_id, f"issues/{issue_id}/", {"state": state_id}) is not None

    def create_issue(
        self,
        project_id: str,
        *,
        name: str,
        description_html: str,
    ) -> Optional[dict[str, Any]]:
        """Create a Plane issue and return its canonical provider payload.

        The caller owns durable retry and receipts. This low-level adapter stays
        best-effort so a provider outage is reported as ``None`` rather than
        escaping the policy-guarded worker boundary.
        """
        result = self._request(
            "POST",
            project_id,
            "issues/",
            {
                "name": name,
                "description_html": description_html,
            },
        )
        return result if isinstance(result, dict) and result.get("id") else None

    def update_issue(
        self,
        project_id: str,
        issue_id: str,
        changes: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        result = self._request(
            "PATCH",
            project_id,
            f"issues/{issue_id}/",
            changes,
        )
        return result if isinstance(result, dict) else None

    def add_comment(self, project_id: str, issue_id: str, comment_html: str) -> bool:
        return self._request("POST", project_id, f"issues/{issue_id}/comments/",
                             {"comment_html": comment_html}) is not None
