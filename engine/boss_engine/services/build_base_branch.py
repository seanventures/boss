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

A DEFAULT BRANCH THAT MOVED IS FOLLOWED (BOSSAPP-1439). A repository's default branch can be
renamed (``master`` -> ``main``) or switched to another branch after BOSS recorded it. The
recorded branch then either no longer exists (every build failed at the clone) or is a stale
branch nobody merges to any more (builds quietly started from old code). When the caller can
ask the host (``host_default``, ``branch_exists``), the recorded branch is checked:

* gone from the host, and the host's default exists: the build follows the host's default
  (``SOURCE_MOVED``), the project's binding is updated to it, and the build says so in words;
* still there but no longer the host's default, and BOSS recorded it FROM the host (binding
  metadata ``default_branch_from = "host"``, written when the connection is tested): followed
  the same way. A branch a PERSON chose (``"person"``, or unmarked) is kept: a project bound to a
  release branch is not the host's default on purpose.

A spec that names a branch which no longer exists is followed the same way only when it named
the default of its day (the binding's recorded branch, or ``main`` / ``master``); any other
missing branch is the person's choice and fails at the clone as before.
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
SOURCE_MOVED = "default_moved"

#: Branch names that are the default of their day when a spec names them without a binding.
DEFAULT_NAMES = frozenset({"main", "master"})

#: Branches BOSS itself creates (lineage, release, diagnostics): never a PR's base.
BOSS_BRANCH_PREFIX = "boss/"


@dataclass(frozen=True)
class BuildBase:
    clone: str          # the branch the build clones and cuts its work branch from
    clone_source: str
    pr: str             # the branch its pull request is opened against
    pr_source: str
    #: What moved and what BOSS did about it, in words (SOURCE_MOVED), else None.
    note: Optional[str] = None
    moved_from: Optional[str] = None

    def as_event(self) -> dict:
        out = {"base_branch": self.clone, "source": self.clone_source,
               "pr_base": self.pr, "pr_source": self.pr_source}
        if self.note:
            out.update(note=self.note, moved_from=self.moved_from)
        return out


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


def _binding(connection, project_id: str, owner: str, repo: str) -> Optional[dict]:
    """The project's active binding row for ``owner/repo`` that names a branch:
    {id, default_branch, from} (``from``: who recorded the branch — "host", "person" or None)."""
    if not (project_id and owner and repo):
        return None
    try:
        with connection.begin_nested():
            row = connection.execute(
                text(
                    "SELECT id, default_branch, metadata->>'default_branch_from' AS recorded_from "
                    "FROM github_repository_bindings "
                    "WHERE project_id = :p AND lower(full_name) = lower(:fn) "
                    "AND status = 'active' AND default_branch IS NOT NULL "
                    "ORDER BY CASE role WHEN 'primary' THEN 0 ELSE 1 END LIMIT 1"
                ),
                {"p": project_id, "fn": f"{owner}/{repo}"},
            ).mappings().first()
    except Exception:  # noqa: BLE001 — an older table (no metadata): the branch alone
        branch = binding_default_branch(connection, project_id, owner, repo)
        return {"id": None, "default_branch": branch, "from": None} if branch else None
    if not row or not _clean(row["default_branch"]):
        return None
    return {"id": row["id"], "default_branch": _clean(row["default_branch"]),
            "from": row["recorded_from"]}


def moved_words(owner: str, repo: str, old: str, new: str, *, gone: bool) -> str:
    """What moved and what BOSS did, for the build's record and the project's activity."""
    why = (f"{old} no longer exists on {owner}/{repo}" if gone else
           f"{owner}/{repo}'s default branch is no longer {old}")
    return (f"The repository's default branch moved: {why}; its default is now {new}. BOSS "
            f"built from {new} and recorded {new} as the project's branch. If {new} is not the "
            "branch this project should build from, change it in Setup › Repositories.")


def _follow(connection, project_id: str, binding: Optional[dict], old: str, new: str) -> None:
    """Record the host's new default on the binding, once, with an event. Never raises."""
    if not binding or not binding.get("id") or binding.get("default_branch") != old:
        return
    try:
        import json as _json

        from .core import emit_event

        with connection.begin_nested():
            changed = connection.execute(
                text("UPDATE github_repository_bindings SET default_branch = :new, "
                     "metadata = metadata || CAST(:m AS JSONB), updated_at = now() "
                     "WHERE id = :id AND default_branch = :old"),
                {"new": new, "old": old, "id": binding["id"],
                 "m": _json.dumps({"default_branch_from": "host",
                                   "default_branch_moved_from": old})})
            if getattr(changed, "rowcount", 0):
                emit_event(connection, project_id, "github.default_branch_moved", "boss",
                           "github_repository_binding", binding["id"],
                           {"from": old, "to": new})
    except Exception:  # noqa: BLE001 — the build still follows the move; the record can wait
        logger.warning("base branch: could not record the moved default %s -> %s", old, new,
                       exc_info=True)


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
    branch_exists: Optional[Callable[[str], bool]] = None,
) -> BuildBase:
    """The clone base and the PR base for a build of ``owner/repo`` in ``project_id``.

    ``host_default`` is called at most once: when neither the spec nor the project names a
    branch, or to check a recorded branch that may have moved (``branch_exists``, BOSSAPP-1439).
    Logs which source won.
    """
    host: dict[str, Optional[str]] = {}

    def host_branch() -> Optional[str]:
        if "v" not in host:
            host["v"] = None
            if host_default is not None:
                try:
                    host["v"] = _clean(host_default())
                except Exception:  # noqa: BLE001 — unreadable: falls back as before
                    logger.warning("base branch: could not read %s/%s's default branch",
                                   owner, repo, exc_info=True)
        return host["v"]

    def exists(branch: str) -> Optional[bool]:
        if branch_exists is None:
            return None
        try:
            said = branch_exists(branch)
            return None if said is None else bool(said)
        except Exception:  # noqa: BLE001 — unknown is never "gone"
            return None

    def moved_to(branch: str, *, recorded_by_host: bool) -> Optional[tuple[str, bool]]:
        """(the host's default, whether `branch` is gone) when `branch` should be left for it."""
        if host_default is None:
            return None
        gone = exists(branch) is False
        # A branch a PERSON chose that is gone is theirs to replace (Setup › Repositories):
        # only a default of its day (read from the host, or main/master) is followed.
        if not (recorded_by_host or (gone and branch in DEFAULT_NAMES)):
            return None
        new = host_branch()
        if not new or new == branch or is_boss_branch(new):
            return None
        # Followed only to a branch the host confirms; a host that cannot be read changes
        # nothing (branch_exists answers False on any error, so "gone" alone is not enough).
        if branch_exists is not None and exists(new) is not True:
            return None
        return new, gone

    binding = _binding(connection, project_id, owner, repo)

    def mainline() -> tuple[str, str, Optional[str], Optional[str]]:
        branch = binding["default_branch"] if binding else None
        if branch:
            move = moved_to(branch, recorded_by_host=binding.get("from") == "host")
            if move:
                new, gone = move
                _follow(connection, project_id, binding, branch, new)
                return new, SOURCE_MOVED, moved_words(owner, repo, branch, new, gone=gone), branch
            return branch, SOURCE_BINDING, None, None
        branch = host_branch()
        if branch:
            return branch, SOURCE_HOST_DEFAULT, None, None
        return "main", SOURCE_FALLBACK, None, None

    asked = _clean(explicit)
    if asked and not is_boss_branch(asked):
        recorded = binding["default_branch"] if binding else None
        recorded_from_host = bool(binding) and binding.get("from") == "host"
        move = (moved_to(asked, recorded_by_host=recorded_from_host and asked == recorded)
                if asked == recorded or (not recorded and asked in DEFAULT_NAMES) else None)
        if move and move[1]:
            new, _gone = move
            _follow(connection, project_id, binding, asked, new)
            note = moved_words(owner, repo, asked, new, gone=True)
            base = BuildBase(new, SOURCE_MOVED, new, SOURCE_MOVED, note, asked)
        else:
            base = BuildBase(asked, SOURCE_SPEC, asked, SOURCE_SPEC)
    else:
        line, source, note, old = mainline()
        base = (BuildBase(asked, SOURCE_SPEC, line, source, note, old) if asked
                else BuildBase(line, source, line, source, note, old))
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


def lineage_clone(run_id: str, fctx: Optional[dict], resolved_clone: str,
                  branch_exists: Callable[[str], bool]) -> tuple[str, str]:
    """(work_branch, clone_branch) for a build of a lineage — the branch the build pushes to and
    the branch it clones. A later attempt (a retry, a repair, a follow-up) shares ONE branch
    derived from the root run and clones it when it exists, so it builds on the app's code;
    a first build clones ``resolved_clone`` (resolve_build_base). The pipeline asks this
    (governed_code_task_service) and so does every test that asserts where a build lands.
    A release fix or a pushed local branch is decided by the pipeline before this."""
    root = str((fctx or {}).get("root_run_id") or run_id)
    work_branch = f"boss/code/{root}"
    if root != run_id and branch_exists(work_branch):
        return work_branch, work_branch
    return work_branch, resolved_clone
