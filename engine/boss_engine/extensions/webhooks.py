"""webhooks.receive: POST /v1/ext/{extension_id}/webhook/{hook}, verified BOSS-side, then the broker.

PER-HOOK SECRET: HMAC-SHA256(server key, extension ‖ hook ‖ install nonce) — derived, never
stored, different for every hook, and rotated by reinstalling. The sender signs the RAW body with
it and sends `X-Boss-Signature: sha256=<hex>` (the GitHub shape). BOSS checks the signature in
constant time BEFORE anything reaches the extension: a missing or wrong signature is a 401 and the
child never starts. The extension receives the raw body and the headers (minus credentials and the
signature) and answers {"status": 2xx-5xx, "body": <json>}.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any, Mapping, Optional

from .registry import ExtensionRefused, InstalledExtension

SIGNATURE_HEADER = "x-boss-signature"
_DROPPED_HEADERS = {"authorization", "cookie", "proxy-authorization", SIGNATURE_HEADER,
                    "x-api-key", "x-boss-api-key"}


def hook_secret(ext: InstalledExtension, hook: str) -> str:
    from ..identity.authn_support import keyed_hash

    return "whsec_" + keyed_hash("boss.ext.webhook", f"{ext.extension_id}:{hook}:{ext.nonce}")[:48]


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify(ext: InstalledExtension, hook: str, body: bytes, headers: Mapping[str, str]) -> bool:
    given = ""
    for k, v in headers.items():
        if k.lower() == SIGNATURE_HEADER:
            given = str(v or "").strip()
    if not given:
        return False
    return hmac.compare_digest(given, sign(hook_secret(ext, hook), body))


def receive(engine: Any, ext: Optional[InstalledExtension], hook: str, body: bytes,
            headers: Mapping[str, str]) -> tuple[int, Any]:
    """(status, body) for the route. Every refusal happens before the child starts."""
    from . import broker

    if ext is None:
        return 404, {"error": "extension_unknown"}
    try:
        manifest = ext.manifest                         # the trust gate
    except ExtensionRefused as exc:
        return exc.status, {"error": exc.code, "detail": str(exc)}
    spec = manifest.webhook(hook) if manifest.has("webhooks.receive") else None
    if spec is None:
        return 404, {"error": "webhook_unknown"}
    if not ext.enabled_anywhere:
        return 409, {"error": "extension_disabled", "detail": "enable the extension in a project first"}
    if len(body) > spec.maxBodyBytes:
        return 413, {"error": "body_too_large"}
    if not verify(ext, hook, body, headers):
        return 401, {"error": "bad_signature",
                     "detail": f"sign the raw body with this hook's secret: {SIGNATURE_HEADER.title()}: sha256=<hex>"}
    passed = {k.lower(): str(v)[:1000] for k, v in headers.items()
              if k.lower() not in _DROPPED_HEADERS}
    outcome = broker.call(engine, ext, "webhook.receive",
                          {"hook": hook, "body_b64": base64.b64encode(body).decode("ascii"),
                           "headers": passed})
    if not outcome.ok:
        return (504 if outcome.code == "timeout" else 502), {"error": outcome.code,
                                                             "detail": outcome.error}
    res = outcome.result if isinstance(outcome.result, dict) else {"body": outcome.result}
    status = res.get("status", 200)
    status = status if isinstance(status, int) and 200 <= status <= 599 else 200
    return status, res.get("body", {})
