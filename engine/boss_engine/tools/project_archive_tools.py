"""Archive and restore a project, as actions of the BOSS agent (BOSSAPP-768 S5).

The same service as the console's buttons and POST /v1/projects/{id}/archive|unarchive
(services/projects_archive.py), so an ask and a press are one act, audited and evented the same
way. A GOVERNED WRITE: both are `operator_only`, so the agent OFFERS them as a button and a
person's press runs them; plain text never archives anything. The identity gate checks
`project:delete` (owner, step-up) at org scope with the presser's own context.
"""
from __future__ import annotations

from typing import Any

from .registry import ToolSpec

_PROJECT_ARG = {
    "type": "object",
    "properties": {
        # Not the generic `project` argument (PROJECT_ARG): project:delete is decided at org
        # scope, and the project is what the call acts ON, not where it is decided.
        "target": {"type": "string",
                   "description": "the project to archive or restore: its id (its slug, e.g. "
                                  "northpeak-bank-a)"},
        "note": {"type": "string", "description": "why, in the person's words (optional)"},
    },
    "required": ["target"],
    "additionalProperties": False,
}


def _target(args: dict, project_id: str) -> str:
    return str((args or {}).get("target") or "").strip() or project_id


def _org_matches(row: dict, auth: Any) -> bool:
    return auth is None or str(row["org_id"]) == str(auth.principal.org_id)


def _said(row: dict) -> dict[str, Any]:
    return {"project_id": row["id"], "name": row["name"],
            "archived_at": row["archived_at"].isoformat() if row.get("archived_at") else None,
            "archived_by": row.get("archived_by")}


def _archive(ctx, *, project_id, actor, args):
    from ..services import projects_archive as service
    from .registry import CURRENT_TOOL_AUTH

    auth = CURRENT_TOOL_AUTH.get()
    target = _target(args, project_id)
    with ctx.begin() as c:
        row = service.state(c, target)
        if row is None or not _org_matches(row, auth):
            return {"ok": False, "error": f"There is no project {target}."}
        try:
            done = service.archive(c, ctx.settings, project_id=target, actor=actor, auth=auth,
                                   note=args.get("note"))
        except service.ArchiveRefused as refused:
            return {"ok": False, "error": str(refused)}
    return {"ok": True, **_said(done), "stops": done.get("stops", []),
            "note": (f"{done['name']} is archived: it takes no new work and its schedules, "
                     "monitors and indexing are paused. Restore it from the project menu "
                     "(Show archived) or ask me to." if done.get("changed")
                     else f"{done['name']} was already archived.")}


def _restore(ctx, *, project_id, actor, args):
    from ..services import projects_archive as service
    from .registry import CURRENT_TOOL_AUTH

    auth = CURRENT_TOOL_AUTH.get()
    target = _target(args, project_id)
    with ctx.begin() as c:
        row = service.state(c, target)
        if row is None or not _org_matches(row, auth):
            return {"ok": False, "error": f"There is no project {target}."}
        done = service.restore(c, ctx.settings, project_id=target, actor=actor, auth=auth)
    return {"ok": True, **_said(done), "href": f"/?project_id={done['id']}",
            "note": (f"{done['name']} is restored: it takes work again and its schedules and "
                     "monitors resume on the next tick." if done.get("changed")
                     else f"{done['name']} was not archived.")}


#: name -> (description, handler)
PROJECT_ARCHIVE_TOOLS: dict[str, tuple] = {
    "archive_project": (
        "Archive a BOSS PROJECT (the person asked to archive, retire or put away a project): it "
        "is hidden from the project list, refuses new builds, workflow runs, code tasks and "
        "schedules, and its schedulers, monitors and indexing pause. Nothing is deleted and "
        "everything stays readable; restore_project brings it back. Offer it; it runs when the "
        "person presses it. The install's home project and an org's last open project cannot "
        "be archived.",
        _archive),
    "restore_project": (
        "Restore an archived BOSS PROJECT: listed again, takes work again, its schedules and "
        "monitors resume. Offer it; it runs when the person presses it.",
        _restore),
}


def register_project_archive_tools(reg) -> None:
    for name, (description, handler) in PROJECT_ARCHIVE_TOOLS.items():
        reg.register(ToolSpec(
            name=name, description=description, parameters=_PROJECT_ARG, handler=handler,
            kind="write",
            # Undone by the other one, and nothing is deleted. Still a person's press: archiving
            # stops every schedule and monitor of a project at once.
            reversibility="safe", operator_only=True,
            requires_durable_workspace=True, permission="project:delete", org_scope=True))
