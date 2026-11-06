"""A person acting on a subject takes it over from the agents (design §4 L4; spec §4.2).

THE CODEX BUG BOSS MUST NOT HAVE (openai/codex #57009: "keeps firing after I took over")
---------------------------------------------------------------------------------------
When a person pushes to the PR an agent is fixing, comments on it, reviews it, or presses Stop
on the run an agent started, the agent must stop at once. The board already knows how
(`board.on_person_action` / `take_over`: the agent's lease is replaced by the person's and its
next `assert_lease` raises LeaseLost). This module is the WIRING: the GitHub webhook and the two
cancel paths (a workflow run, a build) call it, and it names the subject the way the board does
(`pr:owner/repo#41`, `run:<id>`, `build:<id>`, plus any item linked to that run).

Only PEOPLE take over. BOSS's own bot, a GitHub App/Actions bot, the worker and an agent are not
a person acting; their events change nothing here. Never raises into its caller: a webhook or a
Stop must not fail because the ambient board could not be updated.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import board

log = logging.getLogger(__name__)

#: Actors that are BOSS itself, never a person.
SYSTEM_ACTORS = frozenset({"", "boss", "system", "control_plane", "worker", "scheduler",
                           "budget_governor", "policy", "shepherd", "autopilot", "github",
                           "self_fix", "metatron"})

_WHAT = {"pull_request": "updated the pull request", "pull_request_review": "reviewed the PR",
         "pull_request_review_comment": "commented on the PR's code",
         "issue_comment": "commented on the PR", "push": "pushed to the branch"}


def is_person(actor: Any) -> bool:
    raw = str(actor or "").strip()
    low = raw.lower()
    if low in SYSTEM_ACTORS or low.startswith(("agent:", "worker", "wkr_", "boss:", "policy:",
                                               "squad:")):
        return False
    return not low.endswith("[bot]")


def _take(connection: Connection, *, project_id: str, refs: Iterable[str], actor: str,
          what: str, run_id: Optional[str] = None) -> list[str]:
    taken: list[str] = []
    for ref in dict.fromkeys(r for r in refs if r):
        taken += board.on_person_action(connection, project_id=project_id, subject_ref=ref,
                                        actor=actor, what=what)
    if run_id:
        linked = connection.execute(text(
            "SELECT w.id FROM work_item_links l JOIN work_items w ON w.id = l.work_item_id "
            "WHERE l.project_id = :p AND l.kind IN ('workflow_run', 'code_task') AND l.ref = :r "
            "AND w.lifecycle = ANY(:live) AND COALESCE(w.lease_kind, '') <> 'person'"),
            {"p": project_id, "r": run_id, "live": list(board.LIVE)}).scalars().all()
        for item_id in linked:
            if item_id not in taken:
                board.take_over(connection, item_id, actor=actor, reason=what)
                taken.append(item_id)
    return taken


def on_github_event(connection: Connection, *, project_id: Optional[str], event: str,
                    payload: Mapping[str, Any], bot_login: str = "") -> list[str]:
    """A GitHub delivery by a person on a PR or branch an ambient item is about: take it over.
    Returns the work item ids taken over (empty for bots, BOSS's own App, other subjects)."""
    if not project_id:
        return []
    sender = payload.get("sender") or {}
    login = str(sender.get("login") or "")
    if (not login or str(sender.get("type") or "") == "Bot"
            or (bot_login and login.casefold() == bot_login.strip().casefold())
            or not is_person(login)):
        return []
    repo = str((payload.get("repository") or {}).get("full_name") or "")
    refs: list[str] = []
    if event in ("pull_request", "pull_request_review", "pull_request_review_comment"):
        number = (payload.get("pull_request") or {}).get("number") or payload.get("number")
        if repo and number:
            refs.append(f"pr:{repo}#{int(number)}")
    elif event == "issue_comment":
        issue = payload.get("issue") or {}
        if repo and issue.get("pull_request") and issue.get("number"):
            refs.append(f"pr:{repo}#{int(issue['number'])}")
    elif event == "push":
        ref = str(payload.get("ref") or "")
        if repo and ref.startswith("refs/heads/"):
            refs.append(f"branch:{repo}:{ref[len('refs/heads/'):]}")
    if not refs:
        return []
    return _safe(connection, project_id=project_id, refs=refs, actor=f"github:{login}",
                 what=_WHAT.get(event, "acted on it"))


def on_run_cancelled(connection: Connection, *, project_id: str, run_id: str, actor: str,
                     kind: str = "workflow_run") -> list[str]:
    """A person pressed Stop on a run or build: every live ambient item about it, or linked to
    it, is taken over (the agent stops; nothing restarts it behind the person's back)."""
    if not is_person(actor):
        return []
    prefix = "build" if kind == "code_task" else "run"
    return _safe(connection, project_id=project_id, refs=[f"{prefix}:{run_id}"], actor=actor,
                 what="stopped the build" if kind == "code_task" else "stopped the run",
                 run_id=run_id)


def _safe(connection: Connection, **kwargs: Any) -> list[str]:
    try:
        with connection.begin_nested():
            return _take(connection, **kwargs)
    except Exception:  # noqa: BLE001 — a Stop or a webhook never fails on the ambient board
        log.warning("ambient take-over failed", exc_info=True)
        return []


__all__ = ["is_person", "on_github_event", "on_run_cancelled"]
