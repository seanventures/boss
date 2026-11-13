"""The personal attention inbox over HTTP (BOSSAPP-875; services/inbox.py).

`GET /v1/inbox?mine=1` is the signed-in person's ranked feed: decisions waiting on them, work
they answer for, failed runs of that work — first what blocks the most. Read state, snoozes and
archives are that person's own, so marking them needs only to be able to read the project
(work_item:read); the person is always the signed-in one, never a claimed actor.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


class ReadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    item_keys: list[str] = Field(min_length=1, max_length=500)
    read: bool = True


class SnoozeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    item_key: str = Field(min_length=3, max_length=300)
    minutes: Optional[int] = Field(default=None, ge=1, le=90 * 24 * 60)
    archive: bool = False
    clear: bool = False


def build_inbox_router(*, engine, require_reader: Callable[..., None],
                       viewer: Callable[[], str],
                       viewing: Optional[Callable[[Any], None]] = None) -> APIRouter:
    from boss_engine.services import inbox as svc

    router = APIRouter(tags=["inbox"])

    def _me() -> str:
        who = str(viewer() or "").strip()
        if not who:
            raise HTTPException(status_code=401, detail="the inbox is a signed-in person's")
        return who

    def _run(project_id: str, fn: Callable[[Any, str], Any]) -> Any:
        who = _me()
        with begin(engine) as c:
            require_reader(c, project_id)
            if viewing is not None:
                viewing(c)
            try:
                return fn(c, who)
            except svc.InboxError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/v1/inbox")
    @permit(P.WORK_ITEM_READ, scope=Scope.project(query="project_id"))
    def read_inbox(project_id: str, mine: bool = True, include_snoozed: bool = False,
                   include_archived: bool = False, kind: Optional[str] = None,
                   limit: int = Query(100, ge=1, le=500)) -> dict:
        kinds = [k.strip() for k in kind.split(",")] if kind else None
        return _run(project_id, lambda c, who: svc.inbox(
            c, project_id, actor=who, mine=mine, include_snoozed=include_snoozed,
            include_archived=include_archived, kinds=kinds, limit=limit))

    @router.post("/v1/inbox/read")
    @permit(P.WORK_ITEM_READ, scope=Scope.project(body="project_id"))
    def mark_read(payload: ReadBody) -> dict:
        return _run(payload.project_id, lambda c, who: svc.mark_read(
            c, payload.project_id, actor=who, item_keys=payload.item_keys, read=payload.read))

    @router.post("/v1/inbox/snooze")
    @permit(P.WORK_ITEM_READ, scope=Scope.project(body="project_id"))
    def snooze(payload: SnoozeBody) -> dict:
        return _run(payload.project_id, lambda c, who: svc.snooze(
            c, payload.project_id, actor=who, item_key=payload.item_key,
            minutes=payload.minutes, archive=payload.archive, clear=payload.clear))

    return router
