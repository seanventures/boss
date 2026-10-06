"""Just-in-time consent (IDENTITY.md §7.3 2): starting work that needs more authority than the
person's grants cover answers `409 consent_required` with what the console's consent sheet shows.
Owned by S3.

    raise ConsentRequired  ->  409 {"error": "consent_required", "task", "task_run_id", "agents",
                                    "permissions", "projects", "github", "durations", "max_days"}

The sheet then creates a grant (POST /v1/identity/me/delegations with `kind: "task"` — bound to
`task_run_id`, a `pending:` id the next matching run CLAIMS — or `kind: "standing"`), and the
console retries the start. Only a person in a human session is ever asked; system work, legacy
callers and an install with no Passports are unaffected (`require_authority` returns None).
"""
from __future__ import annotations

import secrets
from typing import Any, Iterable, Optional

from . import catalogue
from .lineage import Acting

try:  # the API renders it as a 409 even before the lead installs the flat-body handler
    from fastapi import HTTPException as _Base
except Exception:  # noqa: BLE001 — the engine stays importable without FastAPI
    _Base = Exception  # type: ignore[assignment,misc]


class ConsentRequired(_Base):  # type: ignore[misc,valid-type]
    def __init__(self, body: dict[str, Any]):
        self.body = body
        if _Base is Exception:
            super().__init__("consent_required")
        else:
            super().__init__(status_code=409, detail=body)


GITHUB_LABELS = {"contents": "code", "pull_requests": "pull requests", "issues": "issues", "workflows": "workflows",
                 "checks": "checks", "statuses": "statuses", "actions": "actions"}


def _github_summary(needs: dict[str, str], pattern: str) -> str:
    parts = [f"{GITHUB_LABELS.get(k, k)} {v}" for k, v in sorted(needs.items()) if k != "metadata"]
    return ", ".join(parts) + (f" · pushes only to {pattern}" if needs.get("contents") == "write" else "")


def require_authority(*, org_id: Optional[str], project_id: Optional[str], role_keys: Iterable[str],
                      permissions: Iterable[str], repos: Iterable[str] = (), task: str, run_id: Optional[str],
                      github_needs: Optional[dict[str, str]] = None, db: Any) -> Optional[Acting]:
    """The Acting (person + covering grant) the work runs under, or ConsentRequired.
    None when no person in a human session is starting it, or the agents have no Passports."""
    from . import grants, lineage, passports, runs

    ctx = lineage.current_context()
    if ctx is None or not ctx.is_human_session:
        return None
    agents = []
    for rk in role_keys:
        aid = runs.agent_for_role(db, project_id, rk)
        p = passports.get(aid, db=db) if aid else None
        if p is None:
            return None                                   # identity not seeded for this agent yet
        agents.append(p)
    needed = frozenset(k for k in permissions if catalogue.is_known(k) and catalogue.meta(k).delegable)
    repos = list(repos)
    grant = grants.find_covering(ctx.principal.id, agent_ids=[p.agent_id for p in agents], permissions=needed,
                                 project_id=project_id, repos=repos, run_id=run_id, db=db)
    if grant is not None:
        return Acting(principal_id=ctx.principal.id, org_id=ctx.principal.org_id, grant_id=grant.id, run_id=run_id)
    pol = grants.policy_for(ctx.principal.org_id, db=db)
    needs = dict(github_needs or {"contents": "write", "pull_requests": "write"})
    raise ConsentRequired({
        "error": "consent_required",
        "task": task[:200],
        "task_run_id": grants.PENDING_PREFIX + secrets.token_urlsafe(12),
        "agents": [{"id": p.agent_id, "name": _name(db, p.agent_id), "role_key": p.role_key} for p in agents],
        "permissions": sorted(needed),
        "projects": [project_id] if project_id else [],
        "github": ({"repo": repos[0], "repos": repos, "permissions": needs,
                    "summary": _github_summary(needs, "boss/*")} if repos else None),
        "durations": [{"kind": "task", "label": "For this build only", "days": None},
                      {"kind": "standing", "label": f"For {int(pol.delegation_default_days)} days",
                       "days": int(pol.delegation_default_days)}],
        "max_days": int(pol.delegation_max_days),
    })


def _name(db: Any, principal_id: str) -> str:
    from sqlalchemy import text

    return str(db.execute(text("SELECT display_name FROM identity_principals WHERE id = :i"),
                          {"i": principal_id}).scalar() or principal_id)


#: What a code task's builder needs (the same scope the run token is minted with).
CODE_TASK_PERMISSIONS = frozenset({"code_task:read", "code_task:steer", "repository:read", "github:read", "github:sync"})
