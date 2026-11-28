"""A BUILD NEVER STARTS FROM A CHECKOUT THAT HOLDS NO APP WHEN THE REPOSITORY HAS ONE (AR-54).

A workflow's build step (`validate_and_fix` of "API onboarding", crun_671a765b) was started with
no base branch, so it cloned the project's mainline: a README and a .gitignore, because the
app BOSS built earlier lives on its unmerged `boss/code/<root>` branches. The agent found no app
and no reachable remote, wrote `cmd/gwlint` and a `go.mod` from scratch and the step succeeded:
a second, parallel app, reported as done. Builds started from the build page pass the lineage
branch and were never affected.

This is the one check every launcher shares, run on the provisioned checkout before any model
call: the tree holds no application files, yet BOSS has built work in this very repository
before. The build fails with the branches that hold it, never a from-scratch build.

WHOSE BUILDS COUNT (app_scope, ENT-FEAT-01): the app's own. Neighborhood Board, started as a new
app into the project that already held Recipe Box (one repository, Recipe Box's change still open
on its branch), was refused here because the search looked at every build of the repository. A
build that declares a new app now starts from the default branch beside the other apps; a build of
the same recorded app is still refused; only a build that names no app is checked against every
app of the repository (the AR-54 case).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import text

#: Files that are not an app: a checkout holding only these is empty for building purposes.
_NOT_APP = ("readme", "license", "licence", "notice", "contributing", "changelog", ".gitignore",
            ".gitattributes", ".editorconfig")


def app_files(root: Path | str) -> Optional[list[str]]:
    """The tracked files of the checkout that are application files; None when `root` is not a git
    checkout (nothing to prove). Read-only git on the run's tree (git_safety.tree_git)."""
    from .git_safety import tree_git

    try:
        got = tree_git(root, "ls-files", timeout=30)
    except Exception:  # noqa: BLE001
        return None
    if got is None or got.returncode != 0:
        return None
    out = []
    for rel in got.stdout.splitlines():
        low = rel.lower()
        if low.startswith(".github/") or low.startswith("docs/"):
            continue
        if "/" not in rel and low.startswith(_NOT_APP):
            continue
        out.append(rel)
    return out


def _existing(rows, branch_exists) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        branch = f"boss/code/{row['root']}"
        try:
            if branch_exists is not None and not branch_exists(branch):
                continue
        except Exception:  # noqa: BLE001 — cannot tell: it counts
            pass
        out.append(dict(row))
    return out


_BUILT = ("SELECT id, COALESCE(finalize_ctx->>'root_run_id', id) AS root, pull_request_url, "
          "finalize_ctx->>'app_id' AS app_id "
          "FROM code_task_runs WHERE id <> :r AND status = 'completed' "
          "AND COALESCE(finalize_ctx->>'root_run_id', id) <> :root "
          "AND jsonb_typeof(state->'impl_files') = 'array' "
          "AND jsonb_array_length(state->'impl_files') > 0 ")


def earlier_builds(connection, *, owner: str, repo: str, run_id: str, root_run: str,
                   app_id: str = "", new_app: bool = False,
                   branch_exists: Optional[Callable[[str], bool]] = None) -> list[dict[str, Any]]:
    """Completed builds that produced files and whose branch still exists, newest first, not
    this build's own lineage.

    SCOPED BY APP (app_scope): a build of THIS app (the same recorded app id) always counts. A
    build that declared itself a NEW app (app_scope.declared_new_app) is never stopped by another
    app's branch: it starts from the default branch beside them (ENT-FEAT-01, Neighborhood Board
    beside Recipe Box). Only a build that names no app — which app it is, is exactly what is
    unknown — counts every app built in this repository (AR-54, `repository_builds`)."""
    from .app_scope import same_app_sql, same_app_params

    root = root_run or run_id
    rows = []
    if app_id:
        rows = connection.execute(text(
            _BUILT + "AND " + same_app_sql() + " ORDER BY created_at DESC LIMIT 20"),
            {"r": run_id, "root": root,
             **same_app_params(owner=owner, repo=repo, app_id=app_id, root=root)}
        ).mappings().all()
    if not rows and not new_app:
        rows = repository_builds(connection, owner=owner, repo=repo, run_id=run_id, root=root)
    return _existing(rows, branch_exists)


def repository_builds(connection, *, owner: str, repo: str, run_id: str, root: str) -> list:
    """Every app's completed builds in owner/repo (any project that works in it): ONLY for a
    build that names no app (earlier_builds), where any of them may be the app it meant."""
    return connection.execute(text(
        _BUILT + "AND spec->>'owner' = :o AND spec->>'repo' = :n "
        "ORDER BY created_at DESC LIMIT 20"),
        {"r": run_id, "root": root, "o": owner, "n": repo}).mappings().all()


def problem(connection, root: Path | str, *, owner: str, repo: str, run_id: str,
            base_branch: str, root_run: str = "", app_id: str = "", new_app: bool = False,
            branch_exists: Optional[Callable[[str], bool]] = None) -> Optional[str]:
    """Why this checkout cannot be built on, or None. Words a person can act on.

    Follow-ups (not handled here): a micro-VM stage clones inside its guest, and a multi-repo
    change set clones several repositories; neither runs this check yet."""
    found = app_files(root)
    if found is None or found:
        return None
    try:
        earlier = earlier_builds(connection, owner=owner, repo=repo, run_id=run_id,
                                 root_run=root_run, app_id=app_id, new_app=new_app,
                                 branch_exists=branch_exists)
    except Exception:  # noqa: BLE001 — a check that cannot read the history never blocks
        return None
    if not earlier:
        return None  # a genuinely new app: an empty repository is where it starts
    roots = []
    for row in earlier:
        branch = f"boss/code/{row['root']}"
        if branch not in roots:
            roots.append(branch)
    where = base_branch or "the default branch"
    if app_id and any(str(row.get("app_id") or "") == app_id for row in earlier):
        return (f"The checkout of {repo} (branch {base_branch or 'default'}) holds no application "
                "files, but BOSS has already built this app in this repository: its code is on "
                f"{', '.join(roots[:3])}, not merged into {where}. "
                "Building here would write a second app from scratch beside it, so this build "
                "stopped before planning. Merge that change, or start this work as a follow-up of "
                "that build (it builds on its branch).")
    return (f"The checkout of {repo} (branch {base_branch or 'default'}) holds no application "
            "files, but BOSS has already built an app in this repository: its code is on "
            f"{', '.join(roots[:3])}, not merged into {where}, and this build names no app. "
            "Building here could write a second app from scratch beside it, so this build "
            "stopped before planning. To change that app, start this work as a follow-up of "
            "that build (it builds on its branch); to build a different app, start it as a new "
            "app.")
