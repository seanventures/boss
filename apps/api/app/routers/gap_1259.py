"""Tenant handoff from a publish (BOSSAPP-1259).

Place records the customer tenant locally. Handover returns the image reference,
a Terraform stub, and an SBOM only when one was already recorded. No cloud call.
"""
from __future__ import annotations

from typing import Any, Optional

from boss_engine.identity import authenticated
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from boss_engine.services.tenant_handoff import TenantHandoffError, handoff_from_publish

router = APIRouter(tags=["tenant-handoff"])


class HandoffBody(BaseModel):
    app: str = ""
    image_reference: str = ""
    mode: str = ""
    tenant: Optional[str] = None
    recorded_sbom: Any = None
    publish_id: Optional[str] = None


@router.post("/v1/tenant-handoff")
@authenticated(note="Hands a published app to a tenant or returns its papers. No cloud call.")
def tenant_handoff(body: HandoffBody) -> dict[str, Any]:
    try:
        return handoff_from_publish(
            {
                "app": body.app,
                "image_reference": body.image_reference,
                "publish_id": body.publish_id,
                "recorded_sbom": body.recorded_sbom,
            },
            mode=body.mode,
            tenant=body.tenant,
        )
    except TenantHandoffError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
