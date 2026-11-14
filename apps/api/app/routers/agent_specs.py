"""The agent definition, versioned: `boss.agent/v1` specs over HTTP (AGENT_PLATFORM §4.1, D2).

An agent is a versioned spec; the roles row and its grants are the active version's projection
(engine/boss_engine/agents). These routes create an agent from a spec, save and publish new
versions, roll back, diff two versions and move a spec in and out as YAML. PATCH /v1/agents keeps
working: each edit there is recorded as the agent's next version too.

Writes bind the actor the way /v1/agents does (`require_write_actor`), and a spec that grants
skills or MCP servers needs the same `workflow_manage` an edit that grants them needs.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


class SpecCreateBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    #: The document as a mapping, or `yaml` as text (one of the two).
    spec: Optional[dict[str, Any]] = None
    yaml: Optional[str] = None


class SpecSaveBody(SpecCreateBody):
    #: False saves version N+1 as a draft; true (default) publishes it.
    publish: bool = True


class SpecImportBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    yaml: str = Field(min_length=1)
    publish: bool = True


class SpecPublishBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    kind: str = "agent"


class SpecRollbackBody(SpecPublishBody):
    to_version: int = Field(ge=1)


def build_agent_specs_router(
    *,
    engine,
    require_write_actor: Callable[..., str],
    require_reader: Callable[..., None],
) -> APIRouter:
    from boss_engine.agents import spec as agent_spec
    from boss_engine.agents import versions

    router = APIRouter(tags=["agents"])

    def _fail(error: Exception):
        from boss_engine.services.agent_identity import IdentityError
        from boss_engine.services.agents import AgentError

        if isinstance(error, versions.VersionError):
            raise HTTPException(status_code=error.status,
                                detail={"message": str(error), "errors": error.errors}) from error
        if isinstance(error, (AgentError, IdentityError)):
            raise HTTPException(status_code=error.status, detail=str(error)) from error
        raise error

    def _doc(body: Any) -> Any:
        if (body.spec is None) == (body.yaml is None):
            raise HTTPException(status_code=422, detail="send the agent spec as `spec` (a mapping) or `yaml`, "
                                                        "not both and not neither.")
        return body.spec if body.spec is not None else body.yaml

    def _grants(doc: Any) -> bool:
        import yaml as _yaml

        if isinstance(doc, str):
            try:
                doc = _yaml.safe_load(doc)
            except _yaml.YAMLError:
                return False
        tools = ((doc or {}).get("spec") or {}).get("tools") if isinstance(doc, dict) else None
        return bool(isinstance(tools, dict) and (tools.get("skills") or tools.get("mcp")))

    def _writer(connection, project_id: str, actor: Optional[str], grants: bool) -> str:
        who = require_write_actor(connection, project_id, actor, "role_review")
        if grants:
            # Granting is the same permission everywhere it happens (POST /v1/role-skills).
            require_write_actor(connection, project_id, who, "workflow_manage")
        return who

    @router.get("/v1/agent-specs/schema")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def spec_schema(project_id: str) -> dict[str, Any]:
        """The JSON Schema of boss.agent/v1, for editors and clients."""
        with begin(engine) as connection:
            require_reader(connection, project_id)
        return agent_spec.json_schema()

    @router.post("/v1/agent-specs", status_code=201)
    @permit(P.AGENT_CREATE, scope=Scope.project(body="project_id"))
    def post_agent_spec(body: SpecCreateBody) -> dict[str, Any]:
        """A new agent from a spec: the role, its identity and version 1, published, at once."""
        doc = _doc(body)
        with begin(engine) as connection:
            actor = _writer(connection, body.project_id, body.actor, _grants(doc))
            try:
                return versions.create_from_spec(connection, body.project_id, doc, actor=actor)
            except Exception as error:  # noqa: BLE001 — typed mapping in _fail
                _fail(error)

    @router.post("/v1/agent-specs/import", status_code=201)
    @permit(P.AGENT_CREATE, scope=Scope.project(body="project_id"))
    def import_spec(body: SpecImportBody) -> dict[str, Any]:
        """A YAML spec in: a new agent when its key is new, else that agent's next version."""
        with begin(engine) as connection:
            actor = _writer(connection, body.project_id, body.actor, _grants(body.yaml))
            try:
                return versions.import_yaml(connection, body.project_id, body.yaml, actor=actor,
                                            publish_now=body.publish)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.get("/v1/agents/{role_key}/spec")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def get_spec(role_key: str, project_id: str, kind: str = "agent") -> dict[str, Any]:
        """The active version: its document, hash, compiled prompt and what publishing it did."""
        with begin(engine) as connection:
            require_reader(connection, project_id)
            current = versions.active(connection, project_id, role_key, kind=kind)
        if current is None:
            raise HTTPException(status_code=404, detail=f"{role_key} has no published spec yet.")
        return current

    @router.put("/v1/agents/{role_key}/spec")
    @permit(P.AGENT_UPDATE, scope=Scope.project(body="project_id"))
    def save_spec(role_key: str, body: SpecSaveBody) -> dict[str, Any]:
        """Version N+1 from a whole document; published unless `publish` is false. A document
        identical to an existing version re-uses it (`duplicate: true`)."""
        doc = _doc(body)
        with begin(engine) as connection:
            actor = _writer(connection, body.project_id, body.actor, _grants(doc))
            try:
                return versions.save_and_publish(connection, body.project_id, doc, actor=actor, key=role_key,
                                                 publish_now=body.publish)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.get("/v1/agents/{role_key}/spec/versions")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def list_spec_versions(role_key: str, project_id: str, kind: str = "agent") -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return {"agent_key": role_key, "kind": kind,
                    "items": versions.list_versions(connection, project_id, role_key, kind=kind)}

    @router.get("/v1/agents/{role_key}/spec/versions/{version}")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def get_spec_version(role_key: str, version: int, project_id: str, kind: str = "agent") -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            try:
                return versions.get_version(connection, project_id, role_key, version, kind=kind)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.post("/v1/agents/{role_key}/spec/versions/{version}/publish")
    @permit(P.AGENT_UPDATE, scope=Scope.project(body="project_id"))
    def publish_spec_version(role_key: str, version: int, body: SpecPublishBody) -> dict[str, Any]:
        """Make a saved version the active one: grants, identity seal and Passport in one go."""
        with begin(engine) as connection:
            actor = _writer(connection, body.project_id, body.actor, True)
            try:
                return versions.publish(connection, body.project_id, role_key, version, actor=actor,
                                        kind=body.kind)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.post("/v1/agents/{role_key}/spec/rollback")
    @permit(P.AGENT_UPDATE, scope=Scope.project(body="project_id"))
    def rollback_spec(role_key: str, body: SpecRollbackBody) -> dict[str, Any]:
        """Back to an earlier version: the row, the grants and the compiled prompt are exactly
        what that version compiled to."""
        with begin(engine) as connection:
            actor = _writer(connection, body.project_id, body.actor, True)
            try:
                return versions.rollback(connection, body.project_id, role_key, body.to_version, actor=actor,
                                         kind=body.kind)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.get("/v1/agents/{role_key}/spec/diff")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def diff_spec(role_key: str, project_id: str, from_version: int, to_version: int,
                  kind: str = "agent") -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            try:
                return versions.diff(connection, project_id, role_key, from_version, to_version, kind=kind)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.get("/v1/agents/{role_key}/spec/export")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def export_spec(role_key: str, project_id: str, version: Optional[int] = None,
                    kind: str = "agent") -> dict[str, Any]:
        """The spec as YAML (the active version, or `version`), ready for import elsewhere."""
        with begin(engine) as connection:
            require_reader(connection, project_id)
            try:
                return {"agent_key": role_key, "yaml": versions.export_yaml(
                    connection, project_id, role_key, version, kind=kind)}
            except Exception as error:  # noqa: BLE001
                _fail(error)

    return router
