"""What a run of a multi-repo app may touch: EXACTLY its app's repositories.

Three layers, each enforced by something other than a prompt:

  1. the identity grant the person consents to (the consent sheet) names exactly the app's
     repositories (`required_repos`), never the project's or the organization's;
  2. every build of the set is its own run whose run token names ONE repository — its own — so
     GitHub mints an installation token for that repository alone (identity/runs.py, 1 hour,
     revoked when the run ends);
  3. `outside_run_grant`, called at every mint (identity/github_app.mint_for_run), refuses a
     repository that is not in the set's recorded grant (`repo_run_grants`), and a build of a set
     with no recorded grant at all (fail closed).

Read-only repositories (deploy config) are in the grant for the deploy plan and the cross-repo
reviewer, which read them with read-only tokens; no build ever targets one (components.validate).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from ..services.core import make_id
from .common import MultiRepoError, iso, jdump, jload, norm_repo, org_of_project, sql

TOKEN_LIFETIME = timedelta(hours=1)
_CT = re.compile(r"(?:^run_ct:|/ct:)([A-Za-z0-9_\-]+)")

#: What each access means on GitHub, in the consent sheet's words.
ACCESS_WORDS = {
    "write": "Read · Write on boss/* branches · Pull requests",
    "read": "Read only · for the deploy plan",
}
WRITE_PERMISSIONS = {"contents": "write", "pull_requests": "write", "metadata": "read"}
READ_PERMISSIONS = {"contents": "read", "metadata": "read"}


def required_repos(db: Any, project_id: str, app_key: str) -> list[dict[str, Any]]:
    """The app's repositories and the access each needs: the grant, exactly."""
    from . import components

    return components.app_repos(db, project_id, app_key)


def consent_details(db: Any, project_id: str, app_key: str, *, run_label: Optional[str] = None) -> dict[str, Any]:
    """What the consent sheet shows: the repositories and their access, the ones it cannot reach,
    and how the access works. Added to the identity 409 consent_required body as `multirepo`."""
    from . import components

    app = components.get(db, project_id, app_key)
    repos = required_repos(db, project_id, app_key)
    mine = {norm_repo(r["full_name"]) for r in repos}
    org_id = org_of_project(db, project_id) or ""
    try:
        with db.begin_nested():
            others = [str(r) for r in db.execute(sql(
                "SELECT full_name FROM github_repos WHERE org_id = :o ORDER BY lower(full_name)"),
                {"o": org_id}).scalars().all() if norm_repo(str(r)) not in mine]
    except Exception:  # noqa: BLE001
        others = []
    return {
        "app_key": app_key, "app_name": app["app_name"], "run_label": run_label,
        "repos": [{"full_name": r["full_name"], "access": r["access"], "components": r["components"],
                   "words": ACCESS_WORDS[r["access"]]} for r in repos],
        "unreachable": others,
        "how": f"GitHub App token limited to these {len(repos)} repositories",
        "lasts": "1 hour, renewed per step, ends with the run",
        "never": "push to a base branch, change settings, delete branches it did not create",
    }


def record(db: Any, *, project_id: str, app_key: str, change_set_id: Optional[str], identity_grant_id: Optional[str],
           consented_by: Optional[str], remember: bool = False) -> dict[str, Any]:
    """Record the grant a set runs under: its repositories are the app's, computed HERE from the
    components, never taken from a caller."""
    repos = required_repos(db, project_id, app_key)
    if not repos:
        raise MultiRepoError(409, "no_components", "This app has no components to grant access to.")
    gid = make_id("rgr")
    expires = datetime.now(timezone.utc) + (timedelta(days=30) if remember else timedelta(days=2))
    perms = {r["full_name"]: (WRITE_PERMISSIONS if r["access"] == "write" else READ_PERMISSIONS) for r in repos}
    db.execute(sql("INSERT INTO repo_run_grants (id, project_id, app_key, change_set_id, identity_grant_id, repos, "
                   "permissions, consented_by, remember, expires_at) VALUES (:id, :p, :a, :cs, :ig, CAST(:r AS JSONB), "
                   "CAST(:perm AS JSONB), :by, :rem, :exp)"),
               {"id": gid, "p": project_id, "a": app_key, "cs": change_set_id, "ig": identity_grant_id,
                "r": jdump([{"full_name": r["full_name"], "access": r["access"]} for r in repos]),
                "perm": jdump(perms), "by": consented_by, "rem": remember, "exp": expires})
    return get_for_change_set(db, change_set_id) if change_set_id else {"id": gid}


def get_for_change_set(db: Any, change_set_id: Optional[str]) -> Optional[dict[str, Any]]:
    if not change_set_id:
        return None
    row = db.execute(sql("SELECT * FROM repo_run_grants WHERE change_set_id = :c ORDER BY created_at DESC LIMIT 1"),
                     {"c": change_set_id}).mappings().first()
    if row is None:
        return None
    return {"id": row["id"], "app_key": row["app_key"], "repos": jload(row["repos"], []),
            "permissions": jload(row["permissions"], {}), "consented_by": row["consented_by"],
            "identity_grant_id": row["identity_grant_id"], "remember": bool(row["remember"]),
            "expires_at": iso(row["expires_at"]), "ended_at": iso(row["ended_at"]),
            "created_at": iso(row["created_at"]), "token_lifetime_minutes": int(TOKEN_LIFETIME.total_seconds() // 60)}


def end(db: Any, change_set_id: str) -> None:
    db.execute(sql("UPDATE repo_run_grants SET ended_at = COALESCE(ended_at, now()) WHERE change_set_id = :c"),
               {"c": change_set_id})


def code_task_of(identity_run_id: str) -> Optional[str]:
    found = _CT.findall(str(identity_run_id or ""))
    return found[-1] if found else None


def outside_run_grant(db: Any, identity_run_id: str, repos: Iterable[str]) -> bool:
    """True when this run belongs to a change set and asks for a repository outside the set's
    grant — or the set has no grant recorded (fail closed). False for every other run."""
    crun = code_task_of(identity_run_id)
    if not crun:
        return False
    try:
        with db.begin_nested():
            cs = db.execute(sql("SELECT p.change_set_id, s.mode FROM change_set_prs p JOIN repo_change_sets s "
                                "ON s.id = p.change_set_id WHERE p.code_task_run_id = :r LIMIT 1"),
                            {"r": crun}).mappings().first()
    except Exception:  # noqa: BLE001 — before change sets exist
        return False
    if cs is None or cs["mode"] != "multi":
        return False
    grant = get_for_change_set(db, cs["change_set_id"])
    if grant is None or grant.get("ended_at"):
        return True
    allowed = {norm_repo(r["full_name"]) for r in grant["repos"] if r.get("access") == "write"}
    return any(norm_repo(r) not in allowed for r in repos)


def assert_within(db: Any, change_set_id: str, full_name: str, *, access: str = "write") -> None:
    """For BOSS's own control-plane calls on a set (PR bodies, merges, reverts): the repository
    must be in the set's grant with at least this access."""
    grant = get_for_change_set(db, change_set_id)
    if grant is None:
        raise MultiRepoError(403, "no_grant", "This change has no recorded grant.")
    for r in grant["repos"]:
        if norm_repo(r["full_name"]) == norm_repo(full_name) and (access == "read" or r.get("access") == "write"):
            return
    raise MultiRepoError(403, "repo_outside_app", f"{full_name} is outside this change's grant.")
