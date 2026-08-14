"""Provider adapter conformance (PRV-10) report API."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from boss_engine.services.provider_conformance import build_provider_conformance_report

router = APIRouter(tags=["provider-conformance"])


@router.get("/v1/provider-conformance")
@router.get("/v1/multi-cloud/provider-conformance")
def provider_conformance(provider_key: Optional[str] = Query(None)):
    try:
        return build_provider_conformance_report(provider_key=provider_key)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

