"""A STANDARD WORKFLOW IS EDITED IN PLACE, AND CAN ALWAYS GO BACK TO WHAT BOSS SHIPPED.

    "we should make all the workflows editable, and if they make mistakes we should go back to
     factory mode which is reset to original value. Then they can start from beginning.. if
     they need tiny bit of change they can directly do it right?"

Until this, a catalogue workflow ("standard") was read-only and a change meant a copy. Now an
edit publishes the next version of the standard itself, and the shipped spec stays recorded as
its BASELINE: `workflow_definitions.standard_version_id` (the version holding what BOSS last
shipped) and `standard_content_hash` (that version's hash).

  * MODIFIED  = the active version's hash differs from the baseline's.
  * RESET     = publish the baseline spec again. Versions are unique by content, so that makes
                the shipped version active again rather than minting an identical copy; the
                edits stay in the version list, and Restore brings them back.
  * UPGRADE   = the catalogue seed (workflow_catalogue.seed) ships a newer spec. Unmodified:
                it becomes active and the new baseline, silently. Modified: it is published
                NOT active and offered ("New standard available"): take it and keep my changes
                (when the three-way apply is clean and the result validates), take it only, or
                not now.

Runs are never moved: a run reads the version it started on (workflow_runs.workflow_version_id),
so a run in flight keeps its version through an edit, a reset or an upgrade.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import workflows
from .core import emit_event

#: Keys BOSS stamps onto a spec when it is published; never a change the operator made.
_STAMPS = frozenset({workflows.AGENT_RULES_KEY, "_quality_bar_by"})

#: What a changed step field is called in a diff line.
_FIELD_WORDS = {
    "captain": "agent", "agent": "agent", "approval": "approval", "binding": "settings",
    "next": "what comes next", "label": "name", "title": "name", "kind": "kind",
    "on_error": "on failure", "retry": "retries", "timeout_seconds": "time limit",
    "contract": "contract", "condition": "condition", "wait_seconds": "wait",
}


class StandardError(workflows.WorkflowConflictError):
    """A standard-only act asked of a workflow that is not one, or with nothing to do."""


# ── reading ──────────────────────────────────────────────────────────────────────────────────

def _spec(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _version(connection: Connection, version_id: Optional[str]) -> Optional[dict[str, Any]]:
    if not version_id:
        return None
    row = connection.execute(
        text("SELECT id, version, spec, content_hash FROM workflow_versions WHERE id = :id"),
        {"id": version_id}).mappings().first()
    return dict(row) if row else None


def _definition(connection: Connection, project_id: str, definition_id: str) -> dict[str, Any]:
    definition = workflows.require_namespace(connection, project_id, definition_id,
                                             workflows.PRODUCT_NAMESPACE)
    if not definition.get("is_catalogue"):
        raise StandardError(f"'{definition['name']}' is not a standard workflow, so there is "
                            "no shipped version to go back to.")
    return definition


def _clean(spec: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in spec.items() if k not in _STAMPS}


def _steps(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in spec.get("steps") or [] if isinstance(s, dict) and s.get("id")]


def _label(step: dict[str, Any]) -> str:
    try:
        return workflows._step_label(step, str(step["id"]))
    except Exception:  # noqa: BLE001 — a label is words; never why a diff fails
        return str(step["id"])


def structural_diff(base: Any, current: Any) -> list[dict[str, Any]]:
    """Steps added, removed and changed from `base` to `current`, one plain line each.

    [{"change": "added"|"removed"|"changed"|"setting", "step_id", "text"}]. A step is matched by
    its id; a changed one names which of its parts changed, in words.
    """
    base, current = _clean(_spec(base)), _clean(_spec(current))
    before = {str(s["id"]): s for s in _steps(base)}
    after = {str(s["id"]): s for s in _steps(current)}
    order = [str(s["id"]) for s in _steps(current)]
    lines: list[dict[str, Any]] = []
    for index, step_id in enumerate(order):
        step = after[step_id]
        if step_id not in before:
            previous = next((after[order[i]] for i in range(index - 1, -1, -1)), None)
            where = f", after {_label(previous)}" if previous else ", first"
            lines.append({"change": "added", "step_id": step_id,
                          "text": f"+ {_label(step)}{where}"})
            continue
        old = before[step_id]
        if old == step:
            continue
        parts: list[str] = []
        for key in sorted(set(old) | set(step)):
            if old.get(key) != step.get(key):
                word = _FIELD_WORDS.get(key, key.replace("_", " "))
                if word not in parts:
                    parts.append(word)
        old_label, new_label = _label(old), _label(step)
        name = old_label if old_label == new_label else f"{old_label} → {new_label}"
        lines.append({"change": "changed", "step_id": step_id,
                      "text": f"~ {name}: {', '.join(parts)} changed"})
    for step_id, step in before.items():
        if step_id not in after:
            lines.append({"change": "removed", "step_id": step_id,
                          "text": f"− {_label(step)} removed"})
    for key in sorted((set(base) | set(current)) - {"steps"}):
        if base.get(key) != current.get(key):
            lines.append({"change": "setting", "step_id": None,
                          "text": f"~ Workflow setting: {key.replace('_', ' ')} changed"})
    return lines


def state(connection: Connection, definition: dict[str, Any]) -> Optional[dict[str, Any]]:
    """What the editor shows about a standard, or None for a workflow that is not one.

    {modified, baseline_version, baseline_hash, changes: [diff lines], new_standard: None |
    {version, changes (standard's own, baseline → new), applies_cleanly, conflicts}}
    """
    if not definition.get("is_catalogue"):
        return None
    baseline = _version(connection, definition.get("standard_version_id"))
    active = _version(connection, definition.get("active_version_id"))
    modified = bool(baseline and active and active["content_hash"] != baseline["content_hash"])
    out: dict[str, Any] = {
        "modified": modified,
        "baseline_version": int(baseline["version"]) if baseline else None,
        "baseline_hash": baseline["content_hash"] if baseline else None,
        "changes": structural_diff(baseline["spec"], active["spec"])
        if modified and baseline and active else [],
        "new_standard": None,
    }
    waiting = definition.get("standard_available_version_id")
    if (waiting and waiting != definition.get("standard_dismissed_version_id")
            and waiting != definition.get("standard_version_id")):
        offered = _version(connection, waiting)
        if offered and baseline and active:
            merged, conflicts = three_way(_spec(baseline["spec"]), _spec(active["spec"]),
                                          _spec(offered["spec"]))
            clean = not conflicts and _validates(connection, str(definition["project_id"]),
                                                 merged)
            out["new_standard"] = {
                "version": int(offered["version"]),
                "changes": structural_diff(baseline["spec"], offered["spec"]),
                "applies_cleanly": clean,
                "conflicts": conflicts,
            }
    return out


def state_by_id(connection: Connection, project_id: str,
                definition_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text("SELECT * FROM workflow_definitions WHERE id = :id AND project_id = :p"),
        {"id": definition_id, "p": project_id}).mappings().first()
    return state(connection, dict(row)) if row else None


# ── the three-way apply ──────────────────────────────────────────────────────────────────────

def _merge_value(base: Any, ours: Any, theirs: Any) -> tuple[Any, bool]:
    """(value, conflict) for one key: whoever changed it wins; both, differently, conflict."""
    if ours == theirs:
        return ours, False
    if ours == base:
        return theirs, False
    if theirs == base:
        return ours, False
    return ours, True


_MISSING = object()


def three_way(base: dict[str, Any], ours: dict[str, Any],
              theirs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Apply what the standard changed (base → theirs) onto the operator's spec (ours).

    Field by field within each step (matched by id), and key by key at the top level. A field
    both sides changed differently is a conflict, named in words; the caller then offers only
    "take the new standard" — never a guess.
    """
    base, ours, theirs = _clean(base), _clean(ours), _clean(theirs)
    conflicts: list[str] = []
    merged: dict[str, Any] = {}
    for key in list(dict.fromkeys([*theirs, *ours, *base])):
        if key == "steps":
            continue
        value, clash = _merge_value(base.get(key, _MISSING), ours.get(key, _MISSING),
                                    theirs.get(key, _MISSING))
        if clash:
            conflicts.append(f"workflow setting '{key}'")
        if value is not _MISSING:
            merged[key] = copy.deepcopy(value)

    b = {str(s["id"]): s for s in _steps(base)}
    o = {str(s["id"]): s for s in _steps(ours)}
    t = {str(s["id"]): s for s in _steps(theirs)}
    steps: dict[str, dict[str, Any]] = {}
    for step_id in dict.fromkeys([*t, *o]):
        bs, os_, ts = b.get(step_id), o.get(step_id), t.get(step_id)
        if bs is None:                        # added by one side (or both)
            if os_ is not None and ts is not None and os_ != ts:
                conflicts.append(f"step '{_label(os_)}' added differently")
            steps[step_id] = copy.deepcopy(os_ if os_ is not None else ts)
            continue
        if os_ is None and ts is None:        # removed by both
            continue
        if os_ is None:                       # the operator removed it
            if ts != bs:
                conflicts.append(f"step '{_label(bs)}' (you removed it; the standard changed it)")
            continue
        if ts is None:                        # the standard removed it
            if os_ != bs:
                conflicts.append(f"step '{_label(bs)}' (the standard removed it; you changed it)")
                steps[step_id] = copy.deepcopy(os_)
            continue
        step: dict[str, Any] = {}
        for key in list(dict.fromkeys([*ts, *os_, *bs])):
            value, clash = _merge_value(bs.get(key, _MISSING), os_.get(key, _MISSING),
                                        ts.get(key, _MISSING))
            if clash:
                conflicts.append(f"step '{_label(os_)}' {_FIELD_WORDS.get(key, key)}")
            if value is not _MISSING:
                step[key] = copy.deepcopy(value)
        steps[step_id] = step

    # ORDER: the standard's, with each of the operator's own steps after the one it followed.
    order = [i for i in t if i in steps]
    ours_order = [i for i in o]
    for index, step_id in enumerate(ours_order):
        if step_id in order or step_id not in steps:
            continue
        anchor = next((ours_order[j] for j in range(index - 1, -1, -1)
                       if ours_order[j] in order), None)
        order.insert(order.index(anchor) + 1 if anchor else 0, step_id)
    merged["steps"] = [steps[i] for i in order]
    # Wiring that points at a step the result no longer has is not clean.
    present = set(order)
    for step in merged["steps"]:
        dangling = [n for n in step.get("next") or [] if isinstance(n, str) and n not in present]
        if dangling:
            conflicts.append(f"step '{_label(step)}' leads to a removed step")
    if merged.get("entry_step") and merged["entry_step"] not in present:
        conflicts.append("the first step was removed")
    return merged, conflicts


def _validates(connection: Connection, project_id: str, spec: dict[str, Any]) -> bool:
    try:
        with connection.begin_nested():
            _, shape, blocked = workflows.spec_problems(
                connection, project_id, spec, namespace=workflows.PRODUCT_NAMESPACE)
        return not (shape or blocked)
    except Exception:  # noqa: BLE001 — unknown is not clean
        return False


# ── the acts ─────────────────────────────────────────────────────────────────────────────────

def _set_baseline(connection: Connection, definition_id: str, version: dict[str, Any], *,
                  clear_offer: bool) -> None:
    connection.execute(
        text("UPDATE workflow_definitions SET standard_version_id = :v, "
             "standard_content_hash = :h"
             + (", standard_available_version_id = NULL, standard_dismissed_version_id = NULL"
                if clear_offer else "")
             + ", updated_at = now() WHERE id = :id"),
        {"v": version["id"], "h": version["content_hash"], "id": definition_id})


def record_shipped(connection: Connection, definition_id: str, spec: dict[str, Any],
                   actor: str) -> dict[str, Any]:
    """THE CATALOGUE SEED'S ONE DOOR for publishing a shipped spec onto a standard.

    Unmodified (or no baseline yet): publish and activate it, and it is the baseline.
    Modified: never overwrite. Publish it NOT active and offer it ("New standard available"),
    unless it is the baseline already. Returns publish_version's result plus `offered`.
    """
    project_id = str(connection.execute(
        text("SELECT project_id FROM workflow_definitions WHERE id = :id"),
        {"id": definition_id}).scalar_one())
    # The reach lock BEFORE the row lock, in the order publish_version and an agent delete
    # (restaff_without) take them, so a boot seed beside an agent delete cannot deadlock.
    workflows.hold_workflow_reach(connection, project_id)
    definition = connection.execute(
        text("SELECT * FROM workflow_definitions WHERE id = :id FOR UPDATE"),
        {"id": definition_id}).mappings().one()
    baseline_hash = definition.get("standard_content_hash")
    active = _version(connection, definition.get("active_version_id"))
    modified = bool(baseline_hash and active and active["content_hash"] != baseline_hash)
    if not modified:
        version = workflows.publish_version(connection, project_id=project_id,
                                            definition_id=definition_id, spec=spec, actor=actor)
        _set_baseline(connection, definition_id, version, clear_offer=True)
        return {**version, "offered": False}
    version = workflows.publish_version(connection, project_id=project_id,
                                        definition_id=definition_id, spec=spec, actor=actor,
                                        activate=False)
    if version["content_hash"] == baseline_hash:
        return {**version, "offered": False}
    baseline = _version(connection, definition.get("standard_version_id"))
    if baseline is not None and not structural_diff(baseline["spec"], version["spec"]):
        # NOTHING THE OPERATOR WOULD SEE CHANGED (an agent-rules stamp moved on): the new
        # version becomes the baseline quietly. Offering it would show an empty "New standard".
        _set_baseline(connection, definition_id, version, clear_offer=True)
        return {**version, "offered": False}
    if definition.get("standard_available_version_id") != version["id"]:
        connection.execute(
            text("UPDATE workflow_definitions SET standard_available_version_id = :v, "
                 "updated_at = now() WHERE id = :id"),
            {"v": version["id"], "id": definition_id})
        emit_event(connection, project_id, "workflow.standard_available", actor,
                   "workflow_definition", definition_id,
                   {"workflow_key": definition["workflow_key"], "version": version["version"]})
    return {**version, "offered": True}


def _after_publish(connection: Connection, project_id: str, definition: dict[str, Any]) -> None:
    """New work follows the version now active: BOSS's own default mapping moves with it."""
    from .workflow_catalogue import repoint_defaults_for

    repoint_defaults_for(connection, project_id, str(definition["workflow_key"]))


def _staffed(connection: Connection, project_id: str, spec: Any) -> dict[str, Any]:
    """The spec with every default agent this project still HAS; a default whose agent was
    deleted since it shipped goes to Auto (workflow_catalogue._staffed_for, as the seed does).
    A shipped baseline recorded before a delete must not bring the deleted agent back."""
    from .workflow_catalogue import DEFAULT_CAPTAIN_FALLBACK, _set_default_captain

    spec = _spec(spec)
    present = frozenset(str(k) for k in connection.execute(
        text("SELECT role_key FROM roles WHERE project_id = :p"), {"p": project_id}).scalars())
    steps = []
    for step in spec.get("steps") or []:
        if isinstance(step, dict):
            default = step.get("default_captain")
            # Only a SOFT default whose agent is gone: anything a person named is theirs.
            if default and step.get("captain") == default and default not in present:
                stand_in = DEFAULT_CAPTAIN_FALLBACK.get(default)
                step = _set_default_captain(dict(step), stand_in if stand_in in present else None)
        steps.append(step)
    return {**spec, "steps": steps}


def reset_to_standard(connection: Connection, project_id: str, definition_id: str,
                      actor: str) -> dict[str, Any]:
    """Make the shipped spec the version new runs use. The edits stay in the version list."""
    definition = _definition(connection, project_id, definition_id)
    # The NEWEST shipped standard: a newer one waiting is what "factory" means now, unless the
    # operator set it aside ("Not now") — then the one they started from.
    waiting = definition.get("standard_available_version_id")
    if waiting and waiting == definition.get("standard_dismissed_version_id"):
        waiting = None
    baseline = (_version(connection, waiting)
                or _version(connection, definition.get("standard_version_id")))
    if baseline is None:
        raise StandardError(f"'{definition['name']}' has no shipped version recorded to reset to.")
    version = workflows.publish_version(connection, project_id=project_id,
                                        definition_id=definition_id,
                                        spec=_staffed(connection, project_id, baseline["spec"]),
                                        actor=actor)
    # Publishing the baseline's spec again may hash differently if the agent rules moved on
    # since it shipped; either way what is now active is the shipped spec, so it is the baseline.
    _set_baseline(connection, definition_id, version, clear_offer=True)
    _after_publish(connection, project_id, definition)
    emit_event(connection, project_id, "workflow.standard_reset", actor,
               "workflow_definition", definition_id,
               {"workflow_key": definition["workflow_key"], "version": version["version"]})
    return {"version": int(version["version"]), "standard": state_by_id(
        connection, project_id, definition_id)}


def take_new_standard(connection: Connection, project_id: str, definition_id: str, actor: str,
                      *, keep_changes: bool) -> dict[str, Any]:
    """Take the offered new standard: on its own, or with the operator's changes applied on it
    (only when that applies cleanly and validates — otherwise refused, nothing written)."""
    definition = _definition(connection, project_id, definition_id)
    offered = _version(connection, definition.get("standard_available_version_id"))
    if offered is None:
        raise StandardError(f"There is no new standard waiting for '{definition['name']}'.")
    if keep_changes:
        baseline = _version(connection, definition.get("standard_version_id"))
        active = _version(connection, definition.get("active_version_id"))
        if baseline is None or active is None:
            raise StandardError("Your changes cannot be told apart from the standard here; "
                                "take the new standard only.")
        merged, conflicts = three_way(_spec(baseline["spec"]), _spec(active["spec"]),
                                      _spec(offered["spec"]))
        if conflicts:
            raise StandardError("Your changes and the new standard touch the same things ("
                                + "; ".join(conflicts[:3]) + "). Take the new standard only, "
                                "then make your change again.")
        published = workflows.publish_version(connection, project_id=project_id,
                                              definition_id=definition_id,
                                              spec=_staffed(connection, project_id, merged),
                                              actor=actor)
    else:
        published = workflows.publish_version(connection, project_id=project_id,
                                              definition_id=definition_id,
                                              spec=_staffed(connection, project_id, offered["spec"]),
                                              actor=actor)
    _set_baseline(connection, definition_id, offered, clear_offer=True)
    _after_publish(connection, project_id, definition)
    emit_event(connection, project_id, "workflow.standard_taken", actor,
               "workflow_definition", definition_id,
               {"workflow_key": definition["workflow_key"], "version": published["version"],
                "kept_changes": bool(keep_changes)})
    return {"version": int(published["version"]),
            "standard": state_by_id(connection, project_id, definition_id)}


def dismiss_new_standard(connection: Connection, project_id: str, definition_id: str,
                         actor: str) -> dict[str, Any]:
    """"Not now": hide this offer. A later shipped standard is offered again."""
    definition = _definition(connection, project_id, definition_id)
    waiting = definition.get("standard_available_version_id")
    if not waiting:
        raise StandardError(f"There is no new standard waiting for '{definition['name']}'.")
    connection.execute(
        text("UPDATE workflow_definitions SET standard_dismissed_version_id = :v, "
             "updated_at = now() WHERE id = :id"), {"v": waiting, "id": definition_id})
    emit_event(connection, project_id, "workflow.standard_dismissed", actor,
               "workflow_definition", definition_id,
               {"workflow_key": definition["workflow_key"]})
    return {"standard": state_by_id(connection, project_id, definition_id)}
