"""Document systems (BOSSAPP-1384): Confluence, connected once for the org.

One org-wide connection (``docs_connections``), read through by every project of the org, like the
ticket systems' org-wide list. The token is tested first, then stored by reference in the secret
backend; it is never returned (the answer carries last4 and a fingerprint, only to someone who may
manage the connection).

WHO MAY DO WHAT
  integration:read     see the connection, list its spaces
  integration:manage   connect (test first), re-test, change spaces, disconnect — at the
                       connection's HOME project (the one it was connected from); from another
                       project the caller needs integration:manage there too
  knowledge:manage     sync the chosen spaces into THIS project's knowledge

Errors are typed ``{"detail": {"error", "message"}}``: ``not_connected`` / ``auth_failed`` (424),
``not_found`` (404), ``version_conflict`` (409), ``rate_limited`` (429), ``invalid_request`` /
``capability_not_supported`` / ``host_not_allowed`` (422), ``upstream_error`` (502).
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.identity import P, Scope, permit
from boss_engine.services import confluence_knowledge, docs_connections as docs
from boss_engine.services.knowledge.service import MANAGE

from ..db import begin, get_engine
from .ticket_systems import _also, _manager, _may_manage, _reader, _settings

router = APIRouter(tags=["docs-connections"])


class DocsConnectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    provider: str = Field(default="confluence", max_length=40)
    base_url: str = Field(default="", max_length=500)
    deployment: str = Field(default="cloud", max_length=20)
    auth_method: str = Field(default="basic", max_length=20)
    fields: dict[str, Any] = Field(default_factory=dict, description="Non-secret fields: email.")
    secrets: dict[str, str] = Field(default_factory=dict, description="Write-only: api_token. "
                                    "Tested first, then stored in the secret backend; never "
                                    "returned.")
    default_spaces: Optional[list[str]] = None
    use_jira_credential: bool = Field(default=False, description="Reuse the org's Jira site, "
                                      "email and token (copied into this connection's own vault "
                                      "entry).")
    actor: Optional[str] = None


class DocsProjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    actor: Optional[str] = None


class DocsSettingsBody(DocsProjectBody):
    default_spaces: Optional[list[str]] = None
    knowledge_spaces: Optional[list[str]] = None


class DocsSyncBody(DocsProjectBody):
    spaces: Optional[list[str]] = None
    full: bool = False


def _refuse(error: BaseException) -> HTTPException:
    typed = docs.explain(error)
    return HTTPException(status_code=typed.status, detail=typed.body())


def _provider(name: str) -> str:
    if name not in docs.PROVIDERS:
        raise _refuse(docs.DocsConnectionError("invalid_request",
                                               f"Unknown document system {name!r}."))
    return name


def _row_for_manage(connection, project_id: str, actor: Optional[str]) -> tuple[dict, str]:
    """(the org's row, the acting person), requiring integration:manage here AND at its home."""
    who = _manager(connection, project_id, actor)
    row = docs.get_row(connection, project_id)
    if row is None:
        raise _refuse(docs.DocsConnectionError("not_connected", docs.NOT_CONNECTED, 424))
    if row["home_project_id"] != project_id:
        _also("integration:manage", row["home_project_id"], "docs_connection")
    return row, who


@router.get("/v1/docs-connections/catalog")
@permit(P.INTEGRATION_READ, scope=Scope.org())
def docs_catalog() -> dict:
    """The document systems BOSS can connect: label, auth fields, capabilities."""
    return {"items": docs.catalog()}


@router.get("/v1/docs-connections")
@permit(P.INTEGRATION_READ, scope=Scope.project(query="project_id"))
def list_docs_connections(project_id: str) -> dict:
    """The org's document-system connections as this project sees them (connected once, usable
    from every project). Empty when none is connected."""
    with begin(get_engine()) as connection:
        _reader(connection, project_id)
        row = docs.get_row(connection, project_id, include_disconnected=True)
        view = None
        if row is not None:
            view = docs.public_view(connection, row, project_id,
                                    can_manage=_may_manage(row["home_project_id"]))
    return {"project_id": project_id, "connections": [view] if view else []}


@router.post("/v1/docs-connections", status_code=201)
@permit(P.INTEGRATION_MANAGE, scope=Scope.project(body="project_id"))
def connect_docs_system(payload: DocsConnectBody) -> dict:
    """Connect Confluence: TEST FIRST (nothing is stored when the test fails), then the token goes
    to the secret backend and the row keeps its reference. Reconnecting without a token keeps the
    stored one."""
    provider = _provider(payload.provider)
    settings = _settings()
    token = str(payload.secrets.get("api_token") or "").strip()
    unknown = sorted(set(payload.secrets) - {"api_token"})
    if unknown or set(payload.fields) - {"email"}:
        raise _refuse(docs.DocsConnectionError(
            "invalid_request", "Confluence takes the email, and the api_token secret."))
    with begin(get_engine()) as connection:
        actor = _manager(connection, payload.project_id, payload.actor)
        existing = docs.get_row(connection, payload.project_id)
        if existing is not None and existing["home_project_id"] != payload.project_id:
            _also("integration:manage", existing["home_project_id"], "docs_connection")
        if not token and not payload.use_jira_credential and docs.is_connected(existing):
            token = docs.stored_token(connection, settings, existing)
        try:
            config, token = docs.prepare(
                connection, settings, project_id=payload.project_id, base_url=payload.base_url,
                auth_method=payload.auth_method, deployment=payload.deployment,
                email=str(payload.fields.get("email") or ""), token=token,
                use_jira_credential=payload.use_jira_credential)
        except docs.DocsConnectionError as error:
            raise _refuse(error) from None
    try:
        outcome = docs.run_test(config, token)
    except docs.DocsConnectionError as error:
        raise _refuse(error) from None
    with begin(get_engine()) as connection:
        try:
            row = docs.store(connection, settings, project_id=payload.project_id, config=config,
                             token=token, outcome=outcome, actor=actor,
                             default_spaces=payload.default_spaces)
        except docs.DocsConnectionError as error:
            raise _refuse(error) from None
        view = docs.public_view(connection, row, payload.project_id, can_manage=True)
    del token
    return {"connection": view, "test": {"ok": True, "account": outcome.get("account"),
                                         "sees_spaces": outcome.get("sees_spaces")},
            "provider": provider}


@router.post("/v1/docs-connections/test")
@permit(P.INTEGRATION_MANAGE, scope=Scope.project(body="project_id"))
def test_docs_connection(payload: DocsProjectBody) -> dict:
    """Re-test the connection with one cheap call (who BOSS is there, and whether it can see
    spaces). Records the outcome on the row."""
    settings = _settings()
    with begin(get_engine()) as connection:
        row, actor = _row_for_manage(connection, payload.project_id, payload.actor)
        token = docs.stored_token(connection, settings, row)
        config = dict(row["configuration"])
    try:
        outcome = docs.run_test(config, token)
        ok, error = True, None
    except docs.DocsConnectionError as refused:
        outcome, ok, error = {}, False, refused
    del token
    with begin(get_engine()) as connection:
        docs.record_test(connection, row, ok, actor, account=outcome.get("account"))
        fresh = docs.get_row(connection, payload.project_id, include_disconnected=True)
        view = docs.public_view(connection, fresh, payload.project_id, can_manage=True)
    if error is not None:
        raise HTTPException(status_code=error.status, detail={**error.body(), "connection": view})
    return {"connection": view, "test": {"ok": True, "account": outcome.get("account"),
                                         "sees_spaces": outcome.get("sees_spaces")}}


@router.patch("/v1/docs-connections")
@permit(P.INTEGRATION_MANAGE, scope=Scope.project(body="project_id"))
def update_docs_connection(payload: DocsSettingsBody) -> dict:
    """Change the default spaces (what agents search by default) and the spaces kept in knowledge."""
    with begin(get_engine()) as connection:
        row, actor = _row_for_manage(connection, payload.project_id, payload.actor)
        try:
            docs.update_settings(connection, row, actor=actor,
                                 default_spaces=payload.default_spaces,
                                 knowledge_spaces=payload.knowledge_spaces)
        except docs.DocsConnectionError as error:
            raise _refuse(error) from None
        fresh = docs.get_row(connection, payload.project_id, include_disconnected=True)
        return {"connection": docs.public_view(connection, fresh, payload.project_id,
                                               can_manage=True)}


@router.delete("/v1/docs-connections")
@permit(P.INTEGRATION_MANAGE, scope=Scope.project(query="project_id"))
def disconnect_docs_connection(project_id: str, actor: Optional[str] = None) -> dict:
    """Disconnect: the token reference is removed; the spaces are kept for a reconnect, and
    knowledge already synced stays."""
    with begin(get_engine()) as connection:
        row, who = _row_for_manage(connection, project_id, actor)
        docs.disconnect(connection, row, who)
        fresh = docs.get_row(connection, project_id, include_disconnected=True)
        return {"connection": docs.public_view(connection, fresh, project_id, can_manage=True)}


@router.get("/v1/docs-connections/spaces")
@permit(P.INTEGRATION_READ, scope=Scope.project(query="project_id"))
def docs_spaces(project_id: str, limit: int = Query(100, ge=1, le=500)) -> dict:
    """The spaces the connection's account can see (for the space picker)."""
    settings = _settings()
    with begin(get_engine()) as connection:
        _reader(connection, project_id)
        try:
            _row, client = docs.client_for(connection, settings, project_id)
            return {"spaces": client.list_spaces(limit)}
        except Exception as error:  # noqa: BLE001 — typed refusal, never a trace
            raise _refuse(error) from None


@router.post("/v1/docs-connections/sync")
@permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
def sync_confluence_knowledge(payload: DocsSyncBody) -> dict:
    """Index the chosen spaces' pages into this project's knowledge (changed pages only after the
    first sync). Each page keeps its space, id, version and URL."""
    from apps.api.app.main import require_project_write_actor

    settings = _settings()
    with begin(get_engine()) as connection:
        who = require_project_write_actor(connection, payload.project_id, payload.actor,
                                          MANAGE)
        try:
            return confluence_knowledge.sync(connection, settings, project_id=payload.project_id,
                                             spaces=payload.spaces, full=payload.full, actor=who)
        except Exception as error:  # noqa: BLE001
            raise _refuse(error) from None
