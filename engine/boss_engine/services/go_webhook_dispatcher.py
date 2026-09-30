"""Optional enqueue-only client for the BOSS-102 Go webhook dispatcher.

The Python event ledger stays canonical. This module never POSTs to destination
webhook URLs. When the sidecar URL is unset the engine continues on the
existing Python `webhook.outbound.deliver` path and reports `not_configured`.
"""

from __future__ import annotations

from typing import Any, Callable, Optional
from urllib.parse import urljoin

import httpx

from ..config import Settings

DOC = "docs/operations/GO_SCALE_TRACK.md"
NOT_CONFIGURED_MESSAGE = (
    "Go webhook dispatcher is unset; Python event SoR continues."
)


def _redact(value: Any) -> str:
    text = str(value or "")
    lowered = text.lower()
    if "sha256=" in lowered or text.startswith("env:") or "secret" in lowered:
        return "[REDACTED]"
    return text


class GoWebhookDispatcherClient:
    """Enqueue-only adapter. Destination fan-out lives in the Go sidecar."""

    def __init__(
        self,
        settings: Settings,
        post: Optional[Callable[..., httpx.Response]] = None,
    ) -> None:
        self.settings = settings
        self._post = post or httpx.post

    @property
    def base_url(self) -> str:
        return (self.settings.go_webhook_dispatcher_url or "").strip().rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def status(self) -> dict[str, Any]:
        if not self.configured:
            return {
                "status": "not_configured",
                "configured": False,
                "enqueued": False,
                "go_runtime_shipped": False,
                "s3_go_event_dispatcher": False,
                "community_ga_required": False,
                "dry_run": True,
                "doc": DOC,
                "message": NOT_CONFIGURED_MESSAGE,
            }
        return {
            "status": "configured",
            "configured": True,
            "enqueued": False,
            "go_runtime_shipped": False,
            "s3_go_event_dispatcher": False,
            "community_ga_required": False,
            "dry_run": bool(self.settings.go_webhook_dispatcher_dry_run),
            "doc": DOC,
            "base_url": self.base_url,
            "message": (
                "Go dispatcher URL is set; this client only enqueues envelopes. "
                "Live scale is not claimed (no GA, no live probe)."
            ),
        }

    def enqueue(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """POST the envelope to the sidecar. Never calls destination URLs."""
        if not self.configured:
            return {
                "status": "not_configured",
                "configured": False,
                "enqueued": False,
                "go_runtime_shipped": False,
                "s3_go_event_dispatcher": False,
                "message": NOT_CONFIGURED_MESSAGE,
                "doc": DOC,
            }

        payload = dict(envelope or {})
        if self.settings.go_webhook_dispatcher_dry_run:
            payload["dry_run"] = True
        url = urljoin(self.base_url + "/", "v1/dispatch")
        headers = {"Content-Type": "application/json"}
        token = (self.settings.go_webhook_dispatcher_token or "").strip()
        if token:
            headers["X-Boss-Dispatcher-Token"] = token
        try:
            response = self._post(
                url,
                json=payload,
                headers=headers,
                timeout=self.settings.go_webhook_dispatcher_timeout_seconds,
            )
        except (httpx.HTTPError, ValueError, OSError) as error:
            return {
                "status": "failed",
                "configured": True,
                "enqueued": False,
                "go_runtime_shipped": False,
                "s3_go_event_dispatcher": False,
                "message": _redact(error),
                "error_type": error.__class__.__name__,
                "doc": DOC,
            }
        if response.status_code < 200 or response.status_code >= 300:
            return {
                "status": "failed",
                "configured": True,
                "enqueued": False,
                "go_runtime_shipped": False,
                "s3_go_event_dispatcher": False,
                "http_status": response.status_code,
                "message": _redact(response.text[:500]),
                "doc": DOC,
            }
        body: Any
        try:
            body = response.json()
        except ValueError:
            body = {}
        return {
            "status": "enqueued",
            "configured": True,
            "enqueued": True,
            "go_runtime_shipped": False,
            "s3_go_event_dispatcher": False,
            "dry_run": bool(payload.get("dry_run")),
            "result": body if isinstance(body, dict) else {},
            "doc": DOC,
        }
