"""Skills as a governed supply chain: versions, sources, consents and test cases (BOSSAPP-841..844).

The Skills page's History, Import and Tests tabs read and write through here. Every write binds
the signed-in principal through `require_write_actor`, so a console edit is a PERSON's edit (the
consent gate in skill_definitions.save_skill exempts it) and an agent key acting here is still an
agent (the gate holds it).

Mounted from main.py with the auth helpers injected, so this module does not import main.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text as sql_text

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


class VersionBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    version: Any = None


class PinBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    role_key: str
    version: Any = None


class SourcePreviewBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    kind: str
    locator: str
    ref: str = ""
    pin: str = ""
    skill_key: Optional[str] = None


class SourceImportBody(SourcePreviewBody):
    auto_update: bool = False


class SourceUpdateBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    auto_update: Optional[bool] = None
    ref: Optional[str] = None


class CaseBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class RunBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    version: Any = None


class GradeBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    grade: str
    notes: str = ""


#: The permission string the legacy member check uses for skill authoring (main.py's skill save).
_MANAGE = "workflow_manage"


def build_skill_supply_router(
    *,
    engine,
    settings: Callable[[], Any],
    require_write_actor: Callable[..., str],
    require_reader: Callable[..., None],
) -> APIRouter:
    from boss_engine.services import (change_consent, skill_definitions, skill_sources,
                                      skill_tests, skill_versions)

    router = APIRouter(tags=["skills"])

    def _fail(error: Exception):
        if isinstance(error, change_consent.ConsentRequired):
            raise HTTPException(status_code=403, detail=str(error)) from error
        if isinstance(error, (skill_versions.SkillVersionError, skill_tests.SkillTestError,
                              skill_sources.SkillSourceError)):
            status = getattr(error, "status", 422)
            if "failed its latest test run" in str(error):
                status = 409
            raise HTTPException(status_code=status, detail=str(error)) from error
        if getattr(error, "rule_id", None) and isinstance(error, skill_definitions.SkillDefinitionError):
            # A skill-access rule refused it (BOSSAPP-890, skill_policy.SkillPolicyDenied).
            raise HTTPException(status_code=403, detail=str(error)) from error
        if isinstance(error, skill_definitions.SkillExistsError):
            raise HTTPException(status_code=409, detail=str(error)) from error
        if isinstance(error, skill_definitions.SkillDefinitionError):
            raise HTTPException(status_code=422, detail=str(error)) from error
        raise error

    # ── 842: history ──────────────────────────────────────────────────────────────────────────

    @router.get("/v1/skill-definitions/{skill_key}/versions")
    @permit(P.SKILL_READ, scope=Scope.project(query="project_id"))
    def list_versions(skill_key: str, project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return {"skill_key": skill_key,
                    "versions": skill_versions.list_versions(connection, project_id, skill_key),
                    "holders": skill_versions.holders(connection, project_id, skill_key)}

    @router.get("/v1/skill-definitions/{skill_key}/diff")
    @permit(P.SKILL_READ, scope=Scope.project(query="project_id"))
    def diff(skill_key: str, project_id: str, from_version: str,
             to_version: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            try:
                return skill_versions.diff_versions(connection, project_id, skill_key,
                                                    from_version, to_version)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    @router.post("/v1/skill-definitions/{skill_key}/restore")
    @permit(P.SKILL_MANAGE, scope=Scope.project(body="project_id"))
    def restore(skill_key: str, body: VersionBody) -> dict[str, Any]:
        """Make an old version current again — as a new version that copies it."""
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, _MANAGE)
            try:
                return skill_versions.restore(connection, project_id=body.project_id,
                                              actor=actor, skill_key=skill_key,
                                              version=body.version)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    @router.post("/v1/skill-definitions/{skill_key}/publish")
    @permit(P.SKILL_MANAGE, scope=Scope.project(body="project_id"))
    def publish(skill_key: str, body: VersionBody) -> dict[str, Any]:
        """Put every agent holding the skill on one version; 409 when its tests failed."""
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, _MANAGE)
            try:
                return skill_versions.publish(connection, project_id=body.project_id,
                                              actor=actor, skill_key=skill_key,
                                              version=body.version)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    @router.put("/v1/skill-definitions/{skill_key}/pins")
    @permit(P.AGENT_ABILITY_ASSIGN, scope=Scope.project(body="project_id"))
    def pin(skill_key: str, body: PinBody) -> dict[str, Any]:
        """Keep one agent on one version (version null: follow the current one)."""
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, _MANAGE)
            try:
                return skill_versions.pin(connection, project_id=body.project_id, actor=actor,
                                          role_key=body.role_key, skill_key=skill_key,
                                          version=body.version)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    @router.get("/v1/skill-consents")
    @permit(P.SKILL_READ, scope=Scope.project(query="project_id"))
    def consents(project_id: str, skill_key: Optional[str] = None) -> dict[str, Any]:
        """Agent changes waiting for a person (843), each with its diff and decision."""
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return {"consents": change_consent.pending(connection, project_id, "skill",
                                                       skill_key)}

    # ── 841: sources ──────────────────────────────────────────────────────────────────────────

    @router.get("/v1/skill-sources")
    @permit(P.SKILL_READ, scope=Scope.project(query="project_id"))
    def list_sources(project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return {"sources": skill_sources.list_sources(connection, project_id)}

    @router.post("/v1/skill-sources/preview")
    @permit(P.SKILL_MANAGE, scope=Scope.project(body="project_id"))
    def preview(body: SourcePreviewBody) -> dict[str, Any]:
        """Fetch and parse without writing — what Import would create, at which commit."""
        with begin(engine) as connection:
            require_write_actor(connection, body.project_id, body.actor, _MANAGE)
        fetcher = skill_sources.default_fetcher(engine, settings(), body.project_id)
        try:
            return skill_sources.preview(fetcher, kind=body.kind, locator=body.locator,
                                         ref=body.ref, pin=body.pin, skill_key=body.skill_key)
        except Exception as error:  # noqa: BLE001
            _fail(error)
            raise

    @router.post("/v1/skill-sources", status_code=201)
    @permit(P.SKILL_MANAGE, scope=Scope.project(body="project_id"))
    def import_source(body: SourceImportBody) -> dict[str, Any]:
        fetcher = skill_sources.default_fetcher(engine, settings(), body.project_id)
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, _MANAGE)
            try:
                return skill_sources.import_source(
                    connection, fetcher, project_id=body.project_id, actor=actor,
                    kind=body.kind, locator=body.locator, ref=body.ref, pin=body.pin,
                    auto_update=body.auto_update, skill_key=body.skill_key)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    @router.patch("/v1/skill-sources/{source_id}")
    @permit(P.SKILL_MANAGE, scope=Scope.project(body="project_id"))
    def update_source(source_id: str, body: SourceUpdateBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, _MANAGE)
            try:
                return {"source": skill_sources.update_source(
                    connection, project_id=body.project_id, actor=actor, source_id=source_id,
                    auto_update=body.auto_update, ref=body.ref)}
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    @router.post("/v1/skill-sources/{source_id}/sync")
    @permit(P.SKILL_MANAGE, scope=Scope.project(body="project_id"))
    def sync_now(source_id: str, body: VersionBody) -> dict[str, Any]:
        with begin(engine) as connection:
            require_write_actor(connection, body.project_id, body.actor, _MANAGE)
            owned = connection.execute(
                sql_text("SELECT 1 FROM skill_sources WHERE id = :i AND project_id = :p"),
                {"i": source_id, "p": body.project_id}).first()
        if owned is None:
            raise HTTPException(status_code=404, detail=f"no skill source {source_id!r}.")
        return skill_sources.sync_one(engine, settings(), source_id)

    # ── 844: test cases ───────────────────────────────────────────────────────────────────────

    @router.get("/v1/skill-definitions/{skill_key}/tests")
    @permit(P.SKILL_READ, scope=Scope.project(query="project_id"))
    def tests(skill_key: str, project_id: str, version: Optional[str] = None) -> dict[str, Any]:
        """Each case's latest output on `version` (default current) beside the version before."""
        with begin(engine) as connection:
            require_reader(connection, project_id)
            try:
                side = skill_tests.compare(connection, project_id, skill_key, version)
                current = skill_versions.current(connection, project_id, skill_key)
                gate = (skill_tests.publish_gate(connection, project_id, skill_key,
                                                 current["id"]) if current else None)
                return {**side, "gate": gate}
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    @router.post("/v1/skill-definitions/{skill_key}/tests/cases", status_code=201)
    @permit(P.SKILL_MANAGE, scope=Scope.project(body="project_id"))
    def save_case(skill_key: str, body: CaseBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, _MANAGE)
            try:
                return {"case": skill_tests.save_case(
                    connection, project_id=body.project_id, actor=actor, skill_key=skill_key,
                    name=body.name, case_input=body.input)}
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    @router.delete("/v1/skill-definitions/{skill_key}/tests/cases/{case_id}")
    @permit(P.SKILL_MANAGE, scope=Scope.project(query="project_id"))
    def delete_case(skill_key: str, case_id: str, project_id: str,
                    actor: Optional[str] = None) -> dict[str, Any]:
        with begin(engine) as connection:
            who = require_write_actor(connection, project_id, actor, _MANAGE)
            try:
                return skill_tests.delete_case(connection, project_id=project_id, actor=who,
                                               skill_key=skill_key, case_id=case_id)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    @router.post("/v1/skill-definitions/{skill_key}/tests/run")
    @permit(P.SKILL_INVOKE, scope=Scope.project(body="project_id"))
    def run_tests(skill_key: str, body: RunBody) -> dict[str, Any]:
        """Run every case against one version (default current)."""
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, _MANAGE)
            try:
                return skill_tests.run_cases(connection, settings(), project_id=body.project_id,
                                             actor=actor, skill_key=skill_key,
                                             version=body.version)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    @router.post("/v1/skill-test-runs/{run_id}/grade")
    @permit(P.SKILL_MANAGE, scope=Scope.project(body="project_id"))
    def grade(run_id: str, body: GradeBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, _MANAGE)
            try:
                return {"run": skill_tests.grade_run(connection, project_id=body.project_id,
                                                     actor=actor, run_id=run_id,
                                                     grade=body.grade, notes=body.notes)}
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    return router
