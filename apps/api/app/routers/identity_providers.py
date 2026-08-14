"""HTTP surface for first-class IdP product packs.

Prefix: ``/v1/identity-providers``

Sits on top of generic OIDC/SAML/SCIM runtime. Catalog + dry-run blueprints only;
no crypto reimplementation and no secrets in responses.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.services.enterprise import EnterpriseService
from boss_engine.services.identity_provider_catalog import (
    catalog_summary,
    get_identity_provider,
    list_identity_provider_catalog,
    match_configured_providers,
    materialize_blueprint,
)

from ..config import get_settings
from ..db import begin, get_engine

router = APIRouter(prefix="/v1/identity-providers", tags=["identity-providers"])


class BlueprintRequest(BaseModel):
    """Materialize a dry-run enterprise IdP config from a catalog pack."""

    project_id: str = Field(..., min_length=1)
    variables: dict[str, Any] = Field(default_factory=dict)
    provider_type: Optional[str] = None
    display_name: Optional[str] = None
    audience: Optional[str] = None
    secret_ref: Optional[str] = None
    status: str = "planned"
    actor: str = "system"
    # Prefer dry-run. When true, still only returns JSON; does not upsert by default.
    # apply=True is reserved for a future guarded path; currently rejected.
    apply: bool = False


class BlueprintResponse(BaseModel):
    dry_run: bool
    idp_id: str
    idp_name: str
    project_id: str
    provider_type: str
    setup_steps: list[str]
    default_oidc_claim_map: dict[str, str]
    rendered_endpoints: dict[str, str]
    docs_url: str
    enterprise_upsert_path: str
    enterprise_payload: dict[str, Any]
    notes: list[str]


def _settings_and_engine():
    settings = get_settings()
    engine = get_engine(settings)
    return settings, engine


@router.get("")
@router.get("/")
def list_catalog() -> dict[str, Any]:
    """Return the top-10 IdP product catalog (static, no secrets)."""
    return catalog_summary()


@router.get("/project-status")
def project_status(
    project_id: str = Query(..., min_length=1),
) -> dict[str, Any]:
    """Which catalog IdPs are already configured in the enterprise store."""
    settings, engine = _settings_and_engine()
    enterprise = EnterpriseService(settings)
    with begin(engine) as connection:
        configured = enterprise.list_identity_providers(connection, project_id)
    matched = match_configured_providers(None, configured)
    return {
        "project_id": project_id,
        "configured_count": sum(1 for item in matched if item["configured"]),
        "catalog_count": len(matched),
        "items": matched,
    }


@router.get("/{idp_id}")
def get_catalog_detail(idp_id: str) -> dict[str, Any]:
    """Return full catalog entry including setup steps and claim map."""
    entry = get_identity_provider(idp_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Unknown identity provider: {idp_id}")
    return {
        "idp": entry,
        "blueprint_template": {
            "path": f"/v1/identity-providers/{entry['id']}/blueprint",
            "method": "POST",
            "required_fields": [
                field["key"]
                for field in entry.get("config_fields") or []
                if field.get("required")
            ],
            "optional_fields": [
                field["key"]
                for field in entry.get("config_fields") or []
                if not field.get("required")
            ],
            "example_body": {
                "project_id": "boss-dogfood",
                "variables": {
                    field["key"]: field.get("placeholder") or f"<{field['key']}>"
                    for field in entry.get("config_fields") or []
                    if not field.get("secret")
                },
                "provider_type": entry.get("enterprise_provider_type_default")
                or entry.get("primary_protocol"),
                "status": "planned",
                "actor": "system",
            },
        },
    }


@router.post("/{idp_id}/blueprint", response_model=BlueprintResponse)
def create_blueprint(idp_id: str, payload: BlueprintRequest) -> dict[str, Any]:
    """Materialize a dry-run enterprise IdP config payload.

    Never applies by default. ``apply=true`` is rejected until a guarded
    enterprise upsert path is explicitly enabled (fail closed).
    """
    if payload.apply:
        raise HTTPException(
            status_code=422,
            detail=(
                "apply=true is not supported on this endpoint; "
                "POST the dry-run enterprise_payload to "
                "/v1/enterprise/identity-providers after review."
            ),
        )

    # Reject raw secrets in variables (defense in depth; catalog also checks).
    forbidden = {
        "client_secret",
        "secret",
        "password",
        "private_key",
        "api_token",
        "token",
        "client_secret_value",
    }
    leaked = [k for k in (payload.variables or {}) if str(k).lower() in forbidden]
    if leaked:
        raise HTTPException(
            status_code=422,
            detail=(
                "Raw secrets are not accepted in blueprint variables "
                f"({', '.join(sorted(leaked))}); use secret_ref."
            ),
        )

    try:
        blueprint = materialize_blueprint(
            idp_id,
            project_id=payload.project_id.strip(),
            variables=payload.variables,
            provider_type=payload.provider_type,
            display_name=payload.display_name,
            audience=payload.audience,
            secret_ref=payload.secret_ref,
            status=payload.status,
            actor=payload.actor,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    return blueprint


# Re-export for tests / tooling that want the full static list without HTTP.
def full_catalog() -> list[dict[str, Any]]:
    return list_identity_provider_catalog()
