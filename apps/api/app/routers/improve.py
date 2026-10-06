"""Run scorecards, experiments, person feedback and "Improve BOSS" (client + collector).

docs/product/RUN_SCORECARDS.md, "Implementation" → "Routes". Mounted from main.py with the auth
helpers injected, so this module does not import main at import time.

THREE ROUTES ARE MACHINE ROUTES — `POST /v1/telemetry/ingest`, `/delete`, `/share` — exempt from
transport auth in main.require_transport_auth_for_v1 (like the signed webhooks): an install has
no account here. Each answers 404 unless Collector mode is on, and validates and bounds what it
receives (services/improve_collector.py). Every other route is a normal project route.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from boss_engine.db import begin

COLLECTOR_PATHS = frozenset({"/v1/telemetry/ingest", "/v1/telemetry/delete",
                             "/v1/telemetry/share"})


class Body(BaseModel):
    project_id: str
    actor: Optional[str] = None


class SharingBody(Body):
    switch: Optional[str] = None
    on: Optional[bool] = None
    weights: Optional[dict[str, float]] = None
    #: The collector's ingest URL ("" clears it). Refused while BOSS_TELEMETRY_URL sets it.
    url: Optional[str] = None
    #: Words a shared run always masks (list, or one comma/line separated string).
    mask_words: Optional[Any] = None


class ExperimentBody(Body):
    name: Optional[str] = None
    target_kind: str
    target_key: str
    knob: str
    arm_a: Any
    arm_b: Any
    split_b: float = 0.5
    target_units: int = 20
    arm_cost_cap_usd: float = 25.0


class PromoteBody(Body):
    arm: str
    #: The operator's tick: promote although the evidence is short (defect 3).
    anyway: bool = False


class FeedbackBody(Body):
    subject_kind: str
    subject_id: str
    rating: Optional[str] = None
    chips: Optional[list[str]] = None
    decision_kind: Optional[str] = None
    #: subject_kind "edit": how much a brief/PRD/plan changed before approval, 0..1 — the number
    #: alone; the text never reaches this route.
    edit_ratio: Optional[float] = None


class ShownBody(Body):
    prompt: str = "rate"


class CheckinBody(Body):
    answer: Optional[str] = None
    line: Optional[str] = None
    share_line: bool = False
    dismiss: bool = False


class ShareRunBody(Body):
    bundle: str
    note: Optional[str] = None


def client_ip(peer: str, forwarded: str, trusted: str) -> str:
    """The address a rate limit counts (hashed, held an hour in memory, never stored). The REAL
    peer, unless the peer is a trusted proxy: then the right-most X-Forwarded-For hop that is not
    itself a trusted proxy. A client cannot choose its bucket by sending X-Forwarded-For — a hop
    it wrote sits LEFT of the one its first proxy appended."""
    import ipaddress

    nets = []
    for cidr in (trusted or "").split(","):
        try:
            nets.append(ipaddress.ip_network(cidr.strip(), strict=False))
        except ValueError:
            continue

    def is_trusted(addr: str) -> bool:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        return any(ip in n for n in nets)

    def valid(addr: str) -> bool:
        try:
            ipaddress.ip_address(addr)
            return True
        except ValueError:
            return False

    if not is_trusted(peer or ""):
        return peer or ""
    # A malformed hop ("unknown", a host name, garbage) is skipped, never used as a bucket.
    hops = [h.strip() for h in (forwarded or "").split(",") if valid(h.strip())]
    for hop in reversed(hops):
        if not is_trusted(hop):
            return hop
    return hops[0] if hops else (peer or "")


async def read_capped(request: Request, cap: int) -> bytes:
    """The body, streamed and refused the moment it passes `cap` — never read whole first. A
    150 MB chunked POST costs at most `cap` plus one chunk of memory."""
    from boss_engine.services.improve_collector import CollectorError

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        raise CollectorError(413, "too_large")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > cap:
            raise CollectorError(413, "too_large")
    return bytes(buf)


class _NoAddressForTelemetry:
    """P5: the API's access log names no client address for the collector's routes. Uvicorn's
    access record is (client, method, path, http version, status); the client becomes "-"."""

    def filter(self, record) -> bool:  # logging.Filter protocol
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and str(args[2]).startswith("/v1/telemetry/"):
            record.args = ("-",) + tuple(args[1:])
        return True


def install_access_log_filter() -> None:
    import logging

    log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _NoAddressForTelemetry) for f in log.filters):
        log.addFilter(_NoAddressForTelemetry())


def build_improve_router(
    *,
    engine,
    settings: Callable[[], Any],
    require_write_actor: Callable[..., str],
    require_reader: Callable[..., None],
    actor_of: Callable[..., str],
    require_worker: Callable[[], None],
) -> APIRouter:
    from boss_engine.services import (experiments, improve_boss, improve_collector,
                                      person_feedback, scorecards)

    router = APIRouter(tags=["improve"])
    install_access_log_filter()

    def _reader(project_id: str) -> str:
        pid = (project_id or "").strip()
        if not pid:
            raise HTTPException(status_code=400, detail="project_id is required")
        return pid

    # -------------------------------------------------------------- scorecards
    @router.get("/v1/scorecards")
    def get_scorecards(project_id: str, group: str = "role", win: str = "30d",
                       kind: str = "build") -> dict:
        pid = _reader(project_id)
        with begin(engine) as c:
            require_reader(c, pid)
            try:
                return scorecards.view(c, pid, group=group, win=win, kind=kind)
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error

    @router.post("/v1/scorecards/backfill")
    def backfill_scorecards(payload: Body) -> dict:
        """Build scorecards for history now (the tick also does it, a batch an hour)."""
        pid = _reader(payload.project_id)
        totals: dict[str, int] = {}
        for _ in range(10):
            with begin(engine) as c:
                require_write_actor(c, pid, payload.actor, "workflow_manage")
                done = scorecards.sweep(c)
            for k, v in done.items():
                totals[k] = totals.get(k, 0) + v
            if not any(done.values()):
                break
        return {"recorded": totals}

    # -------------------------------------------------------------- experiments
    @router.get("/v1/experiments")
    def list_experiments(project_id: str) -> dict:
        pid = _reader(project_id)
        with begin(engine) as c:
            require_reader(c, pid)
            return experiments.list_all(c, pid)

    @router.post("/v1/experiments", status_code=201)
    def create_experiment(payload: ExperimentBody) -> dict:
        pid = _reader(payload.project_id)
        with begin(engine) as c:
            actor = require_write_actor(c, pid, payload.actor, "workflow_manage")
            try:
                return experiments.create(
                    c, project_id=pid, actor=actor, name=payload.name or "",
                    target_kind=payload.target_kind, target_key=payload.target_key,
                    knob=payload.knob, arm_a=payload.arm_a, arm_b=payload.arm_b,
                    split_b=payload.split_b, target_units=payload.target_units,
                    arm_cost_cap_usd=payload.arm_cost_cap_usd)
            except experiments.ExperimentError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error

    @router.post("/v1/experiments/{experiment_id}/stop")
    def stop_experiment(experiment_id: str, payload: Body) -> dict:
        pid = _reader(payload.project_id)
        with begin(engine) as c:
            actor = require_write_actor(c, pid, payload.actor, "workflow_manage")
            try:
                return experiments.stop(c, project_id=pid, eid=experiment_id, actor=actor)
            except LookupError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error

    @router.post("/v1/experiments/{experiment_id}/promote")
    def promote_experiment(experiment_id: str, payload: PromoteBody) -> dict:
        pid = _reader(payload.project_id)
        with begin(engine) as c:
            actor = require_write_actor(c, pid, payload.actor, "providerops_manage")
            try:
                return experiments.promote(c, project_id=pid, eid=experiment_id,
                                           arm=payload.arm, actor=actor, anyway=payload.anyway)
            except LookupError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            except experiments.NotEnoughEvidence as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            except (experiments.ExperimentError, ValueError) as error:
                raise HTTPException(status_code=422, detail=str(error)) from error

    # -------------------------------------------------------------- person feedback
    def _person(c, pid: str, claimed: Optional[str]) -> str:
        require_reader(c, pid)
        who = actor_of(c, pid, claimed)
        if not who:
            raise HTTPException(status_code=422, detail="actor is required")
        return who

    @router.post("/v1/feedback")
    def give_feedback(payload: FeedbackBody) -> dict:
        pid = _reader(payload.project_id)
        with begin(engine) as c:
            who = _person(c, pid, payload.actor)
            try:
                if payload.subject_kind == "edit":
                    return {"edit_ratio": person_feedback.record_edit_amount(
                        c, project_id=pid, actor=who, doc_kind=payload.decision_kind or "",
                        subject_id=payload.subject_id, ratio=payload.edit_ratio)}
                return person_feedback.record(
                    c, project_id=pid, actor=who, subject_kind=payload.subject_kind,
                    subject_id=payload.subject_id, rating=payload.rating, chips=payload.chips,
                    decision_kind=payload.decision_kind)
            except person_feedback.FeedbackError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/v1/feedback/prompts")
    def feedback_prompts(project_id: str, actor: Optional[str] = None,
                         subjects: Optional[str] = None) -> dict:
        pid = _reader(project_id)
        pairs = []
        for item in (subjects or "").split(",")[:20]:
            if ":" in item:
                kind, sid = item.split(":", 1)
                pairs.append((kind.strip(), sid.strip()))
        with begin(engine) as c:
            who = _person(c, pid, actor)
            return person_feedback.prompts(c, actor=who, subjects=pairs)

    @router.post("/v1/feedback/prompts/shown")
    def feedback_prompt_shown(payload: ShownBody) -> dict:
        pid = _reader(payload.project_id)
        if payload.prompt not in ("rate", "checkin"):
            raise HTTPException(status_code=422, detail="prompt is rate or checkin")
        with begin(engine) as c:
            who = _person(c, pid, payload.actor)
            person_feedback.mark_shown(c, who, payload.prompt)
        return {"ok": True}

    @router.post("/v1/feedback/checkin")
    def feedback_checkin(payload: CheckinBody) -> dict:
        pid = _reader(payload.project_id)
        with begin(engine) as c:
            who = _person(c, pid, payload.actor)
            try:
                return person_feedback.checkin(
                    c, project_id=pid, actor=who, answer=payload.answer, line=payload.line,
                    share_line=payload.share_line, dismiss=payload.dismiss)
            except person_feedback.CheckinTooSoon as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            except person_feedback.FeedbackError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error

    # -------------------------------------------------------------- sharing (client)
    @router.get("/v1/telemetry/sharing")
    def get_sharing(project_id: str) -> dict:
        pid = _reader(project_id)
        with begin(engine) as c:
            require_reader(c, pid)
            return improve_boss.sharing_view(c, settings())

    @router.put("/v1/telemetry/sharing")
    def put_sharing(payload: SharingBody) -> dict:
        pid = _reader(payload.project_id)
        with begin(engine) as c:
            actor = require_write_actor(c, pid, payload.actor, "providerops_manage")
            try:
                if payload.switch is not None:
                    if payload.on is None:
                        raise ValueError("on is required with switch")
                    improve_boss.set_switch(c, payload.switch, bool(payload.on), actor=actor,
                                            project_id=pid)
                if payload.weights is not None:
                    scorecards.set_weights(c, payload.weights, actor, pid)
                if payload.mask_words is not None:
                    improve_boss.set_mask_words(c, payload.mask_words, actor=actor, project_id=pid)
                if payload.url is not None:
                    if improve_boss.endpoint(c, settings())[1] == "env":
                        raise HTTPException(status_code=409,
                                            detail="Set by BOSS_TELEMETRY_URL in this install's environment.")
                    improve_boss.set_endpoint(c, payload.url, actor=actor, project_id=pid)
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            return improve_boss.sharing_view(c, settings())

    @router.get("/v1/telemetry/preview")
    def get_preview(project_id: str) -> dict:
        """The exact next payload. Reading it freezes it: this is the one that is sent."""
        pid = _reader(project_id)
        with begin(engine) as c:
            require_reader(c, pid)
            return improve_boss.preview(c, settings())

    @router.get("/v1/telemetry/sends/{send_id}")
    def get_send(send_id: str, project_id: str) -> dict:
        pid = _reader(project_id)
        with begin(engine) as c:
            require_reader(c, pid)
            body = improve_boss.sent_body(c, send_id)
        if body is None:
            raise HTTPException(status_code=404, detail="No such send.")
        return {"id": send_id, "json": body}

    @router.get("/v1/telemetry/export")
    def export_batch(project_id: str, actor: Optional[str] = None) -> Response:
        pid = _reader(project_id)
        with begin(engine) as c:
            require_write_actor(c, pid, actor, "providerops_manage")
            raw = improve_boss.export(c, settings())
        if raw is None:
            raise HTTPException(status_code=409, detail="Nothing to export: sharing is off.")
        return Response(content=raw, media_type="application/json", headers={
            "Content-Disposition": 'attachment; filename="boss-improve-batch.json"',
            "Cache-Control": "no-store"})

    def _sendable(c) -> None:
        """409 with the reason when nothing may leave (env off, or no collector): P2."""
        why = improve_boss.blocked(c, settings())
        if why:
            raise HTTPException(status_code=409, detail={"code": "sharing_blocked", "message": why})

    @router.post("/v1/telemetry/rotate")
    def rotate_install_id(payload: Body) -> dict:
        """A new id. The old id's data is deleted at the collector first when it can be; if not,
        the old id is kept to delete later, and the answer says so (`old`)."""
        pid = _reader(payload.project_id)
        with begin(engine) as c:
            actor = require_write_actor(c, pid, payload.actor, "providerops_manage")
        got = improve_boss.rotate(engine, settings(), actor=actor, project_id=pid)
        with begin(engine) as c:
            return {**improve_boss.sharing_view(c, settings()), "rotated": got}

    @router.post("/v1/telemetry/forget")
    def forget_sent(payload: Body) -> dict:
        """Delete what was sent: a signed request per id this install used, then a new id."""
        pid = _reader(payload.project_id)
        with begin(engine) as c:
            actor = require_write_actor(c, pid, payload.actor, "providerops_manage")
            _sendable(c)
        return improve_boss.forget(engine, settings(), actor=actor, project_id=pid)

    @router.get("/v1/telemetry/share-bundle")
    def share_bundle(project_id: str, unit_kind: str, unit_id: str,
                     actor: Optional[str] = None) -> dict:
        pid = _reader(project_id)
        with begin(engine) as c:
            require_write_actor(c, pid, actor, "providerops_manage")
            try:
                return improve_boss.share_bundle(c, pid, unit_kind, unit_id)
            except LookupError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error

    @router.post("/v1/telemetry/share-run")
    def share_run(payload: ShareRunBody) -> dict:
        """Send one edited, redacted run bundle, once (the person pressed Send)."""
        pid = _reader(payload.project_id)
        with begin(engine) as c:
            require_write_actor(c, pid, payload.actor, "providerops_manage")
            _sendable(c)
        got = improve_boss.share_send(engine, settings(), bundle=payload.bundle,
                                      note=payload.note or "")
        if not got.get("sent"):
            raise HTTPException(status_code=502, detail=f"Not sent: {got.get('error')}")
        return got

    @router.post("/v1/telemetry/tick")
    def telemetry_tick(payload: Optional[dict] = None) -> dict:
        """The worker's loop: scorecard upkeep, collector retention, the daily send if due."""
        require_worker()
        out = improve_boss.tick(engine, settings())
        return {"send": (out.get("send") or {}).get("why") or
                ("sent" if (out.get("send") or {}).get("sent") else "failed"),
                "maintained": "scorecards" in out}

    # -------------------------------------------------------------- collector dashboard
    def _collector_on(c) -> None:
        if not improve_collector.enabled(c):
            raise HTTPException(status_code=404, detail="Collector mode is off.")

    @router.get("/v1/telemetry/collector")
    def collector_view(project_id: str) -> dict:
        pid = _reader(project_id)
        with begin(engine) as c:
            require_reader(c, pid)
            _collector_on(c)
            return improve_collector.dashboard(c)

    @router.get("/v1/telemetry/collector.csv")
    def collector_csv(project_id: str) -> PlainTextResponse:
        pid = _reader(project_id)
        with begin(engine) as c:
            require_reader(c, pid)
            _collector_on(c)
            body = improve_collector.csv_export(c)
        return PlainTextResponse(body, media_type="text/csv", headers={
            "Content-Disposition": 'attachment; filename="boss-installs.csv"'})

    # -------------------------------------------------------------- collector machine routes
    async def _machine(request: Request, handle: Callable[[Any, Any, str], dict],
                       cap: int = improve_collector.MAX_GZ) -> JSONResponse:
        from starlette.concurrency import run_in_threadpool

        def _on() -> bool:
            with begin(engine) as c:
                return improve_collector.enabled(c)

        if not await run_in_threadpool(_on):
            return JSONResponse(status_code=404, content={"detail": "not_found"})
        try:
            body = await read_capped(request, cap)
        except improve_collector.CollectorError as error:
            return JSONResponse(status_code=error.status, content={"detail": error.code})
        ip = client_ip(request.client.host if request.client else "",
                       request.headers.get("x-forwarded-for") or "",
                       str(getattr(settings(), "trusted_proxies", "") or ""))

        def _work() -> dict:
            payload = improve_collector.unpack(body, request.headers.get("content-encoding"),
                                               cap_gz=cap)
            with begin(engine) as c:
                return handle(c, payload, ip)

        try:
            return JSONResponse(content=await run_in_threadpool(_work))
        except improve_collector.CollectorError as error:
            return JSONResponse(status_code=error.status, content={"detail": error.code})

    @router.post("/v1/telemetry/ingest")
    async def telemetry_ingest(request: Request) -> JSONResponse:
        return await _machine(request, lambda c, p, ip: improve_collector.ingest(c, p, ip=ip))

    @router.post("/v1/telemetry/delete")
    async def telemetry_delete(request: Request) -> JSONResponse:
        return await _machine(request, lambda c, p, ip: improve_collector.delete(c, p),
                              cap=16 * 1024)

    @router.post("/v1/telemetry/share")
    async def telemetry_share(request: Request) -> JSONResponse:
        return await _machine(request, lambda c, p, ip: improve_collector.share(c, p, ip=ip))

    return router
