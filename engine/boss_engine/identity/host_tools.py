"""The keys the host's own tools use, as named service accounts (docs/community/IDENTITY_UPGRADE.md).

Before Identity, every host tool (`boss connect`, `boss doctor`, `make metatron`, the model and
subscription helpers, remote.sh, the examples) called the API with the one shared BOSS_API_KEY.
That key loses identity changes at the claim and stops at the legacy cutoff, so the host tools
get their own credentials, minted from the host shell (`boss identity host-key`, which boss.sh
runs for you) and kept in .env:

  BOSS_HOST_API_KEY   service account "Host tools" with the role "Host tools": everything the
                      shared key could do except changing people and access (the same set the
                      shared key keeps after the claim), org-wide, no cutoff. An Owner sees it
                      under Identity › Service accounts, can narrow the role or revoke the key;
                      the next `boss` command issues a new key.
  BOSS_MCP_API_KEY    service account "MCP (<project>)" per project (`make mcp-setup`): the
                      capabilities you chose, in that one project only, key pinned to it.

Minting is host-shell only (no HTTP route reaches it); only the key's SHA-256 is stored, and the
key is printed once, to the shell that asked.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Optional

HOST_TOOLS_PRINCIPAL = "sva_host_tools"
HOST_TOOLS_ROLE_KEY = "host_tools"
HOST_CLI_PRINCIPAL = "sys_host_cli"
MCP_PRINCIPAL_PREFIX = "sva_mcp_"
#: What `make mcp-setup` granted before Identity (scripts/install/mcp_access.py), as capabilities.
MCP_DEFAULT_CAPABILITIES = ("agent_chat", "command_submit", "task_create", "workflow_manage",
                            "workflow_execute", "health_refresh", "audit_export")


def _sql(statement: str):
    from sqlalchemy import text
    return text(statement)


def host_tools_permissions() -> frozenset[str]:
    """The shared key's set after the claim (no identity writes), minus system-only permissions,
    which no binding can hold."""
    from .migration import CONSOLE_AFTER_CLAIM_SET
    from .permissions_gen import PERMISSION_META

    return frozenset(p for p in CONSOLE_AFTER_CLAIM_SET if not PERMISSION_META[p].system_only)


def mcp_permissions(wanted: Iterable[str] = MCP_DEFAULT_CAPABILITIES) -> frozenset[str]:
    """Each item is one of today's capability names (agent_chat, …) or a catalogue permission
    (workflow_run:start). Plus reading the project, as every member could."""
    from .migration import MEMBER_BASELINE, capability_permissions
    from .permissions_gen import PERMISSION_META

    caps = [w for w in wanted if ":" not in w]
    keys = [w for w in wanted if ":" in w]
    unknown = [k for k in keys if k not in PERMISSION_META or PERMISSION_META[k].system_only]
    from .migration import ALL_CAPABILITIES

    unknown += [c for c in caps if c not in ALL_CAPABILITIES]
    if unknown:
        raise ValueError("unknown permission: " + ", ".join(sorted(unknown)))
    return frozenset(MEMBER_BASELINE | capability_permissions(caps) | set(keys))


def _role(db: Any, org_id: str, key: str, name: str, permissions: frozenset[str],
          scopes: tuple[str, ...]) -> str:
    """An ordinary custom access role holding `permissions`, created once. (A `system` role grants
    nothing through a binding.) An Owner may narrow it in Identity › Roles; issuing a key again
    never undoes that."""
    from .migration import ensure_custom_role

    return ensure_custom_role(db, org_id, key, name, permissions, scopes=scopes, system=False)


def _ensure_account(db: Any, org_id: str, pid: str, name: str, description: str) -> None:
    from .migration import ensure_principal

    ensure_principal(db, HOST_CLI_PRINCIPAL, org_id, "system", "active", "BOSS host CLI")
    ensure_principal(db, pid, org_id, "service_account", "active", name, created_by=HOST_CLI_PRINCIPAL)
    db.execute(_sql("INSERT INTO identity_service_accounts (principal_id, description, "
                    "owner_principal_ids) VALUES (:p, :d, ARRAY[:o]) ON CONFLICT (principal_id) "
                    "DO UPDATE SET description = EXCLUDED.description"),
               {"p": pid, "d": description, "o": HOST_CLI_PRINCIPAL})
    status = db.execute(_sql("SELECT status FROM identity_principals WHERE id = :p"), {"p": pid}).scalar()
    if status != "active":
        # An Owner disabled it: the host shell is the only place that can bring it back, and it
        # says so in the log.
        db.execute(_sql("UPDATE identity_principals SET status = 'active', status_changed_at = now(), "
                        "status_changed_by = :by, status_reason = 'reissued from the host shell', "
                        "updated_at = now() WHERE id = :p"), {"p": pid, "by": HOST_CLI_PRINCIPAL})


def _mint(db: Any, org_id: str, pid: str, name: str, scopes: frozenset[str],
          project_ids: Optional[list[str]], *, revoke_others: bool) -> Any:
    from . import audit, ids
    from .api_keys import CreatedKey
    from .authn import hash_token, mint_api_key
    from .migration import bump_epoch

    revoked = 0
    if revoke_others:
        revoked = len(db.execute(_sql(
            "UPDATE identity_api_credentials SET revoked_at = now(), revoked_by = :by WHERE "
            "principal_id = :p AND revoked_at IS NULL RETURNING id"),
            {"by": HOST_CLI_PRINCIPAL, "p": pid}).all())
    key = mint_api_key("sak")
    kid = ids.new_id("key")
    db.execute(_sql(
        "INSERT INTO identity_api_credentials (id, principal_id, kind, name, token_hash, scopes, "
        "project_ids, created_by) VALUES (:id, :p, 'sak', :n, :th, :s, :proj, :by)"),
        {"id": kid, "p": pid, "n": name, "th": hash_token(key), "s": sorted(scopes),
         "proj": project_ids, "by": HOST_CLI_PRINCIPAL})
    audit.record(audit.SecurityEvent(
        org_id=org_id, event_type="api_key.created", outcome=audit.Outcome.SUCCESS,
        actor_principal_id=HOST_CLI_PRINCIPAL, actor_kind="system", target_type="api_key",
        target_id=kid, details={"kind": "sak", "owner": pid, "issued_from": "host_shell",
                                "projects": project_ids, "revoked_others": revoked}), db=db)
    bump_epoch(db, org_id, "host tools key issued")
    return CreatedKey(kid, key, None)


def issue_host_key(db: Any, *, org_id: str, revoke_others: bool = True) -> Any:
    """Mint the Host tools key (the earlier ones end now unless revoke_others is False)."""
    from .migration import bind

    _ensure_account(db, org_id, HOST_TOOLS_PRINCIPAL, "Host tools",
                    "The boss command and the other tools on the BOSS host. Issued from the host "
                    "shell (boss identity host-key); kept in .env as BOSS_HOST_API_KEY.")
    perms = host_tools_permissions()
    role_id = _role(db, org_id, HOST_TOOLS_ROLE_KEY, "Host tools", perms, ("org",))
    bind(db, org_id, HOST_TOOLS_PRINCIPAL, role_id, "org", org_id, granted_by=HOST_CLI_PRINCIPAL,
         reason="host tools (boss identity host-key)")
    return _mint(db, org_id, HOST_TOOLS_PRINCIPAL, "boss host tools", perms, None,
                 revoke_others=revoke_others)


def _project_slug(project_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", project_id.lower()).strip("_")[:40] or "project"
    return slug + "_" + hashlib.sha256(project_id.encode()).hexdigest()[:6]


def issue_mcp_key(db: Any, *, org_id: str, project_id: str,
                  wanted: Iterable[str] = MCP_DEFAULT_CAPABILITIES) -> Any:
    """Mint the MCP key for ONE project: its own service account, bound in that project only to
    exactly what was asked for, the key pinned to the project. Earlier MCP keys of the same
    project end now."""
    from .migration import bind

    exists = db.execute(_sql("SELECT 1 FROM projects WHERE id = :p"), {"p": project_id}).first()
    if exists is None:
        raise ValueError(f"no project {project_id!r} on this install")
    perms = mcp_permissions(list(wanted) or MCP_DEFAULT_CAPABILITIES)
    pid = MCP_PRINCIPAL_PREFIX + _project_slug(project_id)
    _ensure_account(db, org_id, pid, f"MCP ({project_id})"[:200],
                    f"An agent client's MCP access to project {project_id} (make mcp-setup); kept in "
                    ".env as BOSS_MCP_API_KEY.")
    digest = hashlib.sha256(",".join(sorted(perms)).encode()).hexdigest()[:10]
    role_id = _role(db, org_id, f"mcp_{digest}", "MCP access", perms, ("project",))
    db.execute(_sql("DELETE FROM identity_role_bindings WHERE subject_type = 'principal' AND "
                    "subject_id = :p AND role_id <> :r"), {"p": pid, "r": role_id})
    bind(db, org_id, pid, role_id, "project", project_id, granted_by=HOST_CLI_PRINCIPAL,
         reason=f"MCP access to {project_id} (make mcp-setup)")
    return _mint(db, org_id, pid, f"mcp {project_id}"[:120], perms, [project_id],
                 revoke_others=True)
