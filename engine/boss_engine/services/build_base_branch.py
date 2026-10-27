"""THE BRANCH A BUILD STARTS FROM AND OPENS ITS PULL REQUEST AGAINST — decided once, here.

A build (code task) clones a base branch, cuts its work branch from it, and opens its PR
against it. Those two used to be decided apart, and neither looked at the project: the clone
took ``spec.base_branch or "main"`` (every API entry point wrote ``"main"`` when the form sent
nothing) and the PR took the repository's GitHub default. Project boss-platform binds
paramg/boss with ``default_branch = 'rel/rc10.10-kg'`` — the console's New build form shows
it — yet its builds cloned ``main`` (1,962 commits behind) and opened PRs #2/#3 against
``main`` (crun_23d665a9, crun_2f4eb5eb, 2026-10-26).

One resolver, used for BOTH the clone base and the PR base, in this order:

1. the task's explicit ``spec.base_branch`` (``SOURCE_SPEC``);
2. the project's ``github_repository_bindings.default_branch`` for that repo
   (``SOURCE_BINDING``);
3. the repository host's default branch (``SOURCE_HOST_DEFAULT``); when even that cannot be
   read, ``"main"`` (``SOURCE_FALLBACK``) — what every build used before.

One exception, for the PR only: a BOSS work branch (``boss/...``, e.g. a follow-up that builds
on its app's ``boss/code/<root>`` lineage branch) is a fine place to CLONE from but is never a
PR base — that PR targets the mainline the same steps 2-3 give.

An entry point that has no branch from its caller passes NONE (never ``"main"``), so the
project's branch is not shadowed by a placeholder.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

SOURCE_SPEC = "spec"
SOURCE_BINDING = "project_binding"
SOURCE_HOST_DEFAULT = "github_default"
SOURCE_FALLBACK = "fallback_main"

#: Branches BOSS itself creates (lineage, release, diagnostics): never a PR's base.
BOSS_BRANCH_PREFIX = "boss/"


@dataclass(frozen=True)
class BuildBase:
    clone: str          # the branch the build clones and cuts its work branch from
    clone_source: str
    pr: str             # the branch its pull request is opened against
    pr_source: str

    def as_event(self) -> dict:
        return {"base_branch": self.clone, "source": self.clone_source,
                "pr_base": self.pr, "pr_source": self.pr_source}


def _clean(value: Any) -> Optional[str]:
    branch = str(value or "").strip()
    return branch or None


def is_boss_branch(branch: Optional[str]) -> bool:
    return bool(branch) and str(branch).startswith(BOSS_BRANCH_PREFIX)


def binding_default_branch(connection, project_id: str, owner: str, repo: str) -> Optional[str]:
    """The project's configured branch for ``owner/repo``, or None (no binding, no branch)."""
    if not (project_id and owner and repo):
        return None
    try:
        row = connection.execute(
            text(
                "SELECT default_branch FROM github_repository_bindings "
                "WHERE project_id = :p AND lower(full_name) = lower(:fn) "
                "AND status = 'active' AND default_branch IS NOT NULL "
                "ORDER BY CASE role WHEN 'primary' THEN 0 ELSE 1 END LIMIT 1"
            ),
            {"p": project_id, "fn": f"{owner}/{repo}"},
        ).first()
    except Exception:  # noqa: BLE001 — an install without the table has no binding
        logger.debug("github_repository_bindings unreadable", exc_info=True)
        return None
    return _clean(row[0]) if row else None


def host_default_branch(adapter: Any) -> Optional[str]:
    """The repository host's default branch through whichever call the adapter has."""
    if adapter is None:
        return None
    if hasattr(adapter, "default_branch_sha"):
        value = adapter.default_branch_sha()[0]
    elif hasattr(adapter, "default_branch"):
        value = adapter.default_branch()
    else:
        return None
    return _clean(value) if isinstance(value, str) else None


def resolve_build_base(
    connection, *, project_id: str, owner: str, repo: str,
    explicit: Optional[str] = None,
    host_default: Optional[Callable[[], Optional[str]]] = None,
    run_id: Optional[str] = None,
) -> BuildBase:
    """The clone base and the PR base for a build of ``owner/repo`` in ``project_id``.

    ``host_default`` is called at most once, and only when neither the spec nor the project
    names a branch. Logs which source won.
    """
    def mainline() -> tuple[str, str]:
        branch = binding_default_branch(connection, project_id, owner, repo)
        if branch:
            return branch, SOURCE_BINDING
        if host_default is not None:
            try:
                branch = _clean(host_default())
            except Exception:  # noqa: BLE001 — an unreadable host default falls back as before
                logger.warning("base branch: could not read %s/%s's default branch",
                               owner, repo, exc_info=True)
            if branch:
                return branch, SOURCE_HOST_DEFAULT
        return "main", SOURCE_FALLBACK

    asked = _clean(explicit)
    if asked and not is_boss_branch(asked):
        base = BuildBase(asked, SOURCE_SPEC, asked, SOURCE_SPEC)
    else:
        line, source = mainline()
        base = (BuildBase(asked, SOURCE_SPEC, line, source) if asked
                else BuildBase(line, source, line, source))
    logger.info("base branch for %s/%s (project %s, run %s): clone %s (source: %s), "
                "PR base %s (source: %s)", owner, repo, project_id, run_id or "-",
                base.clone, base.clone_source, base.pr, base.pr_source)
    return base


def pr_base_for_run(connection, run: dict, adapter: Any) -> str:
    """The PR base for a STORED run, for every path that opens its PR after the pipeline (a
    redelivery, a gate override, a checks-failing PR): a release fix's own base, else the
    same answer the pipeline gave (resolve_build_base)."""
    from . import release_scope

    target = release_scope.of_run(run.get("finalize_ctx"))
    if target and target.get("pr_base"):
        return str(target["pr_base"])
    spec = run.get("spec") or {}
    owner, repo = spec.get("owner"), spec.get("repo")
    if not (owner and repo):
        full = str(getattr(adapter, "repository_full_name", "") or "")
        owner, _, repo = full.partition("/")
    return resolve_build_base(
        connection, project_id=run.get("project_id") or "", owner=owner or "", repo=repo or "",
        explicit=spec.get("base_branch"),
        host_default=lambda: host_default_branch(adapter), run_id=run.get("id")).pr
