"""BOSSAPP-1250: ask or block a builder message that contains personal data.

The reconciler calls `before_builder_reserve` at the start of a builder reserve.
This route returns that decision. It does not reserve a budget.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.identity import authenticated
from boss_engine.services.personal_data_gate import before_builder_reserve

router = APIRouter(tags=["personal-data"])


class PersonalDataBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    message: str = Field(default="", max_length=20000)
    allowed: bool = False


@router.post("/v1/builder/personal-data")
@authenticated(note="A builder personal-data check does not reserve a budget.")
def builder_personal_data(payload: PersonalDataBody) -> Any:
    """Classify a builder message. Allow is 200. An ask or a block is a 409.

    `allowed` records that an ask was accepted. This route still does not reserve.
    """
    result = before_builder_reserve(payload.message, allowed=payload.allowed)
    status = 200 if result["action"] == "allow" else 409
    return JSONResponse(status_code=status, content=result)
