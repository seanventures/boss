"""The captain's powers, as actions of the ONE BOSS agent (BOSSAPP-759).

Metatron's captain used to be a separate engine: its own chat turn, its own keyword reader, its
own model decision, and an always-on loop in the worker that applied moves nobody asked for. The
operator, 2026-10-23: "Why do we even have the captain autopilot? Retire it. Put its
functionality into the BOSS agent. There must be ONE agent pipeline."

So these are ordinary registry tools. The BOSS agent chooses them through the same router and the
same model decision as every other action, under the same governance: the tool's permission, the
identity gate, consent. What used to be the captain's private rules stays where it was — inside
the tool (`validate_move`, budget and duplicate checks, decisions and dead letters never applied,
BOSS self-change only as a PR). Those are a deterministic playbook the tool runs, not a choice of
what to do next; that choice is the agent's, or the person's.

Who presses what:
  * captain_watch, captain_goals, captain_set_goal — observe, read, record a goal. The agent may run
    them itself.
  * captain_drive, captain_overwatch, captain_theater — they APPLY a batch of moves (a build may
    start). `operator_only`: the agent offers them; a person's press, or a typed /overwatch,
    /drive, runs them. Plain text never does.
"""
from __future__ import annotations

from typing import Any

from .registry import Exposure, ToolSpec

_NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}


def _captain(ctx):
    from ..services.metatron_captain import MetatronCaptain

    return MetatronCaptain(ctx.settings)


def _done(result: Any) -> dict:
    out = dict(result or {}) if isinstance(result, dict) else {"result": result}
    out.setdefault("ok", True)
    return out


def _watch(ctx, *, project_id, actor, args):
    with ctx.begin() as connection:
        return _done(_captain(ctx).watch_once(connection, project_id=project_id, actor=actor))


def _goals(ctx, *, project_id, actor, args):
    captain = _captain(ctx)
    with ctx.begin() as connection:
        captain.ensure_schema(connection)
        return {"ok": True,
                "goals": captain.list_goals(connection, project_id),
                "memories": captain.list_memories(connection, project_id, limit=12),
                "watch": captain.latest_watch(connection, project_id),
                "last_overwatch": captain.latest_overwatch(connection, project_id)}


def _set_goal(ctx, *, project_id, actor, args):
    title = str(args.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "title is required"}
    auto_drive = args.get("auto_drive")
    with ctx.begin() as connection:
        return _done(_captain(ctx).set_goal(
            connection, project_id=project_id, actor=actor, title=title,
            kind=str(args.get("kind") or "custom"),
            description=str(args.get("description") or ""),
            auto_drive=None if auto_drive is None else bool(auto_drive)))


def _drive(ctx, *, project_id, actor, args):
    with ctx.begin() as connection:
        return _done(_captain(ctx).drive(connection, project_id=project_id, actor=actor,
                                         auto=bool(args.get("auto"))))


def _overwatch(ctx, *, project_id, actor, args):
    with ctx.begin() as connection:
        return _done(_captain(ctx).overwatch_once(connection, project_id=project_id, actor=actor))


def _theater(ctx, *, project_id, actor, args):
    auto = args.get("auto")
    with ctx.begin() as connection:
        return _done(_captain(ctx).command_once(connection, project_id=project_id, actor=actor,
                                                auto=True if auto is None else bool(auto)))


#: name -> (description, parameters, handler, kind, permission, operator_only)
CAPTAIN_TOOLS: dict[str, tuple] = {
    "captain_watch": (
        "Observe the factory and score every captain goal: what is in flight, stalls, dead "
        "letters, previews, budget. Records a watch tick; applies nothing.",
        _NO_ARGS, _watch, "write", "autopilot:operate", False),
    "captain_goals": (
        "The captain's goals, its remembered facts and the latest watch and overwatch.",
        _NO_ARGS, _goals, "read", "autopilot:read", False),
    "captain_set_goal": (
        "Register a measurable goal the captain scores on every watch (kind: working_software, "
        "budget, queue, quality or custom).",
        {"type": "object", "properties": {
            "title": {"type": "string"},
            "kind": {"type": "string",
                     "enum": ["working_software", "budget", "queue", "quality", "custom"]},
            "description": {"type": "string"},
            "auto_drive": {"type": "boolean"}},
         "required": ["title"], "additionalProperties": False},
        _set_goal, "write", "autopilot:operate", False),
    "captain_drive": (
        "Propose the captain's next moves from the latest watch; with auto=true, apply the ones "
        "that pass validation (a preview restart, a factory build). Decisions and dead letters "
        "are never applied. Offer it; it runs when the person presses it.",
        {"type": "object", "properties": {"auto": {"type": "boolean"}},
         "additionalProperties": False},
        _drive, "write", "autopilot:operate", True),
    "captain_overwatch": (
        "One captain cycle: observe, measure goals, validate, apply the safe moves, verify. "
        "Offer it; it runs when the person presses it.",
        _NO_ARGS, _overwatch, "write", "autopilot:operate", True),
    "captain_theater": (
        "Command the fleet: re-prioritise official work and staff captains against the goals "
        "(never a second factory build). Offer it; it runs when the person presses it.",
        {"type": "object", "properties": {"auto": {"type": "boolean"}},
         "additionalProperties": False},
        _theater, "write", "autopilot:operate", True),
}


def register_captain_tools(reg) -> None:
    for name, (description, parameters, handler, kind, permission, operator_only) in \
            CAPTAIN_TOOLS.items():
        reg.register(ToolSpec(
            name=name, description=description, parameters=parameters, handler=handler,
            kind=kind,
            # A drive may start a build, which spends; the rest are redone by running again.
            reversibility="costs_money" if operator_only else "safe",
            requires_durable_workspace=True, permission=permission,
            operator_only=operator_only,
            exposure=Exposure(tags=("captain",)),
            offer_note=("it applies moves a person has not seen one by one, and a build may "
                        "start" if operator_only else "")))
