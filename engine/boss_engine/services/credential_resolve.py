"""One place a project+provider model/provider key becomes a value.

SecretRef first, process settings last. Adapters must not invent a second
lookup order — that is how one host keeps using env while another uses a
bound ref, and the drift is silent until the wrong key is billed.

Rules:
  * A usable SecretRef (active / rotating / rotation_due) for that
    project+provider is resolved through ``resolve_secret_ref``. If the
    backend cannot produce the value, the error propagates. Env is not a
    consolation prize for a bound-but-broken ref.
  * Settings ``env_fallback_attr`` is a documented last resort only when
    no usable ref is bound.
  * The value is never logged, never written to an event, never returned
    in an exception message by this helper.
"""
from __future__ import annotations

from typing import Optional

from ..ports import sql
from .secret_backends import resolve_secret_ref


def resolve_project_credential(
    connection,
    settings,
    *,
    project_id: str,
    provider: str,
    env_fallback_attr: str,
) -> Optional[str]:
    row = connection.execute(
        sql(
            "SELECT id FROM secret_refs "
            "WHERE project_id = :project_id AND provider = :provider "
            "AND status IN ('active', 'rotating', 'rotation_due') "
            "ORDER BY updated_at DESC NULLS LAST, id "
            "LIMIT 1"
        ),
        {"project_id": project_id, "provider": provider},
    ).mappings().first()
    if row and row.get("id"):
        return resolve_secret_ref(connection, row["id"], settings)
    if not env_fallback_attr:
        return None
    raw = getattr(settings, env_fallback_attr, None)
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None
