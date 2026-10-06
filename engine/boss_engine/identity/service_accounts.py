"""Service accounts: non-human principals for CI, scripts and integrations (IDENTITY.md §5.4).

They never sign in; they hold access roles through bindings, like people, and act with
`boss_sak_` keys. Each has at least one human owner. If every owner is disabled the account is
"orphaned" (a posture item), not disabled: that would break production integrations.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from sqlalchemy import text

from . import ids
from .audit import Outcome
from .authn_support import emit
from .principal import AuthContext, Principal, PrincipalKind, PrincipalStatus

UPDATABLE_FIELDS = frozenset({"display_name", "description", "owner_principal_ids"})


class ServiceAccountRefused(Exception):
    def __init__(self, code: str, status: int = 422):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class ServiceAccount:
    principal_id: str
    org_id: str
    display_name: str
    description: Optional[str]
    status: str
    owner_principal_ids: tuple[str, ...]
    orphaned: bool

    def principal(self) -> Principal:
        return Principal(self.principal_id, PrincipalKind.SERVICE_ACCOUNT, self.org_id,
                         PrincipalStatus(self.status), self.display_name)

    def public(self) -> dict[str, Any]:
        return {"id": self.principal_id, "display_name": self.display_name,
                "description": self.description, "status": self.status,
                "owners": list(self.owner_principal_ids), "orphaned": self.orphaned}


_SELECT = ("SELECT p.id, p.org_id, p.display_name, p.status, s.description, s.owner_principal_ids, "
           "NOT EXISTS (SELECT 1 FROM identity_principals o WHERE o.id = ANY(s.owner_principal_ids) "
           "AND o.status = 'active') AS orphaned FROM identity_principals p JOIN identity_service_accounts s "
           "ON s.principal_id = p.id ")


def _row(r: Any) -> ServiceAccount:
    return ServiceAccount(r.id, r.org_id, r.display_name, r.description, r.status,
                          tuple(r.owner_principal_ids or ()), bool(r.orphaned))


def _check_owners(db: Any, org_id: str, owners: Iterable[str]) -> list[str]:
    wanted = sorted(set(owners or ()))
    if not wanted:
        raise ServiceAccountRefused("owner_required", 422)
    found = {r[0] for r in db.execute(text(
        "SELECT id FROM identity_principals WHERE org_id = :o AND kind = 'user' AND status = 'active' "
        "AND NOT network_bound AND id = ANY(:ids)"), {"o": org_id, "ids": wanted})}
    if found != set(wanted):
        raise ServiceAccountRefused("owners_must_be_active_people", 422)
    return wanted


def create(db: Any, *, actor: AuthContext, name: str, description: Optional[str],
           owners: Iterable[str]) -> ServiceAccount:
    org = actor.principal.org_id
    name = (name or "").strip()[:200]
    if not name:
        raise ServiceAccountRefused("name_required", 422)
    owner_ids = _check_owners(db, org, owners)
    pid = ids.new_id("sva")
    db.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name, created_by) "
                    "VALUES (:id, :o, 'service_account', 'active', :n, :by)"),
               {"id": pid, "o": org, "n": name, "by": actor.principal.id})
    db.execute(text("INSERT INTO identity_service_accounts (principal_id, description, owner_principal_ids) "
                    "VALUES (:id, :d, :owners)"),
               {"id": pid, "d": (description or "")[:1000] or None, "owners": owner_ids})
    emit(db, org_id=org, event_type="service_account.created", outcome=Outcome.SUCCESS, ctx=actor,
         target_type="service_account", target_id=pid, details={"owners": owner_ids})
    found = get(db, pid, org_id=org)
    assert found is not None
    return found


def get(db: Any, principal_id: str, *, org_id: str) -> Optional[ServiceAccount]:
    r = db.execute(text(_SELECT + "WHERE p.id = :id AND p.org_id = :o"), {"id": principal_id, "o": org_id}).first()
    return _row(r) if r else None


def list_all(db: Any, *, org_id: str) -> list[ServiceAccount]:
    return [_row(r) for r in db.execute(text(_SELECT + "WHERE p.org_id = :o AND p.status <> 'revoked' "
                                                       "ORDER BY p.display_name"), {"o": org_id})]


def update(db: Any, *, actor: AuthContext, principal_id: str, fields: dict[str, Any]) -> ServiceAccount:
    org = actor.principal.org_id
    current = get(db, principal_id, org_id=org)
    if current is None:
        raise ServiceAccountRefused("not_found", 404)
    extra = sorted(set(fields) - UPDATABLE_FIELDS)
    if extra:
        raise ServiceAccountRefused("field_not_allowed", 422)
    if "display_name" in fields:
        name = str(fields["display_name"] or "").strip()[:200]
        if not name:
            raise ServiceAccountRefused("name_required", 422)
        db.execute(text("UPDATE identity_principals SET display_name = :n, updated_at = now() WHERE id = :id"),
                   {"n": name, "id": principal_id})
    if "description" in fields:
        db.execute(text("UPDATE identity_service_accounts SET description = :d WHERE principal_id = :id"),
                   {"d": (str(fields["description"] or ""))[:1000] or None, "id": principal_id})
    if "owner_principal_ids" in fields:
        owners = _check_owners(db, org, fields["owner_principal_ids"])
        db.execute(text("UPDATE identity_service_accounts SET owner_principal_ids = :o WHERE principal_id = :id"),
                   {"o": owners, "id": principal_id})
    emit(db, org_id=org, event_type="service_account.updated", outcome=Outcome.SUCCESS, ctx=actor,
         target_type="service_account", target_id=principal_id, details={"fields": sorted(fields)})
    found = get(db, principal_id, org_id=org)
    assert found is not None
    return found


def set_status(db: Any, *, actor: AuthContext, principal_id: str, status: str) -> bool:
    """disabled | active | revoked (revoked is terminal and revokes every key)."""
    org = actor.principal.org_id
    current = get(db, principal_id, org_id=org)
    if current is None or current.status == "revoked":
        return False
    db.execute(text("UPDATE identity_principals SET status = :s, status_changed_at = now(), "
                    "status_changed_by = :by, updated_at = now() WHERE id = :id"),
               {"s": status, "by": actor.principal.id, "id": principal_id})
    if status == "revoked":
        db.execute(text("UPDATE identity_api_credentials SET revoked_at = now(), revoked_by = :by "
                        "WHERE principal_id = :id AND revoked_at IS NULL"),
                   {"by": actor.principal.id, "id": principal_id})
    if status in ("disabled", "revoked"):
        emit(db, org_id=org, event_type="service_account.disabled", outcome=Outcome.SUCCESS, ctx=actor,
             target_type="service_account", target_id=principal_id, details={"status": status})
    else:
        emit(db, org_id=org, event_type="service_account.updated", outcome=Outcome.SUCCESS, ctx=actor,
             target_type="service_account", target_id=principal_id, details={"status": status})
    return True
