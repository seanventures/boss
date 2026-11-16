"""Hand a published app to a customer tenant, or hand over its papers (BOSSAPP-1259).

Publish already recorded the image reference and, sometimes, an SBOM. This step
does not call a cloud and does not invent an SBOM. A missing SBOM is said out
loud; the image reference and the Terraform stub still come back.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Mapping, Optional

class TenantHandoffError(ValueError):
    """A handoff that cannot be made. The message is the sentence to show."""


def handoff_from_publish(
    publish: Mapping[str, Any],
    *,
    mode: str,
    tenant: Optional[str] = None,
    ledger: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Place `publish` in a tenant, or hand over its image, stub, and recorded SBOM.

    `ledger`, when passed, keeps one row per handoff. The same request returns
    the row already stored.
    """
    mode_text = str(mode or "").strip()
    if mode_text not in ("place", "handover"):
        raise TenantHandoffError("Mode must be place or handover.")
    app = _text(publish.get("app") if isinstance(publish, Mapping) else None)
    if not app:
        raise TenantHandoffError("Name the app.")
    image = _text(publish.get("image_reference") if isinstance(publish, Mapping) else None)
    if not image:
        raise TenantHandoffError("This publish has no image reference.")
    tenant_text: Optional[str] = None
    if mode_text == "place":
        tenant_text = _text(tenant)
        if not tenant_text:
            raise TenantHandoffError("Name the tenant.")
    sbom = _recorded_sbom(publish if isinstance(publish, Mapping) else {})
    publish_id = _text(publish.get("publish_id") if isinstance(publish, Mapping) else None) or None
    result = {
        "ok": True,
        "mode": mode_text,
        "app": app,
        "image_reference": image,
        "terraform_stub": _stub(app, image, tenant_text),
        "sbom": sbom,
        "sbom_recorded": sbom is not None,
        "sbom_note": "SBOM on file." if sbom is not None else "No SBOM is recorded.",
        "tenant": tenant_text,
        "placed": mode_text == "place",
        "cloud_called": False,
        "publish_id": publish_id,
        "dedupe_key": _dedupe(app, image, mode_text, tenant_text, publish_id, sbom),
    }
    if ledger is not None:
        for prior in ledger:
            if prior.get("dedupe_key") == result["dedupe_key"]:
                return prior
        ledger.append(result)
    return result


def _text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _recorded_sbom(publish: Mapping[str, Any]) -> Any:
    """The SBOM already on the publish, or None. Never a document built here."""
    if "recorded_sbom" in publish:
        raw = publish.get("recorded_sbom")
    elif "sbom" in publish:
        raw = publish.get("sbom")
    else:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        return text or None
    if isinstance(raw, dict):
        return copy.deepcopy(raw) if raw else None
    if isinstance(raw, list):
        return copy.deepcopy(raw) if raw else None
    return None


def _dedupe(app: str, image: str, mode: str, tenant: Optional[str],
            publish_id: Optional[str], sbom: Any) -> str:
    raw = json.dumps(
        {"app": app, "image_reference": image, "mode": mode, "publish_id": publish_id,
         "sbom": sbom, "tenant": tenant},
        sort_keys=True, separators=(",", ":"), default=str)
    return "tenant-handoff:" + hashlib.sha256(raw.encode()).hexdigest()


def _label(app: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_]", "_", app.lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = "app_" + cleaned
    return (cleaned or "app")[:48]


def _hcl(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _stub(app: str, image: str, tenant: Optional[str]) -> str:
    lines = [
        "# Tenant handoff stub. Not applied. No cloud was called.",
        f'resource "boss_tenant_handoff" "{_label(app)}" {{',
        f"  app   = {_hcl(app)}",
        f"  image = {_hcl(image)}",
    ]
    if tenant is not None:
        lines.append(f"  tenant = {_hcl(tenant)}")
    lines.append("}")
    return "\n".join(lines) + "\n"
