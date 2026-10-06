"""CI/CD multi-vendor product surface — catalog, generate, validate, provenance.

Prefix: ``/v1/cicd``

Dry-run first. No live cloud writes. Live poll stays on pipeline_adapter.
"""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from boss_engine.services import cicd_pipelines as cicd


router = APIRouter(prefix="/v1/cicd", tags=["cicd"])


# ── request / response models ────────────────────────────────────────────────


class CICDGenerateRequest(BaseModel):
    vendor: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="github_actions | gitlab_ci | jenkins | circleci | buildkite",
    )
    language: str = Field(..., min_length=1, max_length=64)
    framework: Optional[str] = Field(default=None, max_length=64)

    @field_validator("vendor", "language", mode="before")
    @classmethod
    def _strip_lower(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("framework", mode="before")
    @classmethod
    def _strip_framework(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class CICDValidateRequest(BaseModel):
    yaml: str = Field(..., description="CI config text (YAML or Jenkinsfile)")
    vendor: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Optional vendor hint for structure checks",
    )

    @field_validator("vendor", mode="before")
    @classmethod
    def _strip_vendor(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip().lower()
            return stripped or None
        return value


class CICDRecordRunRequest(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=128)
    vendor: str = Field(..., min_length=1, max_length=64)
    status: str = Field(default="unknown", max_length=32)
    external_id: Optional[str] = Field(default=None, max_length=256)
    repository: Optional[str] = Field(default=None, max_length=256)
    ref: Optional[str] = Field(default=None, max_length=256)
    workflow: Optional[str] = Field(default=None, max_length=256)
    commit_sha: Optional[str] = Field(default=None, max_length=64)
    url: Optional[str] = Field(default=None, max_length=1024)
    dedupe_key: Optional[str] = Field(default=None, max_length=256)
    delivery_run_id: Optional[str] = Field(default=None, max_length=128)
    deployment_run_id: Optional[str] = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: Optional[str] = Field(default=None, max_length=128)

    @field_validator("project_id", "vendor", "status", mode="before")
    @classmethod
    def _strip_required(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value


# ── routes ───────────────────────────────────────────────────────────────────


@router.get("/catalog")
@permit(P.CICD_READ, scope=Scope.org())
def get_catalog() -> dict[str, Any]:
    """Multi-vendor CI/CD catalog (templates + adapter capability hints)."""
    return cicd.catalog()


@router.post("/generate")
@permit(P.CICD_MANAGE, scope=Scope.org())
def post_generate(payload: CICDGenerateRequest) -> dict[str, Any]:
    """Dry-run CI config generation for vendor + language (+ optional framework)."""
    try:
        return cicd.generate(
            vendor=payload.vendor,
            language=payload.language,
            framework=payload.framework,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/validate")
@permit(P.CICD_MANAGE, scope=Scope.org())
def post_validate(payload: CICDValidateRequest) -> dict[str, Any]:
    """Structure-level YAML (or Jenkinsfile) validation — no cloud calls."""
    return cicd.validate_yaml_structure(payload.yaml, vendor=payload.vendor)


@router.post("/runs")
@permit(P.CICD_MANAGE, scope=Scope.project(body="project_id"))
def post_record_run(payload: CICDRecordRunRequest) -> dict[str, Any]:
    """Record a pipeline-run provenance entry (BOSS-side ledger)."""
    try:
        return cicd.record_run(
            project_id=payload.project_id,
            vendor=payload.vendor,
            status=payload.status,
            external_id=payload.external_id,
            repository=payload.repository,
            ref=payload.ref,
            workflow=payload.workflow,
            commit_sha=payload.commit_sha,
            url=payload.url,
            dedupe_key=payload.dedupe_key,
            delivery_run_id=payload.delivery_run_id,
            deployment_run_id=payload.deployment_run_id,
            metadata=payload.metadata,
            actor=payload.actor,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/runs")
@permit(P.CICD_READ, scope=Scope.project(query="project_id"))
def get_runs(
    project_id: str = Query(..., min_length=1),
    limit: int = Query(default=50, ge=1, le=200),
    vendor: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    """List recorded pipeline runs for a project."""
    try:
        return cicd.list_runs(
            project_id=project_id,
            limit=limit,
            vendor=vendor,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/status")
@permit(P.CICD_READ, scope=Scope.project(query="project_id"))
def get_status(
    project_id: str = Query(..., min_length=1),
) -> dict[str, Any]:
    """What's configured / recent runs / vendor catalog summary for a project."""
    try:
        return cicd.status(project_id=project_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
