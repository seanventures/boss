"""The mechanism that protects each `@public` route, by name (IDENTITY.md §6.3, amendment A11).

A public route skips authentication, so it must name what authenticates the request instead. These
are references to the functions that do that check today (each one runs inside its handler or the
service it calls); `tests/identity/public_routes.yaml` lists every public route with its reason and
verifier, and the coverage test holds the two in step. Stream S2.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text

from boss_engine.db import begin
from boss_engine.services import (
    channel_oauth,
    debug_session,
    feedback_agent,
    improve_collector,
    mcp_oauth,
    preview_service,
)
from boss_engine.services.plane_webhooks import PlaneWebhookService
from boss_engine.services.reliability import ReliabilityService
from boss_engine.services.saml import SamlAuthenticator
from boss_engine.services.webhooks import InboundWebhookService

#: GitHub, Bitbucket and generic inbound webhooks: the per-source HMAC check in ingest().
inbound_webhook_hmac = InboundWebhookService.ingest
#: Plane: X-Plane-Signature HMAC-SHA256 over the raw body.
plane_webhook_hmac = PlaneWebhookService.verify_signature
#: Alertmanager / Grafana alerts: the configured alert HMAC.
reliability_alert_hmac = ReliabilityService.verify_alert_signature
#: SAML ACS (signed assertion) and the SP metadata it is verified against.
saml_assertion = SamlAuthenticator.verify_response
#: Channel OAuth callbacks: a single-use state nonce BOSS minted on a pending connection.
channel_oauth_state = channel_oauth.complete
#: MCP server sign-in callbacks (BOSSAPP-846): a single-use state BOSS minted, kept only as its hash.
mcp_oauth_state = mcp_oauth.complete
#: Investigation callbacks (db / host / do): the per-investigation bearer token.
investigation_callback_token = debug_session.authorise
#: The preview reverse proxy: an unguessable preview id, live only while the preview runs (TTL).
preview_capability = preview_service.proxy_preview
#: Feedback posted from inside a running preview (the same capability).
preview_feedback_capability = feedback_agent.submit_preview_feedback
#: The Improve-BOSS collector's machine routes: 404 unless collector mode is on; bounded and
#: validated input; delete verifies the install's own signature.
collector_ingest = improve_collector.ingest
collector_delete = improve_collector.delete
collector_share = improve_collector.share


def preview_running(engine: Any, preview_id: str) -> Optional[dict[str, Any]]:
    """The preview WebSocket capability: {url, mode} of a RUNNING preview with this id, else None."""
    with begin(engine) as connection:
        row = connection.execute(text("SELECT url, status, mode FROM app_previews WHERE id = :id"),
                                 {"id": preview_id}).mappings().first()
    if row is None or row["status"] != "running" or not row["url"]:
        return None
    return {"url": str(row["url"]), "mode": row["mode"]}


def require_in_project(connection: Any, table: str, object_id: Optional[str], project_id: str) -> None:
    """IDENTITY (S2): an object a request names alongside a project must belong to that project,
    or the request is answered 404 — so a permission held in one project never reaches another
    project's object (or its credentials). `table` is a module constant, never request data."""
    if not object_id:
        return
    if table not in _CONFINABLE:
        raise ValueError(f"not a confinable table: {table}")
    found = connection.execute(text(f"SELECT 1 FROM {table} WHERE id = :i AND project_id = :p"),
                               {"i": object_id, "p": project_id}).first()
    if found is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="not_found")


_CONFINABLE = frozenset({"deployment_targets", "agent_threads", "decisions", "code_task_runs",
                         "shepherd_escalations"})
