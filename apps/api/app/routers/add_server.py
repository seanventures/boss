"""ADD A SERVER: the HTTP surface (engine services/add_server.py; docs/product/wireframes/
agentic-factory/3-add-a-server.html).

POST /v1/projects/{id}/servers/read  the one input read: the words, the network probe (port open,
                                     host key) before any credential, the stored keys by name,
                                     the agent's plan and the preconditions. Creates nothing.
POST /v1/servers                     Add and set up: record the server, hand over access (BOSS's
                                     key, your key written to the vault, or a stored key) and
                                     start the agent. Nothing is installed until the key lands.
GET  /v1/servers/{host_id}/setup     the server page's setup: the diagram, the Now line, the gate
                                     (the one line) while the key is awaited, stats and the log.
                                     Reading it also gives the agent its next turn when one is due,
                                     so a page left open watches the key land.
POST /v1/servers/tick                the worker's clock: every due setup and every watched server.

WHO MAY DO WHAT. Reading the words and probing the network creates nothing, so it never asks for
a step-up: `assistant:chat` on the project, the bar Add a project's folder probe uses (never a
viewer or an auditor). ADDING A SERVER IS HANDING OVER ACCESS (a credential is on the list in
Where people decide), so it is `host:manage` — Owner/Admin, step-up (the passkey). The page is
`host:read`. The clock is `system:operate`.
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit

router = APIRouter(tags=["infra"])
_settings = get_settings()
_engine = get_engine(_settings)


class ReadBody(BaseModel):
    text: str = Field("", max_length=4000)
    kind: Optional[str] = Field(None, description="ssh | dials_in | cloud: the kind pill, when picked.")
    probe: bool = Field(True, description="Probe the network (port, host key) when an address is named.")


class AccessBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str = Field("boss_key", description="boss_key | my_key | stored_key")
    backend: str = Field("database", description="database | file | platform_secret_store")
    private_key: Optional[str] = Field(None, max_length=20000,
                                       description="my_key only: write-only, straight to the vault.")
    secret_ref_id: Optional[str] = Field(None, description="stored_key only: a stored SSH key.")


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    project_id: str
    actor: Optional[str] = None
    address: str
    ssh_user: str = "root"
    ssh_port: int = 22
    name: Optional[str] = None
    arch: Optional[str] = None
    admin_identity_file: Optional[str] = None
    notes: Optional[str] = None
    role: str = "staging"
    port_min: Optional[int] = None
    port_max: Optional[int] = None
    max_apps: Optional[int] = None
    access: AccessBody = Field(default_factory=AccessBody)
    setup: dict[str, bool] = Field(default_factory=dict)
    watch: dict[str, Any] = Field(default_factory=dict)
    probe: dict[str, Any] = Field(default_factory=dict)


def _main():
    from apps.api.app import main

    return main


def _refused(error: Exception) -> JSONResponse:
    status = int(getattr(error, "status", 422) or 422)
    return JSONResponse({"error": "server_refused", "message": str(error), "detail": str(error)},
                        status_code=status)


def _write_actor(connection, project_id: str, claimed: Optional[str]) -> str:
    from apps.api.app import identity_guard

    enforced = identity_guard.request_actor()
    if enforced:
        return enforced
    return _main().require_project_write_actor(connection, project_id, claimed, "task_create")


@router.post("/v1/projects/{project_id}/servers/read")
@permit(P.ASSISTANT_CHAT, scope=Scope.project(path="project_id"))
def read_server_words(project_id: str, payload: ReadBody) -> Any:
    from boss_engine.services import add_server
    from boss_engine.services.project_experience.common import require_project

    with begin(_engine) as connection:
        try:
            require_project(connection, project_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from None
        try:
            return add_server.understand(connection, _settings, project_id, payload.text,
                                         kind=payload.kind, run_probe=payload.probe)
        except add_server.ServerRefused as refused:
            return _refused(refused)


_turning: set[str] = set()
_turning_lock = threading.Lock()


def _turn(host_id: str) -> None:
    """The agent's next turn on this server, off the request (each in its own transaction)."""
    from boss_engine.services import add_server

    try:
        with begin(_engine) as connection:
            if add_server.claim(connection, host_id):
                add_server.advance(connection, _settings, host_id)
    except Exception:  # noqa: BLE001 — the worker's clock tries again
        pass
    finally:
        with _turning_lock:
            _turning.discard(host_id)


def _turn_soon(host_id: str) -> None:
    with _turning_lock:
        if host_id in _turning:
            return
        _turning.add(host_id)
    threading.Thread(target=_turn, args=(host_id,), daemon=True, name=f"server-setup-{host_id}").start()


@router.post("/v1/servers")
@permit(P.HOST_MANAGE, scope=Scope.project(body="project_id"))
def add_and_set_up(payload: CreateBody) -> Any:
    from boss_engine.services import add_server
    from boss_engine.services.secret_backends import SecretUnavailable

    body = payload.model_dump()
    with begin(_engine) as connection:
        actor = _write_actor(connection, payload.project_id, payload.actor)
        try:
            made = add_server.create(connection, _settings, project_id=payload.project_id,
                                     actor=actor, spec=body)
        except add_server.ServerRefused as refused:
            return _refused(refused)
        except (SecretUnavailable, ValueError, RuntimeError) as error:
            return _refused(add_server.ServerRefused(f"Not added: {str(error)[:300]}"))
    _turn_soon(made["host_id"])
    return {**made, "href": f"?area=infra&host={made['host_id']}"}


@router.get("/v1/servers/{host_id}/setup")
@permit(P.HOST_READ, scope=Scope.of("deployment_host", path="host_id"))
def server_setup(host_id: str) -> Any:
    from boss_engine.services import add_server

    with begin(_engine) as connection:
        try:
            out = add_server.view(connection, _settings, host_id)
        except add_server.ServerRefused as refused:
            return _refused(refused)
        due = connection.execute(
            add_server.text("SELECT 1 FROM server_setups WHERE host_id = :h AND phase <> 'watching' "
                            "AND next_attempt_at IS NOT NULL AND next_attempt_at <= now()"),
            {"h": host_id}).first()
    if due:
        _turn_soon(host_id)
    return out


@router.post("/v1/servers/tick")
@permit(P.SYSTEM_OPERATE, scope=Scope.org())
def servers_tick() -> dict[str, Any]:
    from boss_engine.services import add_server

    return add_server.tick(_engine, _settings)
