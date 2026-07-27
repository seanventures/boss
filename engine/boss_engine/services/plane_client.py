"""Outbound Plane REST client — the BOSS→Plane half of the integration (Phases 2 & 4).

Phase 1 was inbound (Plane webhook → BOSS code task). This is the write-back: move a ticket through
its states, comment the PR link / gate Decision, and close it on merge. All calls are **best-effort
and fail-safe** — a Plane API hiccup must never break BOSS's pipeline, so every method logs and returns
False/None on error rather than raising.

Auth + base URL come from the same config as the inbound service (`PLANE_API_KEY` token,
`PLANE_BASE_URL`/`PLANE_WORKSPACE`, deriving base+workspace from `PLANE_PROJECT_URL` when unset).
"""
from __future__ import annotations

import json
import logging
import re
import ssl
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)


class PlaneClient:
    def __init__(self, settings) -> None:
        self.settings = settings
        self._state_cache: dict[str, dict[str, str]] = {}   # project_id -> {state_name_lower: id}
        self._ctx_cls = ssl._create_unverified_context()

    # --- connection ----------------------------------------------------------
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
        return base, (ws or "default"), (self.settings.plane_api_token or "")

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
