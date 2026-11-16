"""Preview frame and fake-store upload. Not a device farm. Not a real store."""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from boss_engine.services.store_upload import (
    MemoryReceiptBook,
    StoreUploadError,
    UploadRefused,
    preview_frame,
    upload_signed_build,
)

router = APIRouter(tags=["emulator-frame"])

BOOK = MemoryReceiptBook()


def store_url() -> str:
    """BOSS_FAKE_STORE_URL is a local stand-in. Empty means no upload target."""
    return os.environ.get("BOSS_FAKE_STORE_URL", "").strip()


class StoreUploadIn(BaseModel):
    platform: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    signing_key: str = ""


@router.get("/v1/builder/emulator-frame")
def read_emulator_frame(platform: str = "ios", index: int = 1) -> dict:
    """One local preview frame. Nothing here opens a simulator."""
    try:
        return preview_frame(platform, index)
    except UploadRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/v1/builder/store-uploads", status_code=201)
def create_store_upload(body: StoreUploadIn) -> dict:
    try:
        return upload_signed_build(
            platform=body.platform,
            artifact_id=body.artifact_id,
            signing_key=body.signing_key,
            store_url=store_url(),
            book=BOOK,
        )
    except UploadRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StoreUploadError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
