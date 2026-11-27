"""WHICH EARLIER BUILDS ARE THIS APP'S — the one answer for every lookup of a build's history.

One project, two apps, one repository (ENT-FEAT-01): Recipe Box was built into
`local_northgate/northgate` and its change was still open on `boss/code/<recipe root>`. Neighborhood
Board, started as a NEW app into the same project, cloned the repository's main (README only) and
was stopped before planning: the empty-checkout guard (build_checkout) searched earlier completed
builds BY REPOSITORY, found Recipe Box's branch and refused to "write a second app from scratch
beside it". The two runs carried distinct recorded app ids (`finalize_ctx.app_id`); the lookup never
asked for them.

An app's history is its own: its builds, branches, changes, previews, retries and repairs. A run's
app is
  * its LINEAGE: every attempt under one root run (`finalize_ctx.root_run_id`, else its own id);
  * every run stamped with the same APP ID (`finalize_ctx.app_id`, naming.stable_app_id: project,
    repository and app name hashed together, so it never reaches another project's app).
Repository and owner are always part of the scope as well: an app lives in one repository here.

A lookup over code_task_runs that scopes by repository alone, where the run's app is known, goes
through `same_app_sql` / `same_app_params` instead (engine/tests/test_two_apps_in_one_repository.py
fails on a new one). `declared_new_app` is what a run says when the person asked for a NEW app
(New work › Create a new app, a template's New app, create_code_task new_app): such a build starts
from the default branch and no other app's unmerged branch is a reason to stop it.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

#: finalize_ctx key a build carries when the person asked for a new app (never inferred).
NEW_APP_KEY = "new_app"

#: The rows of code_task_runs that are the same app as (:app_id, :app_root) in (:app_owner,
#: :app_repo). `alias` qualifies the columns ("r." for `FROM code_task_runs r`).
_SAME_APP = (
    "({a}spec->>'repo' = :app_repo AND COALESCE({a}spec->>'owner', '') = :app_owner "
    "AND ((:app_id <> '' AND {a}finalize_ctx->>'app_id' = :app_id) "
    "     OR COALESCE({a}finalize_ctx->>'root_run_id', {a}id) = :app_root))")


def same_app_sql(alias: str = "") -> str:
    """The WHERE fragment selecting the runs of one app (bind it with same_app_params)."""
    return _SAME_APP.format(a=f"{alias}." if alias and not alias.endswith(".") else alias)


def same_app_params(*, owner: str, repo: str, app_id: str = "", root: str = "") -> dict[str, str]:
    """The binds of same_app_sql. With neither an app id nor a root nothing matches."""
    return {"app_owner": str(owner or ""), "app_repo": str(repo or ""),
            "app_id": str(app_id or ""), "app_root": str(root or "")}


def declared_new_app(fctx: Optional[Mapping[str, Any]]) -> bool:
    """The person asked for a NEW app (only ever written by a door that asked them)."""
    return bool(isinstance(fctx, Mapping) and fctx.get(NEW_APP_KEY) is True)


def a_person_asked(auth: Any) -> bool:
    """May this request's `new_app` declare a new app? A person's own request does; an agent's
    does not — a non-person principal, or any request carrying a run token (an agent reaching an
    HTTP door through its run's bridge). A request with no identity at all (an install without
    the identity layer, where only the console's people reach these doors) counts as a person."""
    if auth is None:
        return True
    principal = getattr(auth, "principal", None)
    return bool(principal is not None and getattr(principal, "is_person", False)
                and getattr(auth, "run_token", None) is None)
