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
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import text

#: Files that are not an app: a checkout holding only these is empty for building purposes.
_NOT_APP = ("readme", "license", "licence", "notice", "contributing", "changelog", ".gitignore",
            ".gitattributes", ".editorconfig")


def app_files(root: Path | str) -> Optional[list[str]]:
    """The tracked files of the checkout that are application files; None when `root` is not a git
    checkout (nothing to prove)."""
    try:
        got = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True,
                             timeout=30)
    except Exception:  # noqa: BLE001
        return None
    if got.returncode != 0:
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


def earlier_builds(connection, *, project_id: str, owner: str, repo: str,
                   run_id: str) -> list[dict[str, Any]]:
    """Earlier builds of this repository in this project that produced files: newest first."""
    rows = connection.execute(text(
        "SELECT id, COALESCE(finalize_ctx->>'root_run_id', id) AS root, pull_request_url "
        "FROM code_task_runs WHERE project_id = :p AND id <> :r "
        "AND spec->>'owner' = :o AND spec->>'repo' = :n "
        "AND jsonb_typeof(state->'impl_files') = 'array' "
        "AND jsonb_array_length(state->'impl_files') > 0 "
        "ORDER BY created_at DESC LIMIT 20"),
        {"p": project_id, "r": run_id, "o": owner, "n": repo}).mappings().all()
    return [dict(r) for r in rows]


def problem(connection, root: Path | str, *, project_id: str, owner: str, repo: str,
            run_id: str, base_branch: str) -> Optional[str]:
    """Why this checkout cannot be built on, or None. Words a person can act on."""
    found = app_files(root)
    if found is None or found:
        return None
    try:
        earlier = earlier_builds(connection, project_id=project_id, owner=owner, repo=repo,
                                 run_id=run_id)
    except Exception:  # noqa: BLE001 — a check that cannot read the history never blocks
        return None
    if not earlier:
        return None  # a genuinely new app: an empty repository is where it starts
    roots = []
    for row in earlier:
        branch = f"boss/code/{row['root']}"
        if branch not in roots:
            roots.append(branch)
    return (f"The checkout of {repo} (branch {base_branch or 'default'}) holds no application "
            "files, but BOSS has already built this app in this repository: its code is on "
            f"{', '.join(roots[:3])}, not merged into {base_branch or 'the default branch'}. "
            "Building here would write a second app from scratch beside it, so this build "
            "stopped before planning. Merge that change, or start this work as a follow-up of "
            "that build (it builds on its branch).")
