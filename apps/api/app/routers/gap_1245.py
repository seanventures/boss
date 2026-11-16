"""Named drafts (BOSSAPP-1245).

Open a draft with its own chat and preview. The handler does not write a
commit and does not replace the published URL recorded for the app.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from boss_engine.services.named_draft import (
    NamedDraftError,
    open_named_draft,
    peek_press,
    post_draft_chat,
    press_for,
)

router = APIRouter(prefix="/v1/named-drafts", tags=["named-drafts"])


class OpenDraftBody(BaseModel):
    app_id: str
    name: str
    published_url: str = ""
    commits: list[str] = Field(default_factory=list)


class DraftChatBody(BaseModel):
    app_id: str
    name: str
    text: str = ""


def _error(exc: NamedDraftError) -> JSONResponse:
    return JSONResponse({"error": exc.code, "message": str(exc)}, status_code=422)


@router.post("/open")
def open_draft(body: OpenDraftBody) -> dict[str, Any]:
    try:
        press = press_for(body.app_id, body.published_url, body.commits)
        return open_named_draft(press, body.name).as_dict()
    except NamedDraftError as exc:
        return _error(exc)


@router.post("/chat")
def draft_chat(body: DraftChatBody) -> dict[str, Any]:
    press = peek_press(body.app_id)
    if press is None:
        return _error(NamedDraftError(
            "Open the draft before chatting.", "draft_not_open"))
    try:
        return post_draft_chat(press, body.name, body.text).as_dict()
    except NamedDraftError as exc:
        return _error(exc)
