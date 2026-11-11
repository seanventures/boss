"""GOVERNED GIT ACTIONS ON A CHANGE (BOSSAPP-996). The operator: "provide few important git
commands in the ui, to pull, resolve, showing history etc.. we dont have to go to CLI to execute
these things."

Two actions, one seam, no new git path:

  sync (pull)   merge the base INTO the change's branch — `update_pull_request_branch`, the same
                call on `LocalGitAdapter` and `GitHubAdapter` (GitHub's "Update branch").
  restack       put the branch back on its base after a squash merge — `last_merged_pull_request`
                + `restack_after_merge`, the machinery PR #31 built for pr_follow_up. A branch in
                this state can NEVER merge again by itself (every shared file an add/add
                conflict), and syncing it would only entrench the conflict, so the preview names
                restack first when it applies.

`preview` is the read: where the branch stands (ahead/behind), whether the base merges in clean,
and the conflicting files when it does not — for a local repository from a throwaway merge
(local_repo.merge_preview); GitHub says only CLEAN or DIRTY, so the file list is honest about
being unknown there. Neither action runs ambiently: `propose` writes a blocking decision
(action `change_sync_branch`), and a person's approval carries it out here
(`on_decision_approved`, from DecisionService._handle_approved_follow_on). A refusal — a real
content conflict — raises, so the decision STAYS pending and the page shows the files; fixing
those needs an agent (Request changes), not a git call.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

log = logging.getLogger(__name__)

MODES = ("update", "restack")


class ChangeSyncError(RuntimeError):
    """A refusal, in words a person can act on."""

    def __init__(self, message: str, status: int = 409, error: str = "sync_refused"):
        super().__init__(message)
        self.status = int(status)
        self.error = error

    def body(self) -> dict[str, Any]:
        return {"error": self.error, "message": str(self)}


def _local_adapter(settings: Any, project_id: str, repository: str, engine: Any = None) -> Any:
    from .local_repo_host import LocalGitAdapter

    owner, _, name = str(repository).partition("/")
    return LocalGitAdapter(settings, owner=owner, repo=name, project_id=project_id, engine=engine)


def _adapter_of(src: Any) -> Any:
    """The repository-host adapter behind a change_review source — the ONE seam (PR #31)."""
    if src.kind == "local":
        return _local_adapter(src.settings, src.project_id, src.row["repository"],
                              engine=src.engine)
    return src.adapter


def _restack_of(adapter: Any, branch: str) -> Optional[dict[str, Any]]:
    """The merged change set/PR a restack would replay past, when one is on record."""
    try:
        return adapter.last_merged_pull_request(branch)
    except Exception as error:  # noqa: BLE001 — a preview still answers without it
        log.debug("change_sync: last_merged_pull_request failed: %s", error)
        return None


def preview(c: Any, settings: Any, src: Any) -> dict[str, Any]:
    """Where the change's branch stands against its base, and which action would move it.

    A GitHub host that does not answer gives {"unreachable": true} and a 200 — the panel simply
    does not draw, the way the page's own stale read behaves — never a 500 on a page view."""
    from . import local_repo

    if src.kind != "local":
        try:
            return _github_preview(src)
        except Exception as error:  # noqa: BLE001 — a page view never fails on a preview
            log.debug("change_sync: GitHub preview unreachable: %s", error)
            return {"number": src.number, "source": "github", "state": "unknown",
                    "unreachable": True, "ahead": 0, "behind": 0, "mergeable": None,
                    "conflicts": [], "conflicts_known": False, "restack": None,
                    "why": "GitHub did not answer."}
    adapter = _adapter_of(src)
    snap = adapter.pull_request_snapshot(src.number)
    branch, base = str(snap["head_ref"]), str(snap["base_ref"])
    out: dict[str, Any] = {"number": src.number, "source": "local", "branch": branch,
                           "base": base, "state": snap["state"],
                           "merged": bool(snap.get("merged"))}
    if snap["state"] != "open":
        return {**out, "ahead": 0, "behind": 0, "mergeable": None, "conflicts": [],
                "conflicts_known": True, "restack": None, "why_closed": "The change is not open."}
    git_dir = adapter.git_dir
    stand = local_repo.counts(git_dir, branch, base)
    try:
        look = local_repo.merge_preview(git_dir, branch, base)
    except local_repo.LocalRepoError as error:
        raise ChangeSyncError(str(error), 422, "sync_preview_failed") from error
    out.update({**stand, "mergeable": bool(look["mergeable"]),
                "conflicts": look["conflicts"], "conflicts_known": True,
                "restack": _restack_offer(adapter, branch)})
    return out


def _github_preview(src: Any) -> dict[str, Any]:
    adapter = src.adapter
    snap = adapter.pull_request_snapshot(src.number)
    branch, base = str(snap["head_ref"]), str(snap["base_ref"])
    out: dict[str, Any] = {"number": src.number, "source": "github", "branch": branch,
                           "base": base, "state": snap["state"], "merged": bool(snap.get("merged"))}
    if snap["state"] != "open":
        return {**out, "ahead": 0, "behind": 0, "mergeable": None, "conflicts": [],
                "conflicts_known": True, "restack": None, "why_closed": "The change is not open."}
    # GitHub says CLEAN or DIRTY; it does not name the conflicting files. Saying so beats
    # inventing a list (the console words it accordingly).
    payload = adapter._request(  # noqa: SLF001 — the generic-reader path ci_gate also uses
        "GET", f"/repos/{adapter.repository_full_name}/compare/"
               f"{branch_quote(base)}...{branch_quote(branch)}") or {}
    mergeable = snap.get("mergeable")
    out.update({"ahead": int(payload.get("ahead_by") or 0),
                "behind": int(payload.get("behind_by") or 0),
                "mergeable": bool(mergeable) if mergeable is not None else None,
                "conflicts": [], "conflicts_known": False,
                "restack": _restack_offer(adapter, branch)})
    return out


def _restack_offer(adapter: Any, branch: str) -> Optional[dict[str, Any]]:
    """A branch whose merged work was squashed cannot merge again without a restack: the offer
    names the merged change and the head it replays past."""
    merged = _restack_of(adapter, branch)
    if merged and merged.get("head_sha"):
        return {"number": merged["number"], "merged_head_sha": merged["head_sha"],
                "merged_at": merged.get("merged_at") or ""}
    return None


def branch_quote(ref: str) -> str:
    from urllib.parse import quote

    return quote(str(ref), safe="")


def propose(connection: Any, settings: Any, src: Any, *, mode: str, actor: str) -> str:
    """A blocking decision asking to run the action; approving it runs `on_decision_approved`."""
    from .core import create_blocking_decision

    if mode not in MODES:
        raise ChangeSyncError(f"mode must be one of {', '.join(MODES)}.", 422, "bad_mode")
    try:
        adapter = _adapter_of(src)
        snap = adapter.pull_request_snapshot(src.number)
    except ChangeSyncError:
        raise
    except Exception as error:  # noqa: BLE001 — a host that does not answer is a refusal in words
        raise ChangeSyncError("The repository host did not answer; try again when it is back.",
                              503, "host_unreachable") from error
    if snap["state"] != "open":
        raise ChangeSyncError(f"Change #{src.number} is not open.", 409, "not_open")
    branch, base = str(snap["head_ref"]), str(snap["base_ref"])
    name = "change" if src.kind == "local" else "PR"
    rec: dict[str, Any] = {"action": "change_sync_branch", "mode": mode,
                           "project_id": src.project_id, "source": src.kind,
                           "repository": adapter.repository_full_name, "number": src.number,
                           "branch": branch, "base": base, "head_sha": snap.get("head_sha")}
    if src.kind == "github":
        rec["binding_id"] = src.binding_id
    if mode == "restack":
        merged = _restack_of(adapter, branch)
        if not (merged and merged.get("head_sha")):
            raise ChangeSyncError(f"No merged {name} from {branch} is on record: there is "
                                  "nothing to restack past.", 409, "nothing_to_restack")
        rec["merged_head_sha"] = merged["head_sha"]
        question = (f"Restack {name} #{src.number}'s branch {branch} onto {base}: replay only "
                    f"the commits made after #{merged['number']} merged?")
    else:
        question = (f"Sync {name} #{src.number}'s branch {branch} with {base}: merge "
                    f"{base} in?")
    return create_blocking_decision(connection, src.project_id, None, question, rec,
                                    {}, "change_sync", risk_tier=2)


def on_decision_approved(connection: Any, settings: Any, row: Mapping[str, Any],
                         actor: str) -> dict[str, Any]:
    """Run the approved action, through the same adapter seam. Raises ChangeSyncError on a real
    conflict (the decision stays pending; the page lists the conflicting files)."""
    from .audit import emit_event
    from .change_review import github_adapter_for

    rec = dict(row.get("recommendation") or {})
    project_id = str(row.get("project_id") or rec.get("project_id") or "")
    number = int(rec.get("number") or 0)
    branch, base = str(rec.get("branch") or ""), str(rec.get("base") or "")
    mode = str(rec.get("mode") or "update")
    if rec.get("source") == "github":
        adapter = github_adapter_for(connection, settings, project_id,
                                     str(rec.get("binding_id") or ""))
    else:
        adapter = _local_adapter(settings, project_id, str(rec.get("repository") or ""))
    snap = adapter.pull_request_snapshot(number)
    if snap["state"] != "open":
        raise ChangeSyncError(f"Change #{number} is no longer open.", 409, "not_open")
    if mode == "restack":
        result = adapter.restack_after_merge(branch, str(rec.get("merged_head_sha") or ""),
                                             base=base, number=number)
        if not result.get("restacked"):
            why = str(result.get("why") or "it was refused")
            raise ChangeSyncError(f"The restack changed nothing: {why}. Fixing this needs an "
                                  "agent (Request changes), not a git call.",
                                  409, "restack_refused")
    else:
        if not adapter.update_pull_request_branch(number):
            said = f"{base} does not merge cleanly into {branch}"
            if rec.get("source") != "github":
                from . import local_repo

                look = local_repo.merge_preview(adapter.git_dir, branch, base)
                if look["conflicts"]:
                    said += f": {', '.join(look['conflicts'][:8])} conflict"
            raise ChangeSyncError(f"{said}. Fixing this needs an agent (Request changes), "
                                  "not a git call.", 409, "sync_conflict")
        result = {"synced": True, "base": base}
    emit_event(connection, project_id, "change.sync_branch", actor, "change_set",
               f"{rec.get('repository')}#{number}", {"mode": mode, "branch": branch,
                                                     "base": base, "decision_id": row.get("id"),
                                                     **{k: v for k, v in result.items()
                                                        if k in ("restacked", "paths", "synced")}})
    return result


__all__ = ["MODES", "ChangeSyncError", "on_decision_approved", "preview", "propose"]
