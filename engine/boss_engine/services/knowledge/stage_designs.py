"""Agent design review and lock, per workflow stage (design §4.6).

A stage's DESIGN is its step contract: what the agent reads, writes and calls, its numbered
steps and its cards. Each (workflow, stage) has design versions in ``workflow_stage_designs``:

    not_reviewed ──Send for review──▶ in_review ──Lock design──▶ locked
                                          ▲                        │
                                          └──────── Revise ────────┘   (next version, reference bumped)

* SEND FOR REVIEW opens a version in review, its reference ("Agent Reference v0.1") taken from
  the contract's ``design.reference`` or the first version's.
* LOCK DESIGN needs a PERSON who manages the workflow — never an agent or a run token — and,
  at the API, a fresh second factor (workflow:publish is a step-up permission). It snapshots
  the stage's contract (its hash) as the locked design.
* REVISE opens the next version in review with the reference bumped (v0.2 → v0.3); the locked
  version stays on record, and the console keeps saying which one was locked.
* PUBLISH ENFORCES IT: a save that changes a LOCKED stage's contract (or removes the stage) is
  refused with a sentence that names the stage, its locked reference and "Revise". Only the
  managed fields (``design.status``, ``design.reference``, ``design.under_revision``) may change
  without it. BOSS's own standards (catalogue definitions) ship from code and are not refused.

THE STATUS A STAGE SHOWS, in order: its latest design version here; else what its contract
declares (``design.status``, as a consultant's imported design says "locked"); else "human
step" for a person's decision; else "not reviewed". A contract that DECLARES locked is enforced
like a locked version, its declared contract being the locked one.

A copy of a workflow inherits the design versions of the workflow it was copied from until it
has its own. Every transition is a ``workflow.design_*`` event (the audit trail).
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..core import emit_event, json_hash, make_id

#: Fields of `contract.design` that the review record manages: changing them is not a change
#: of the locked design.
MANAGED_DESIGN_FIELDS = ("status", "reference", "under_revision")
DEFAULT_REFERENCE = "Agent Reference v0.1"
_VERSIONED = re.compile(r"^(.*?v)(\d+)\.(\d+)\s*$")
_PLAIN = re.compile(r"^(.*?v)(\d+)\s*$")


class DesignRefused(ValueError):
    """A design transition that cannot happen now, said so a person knows what to do."""


def contract_hash(contract: Any) -> str:
    """The hash of a stage's design: its contract without the review-managed fields."""
    if not isinstance(contract, Mapping):
        return json_hash({})
    body = {k: v for k, v in contract.items() if k != "design"}
    design = contract.get("design")
    if isinstance(design, Mapping):
        kept = {k: v for k, v in design.items() if k not in MANAGED_DESIGN_FIELDS}
        if kept:
            body["design"] = kept
    return json_hash(body)


def next_reference(reference: str) -> str:
    """"Agent Reference v0.2" → "Agent Reference v0.3"; "Spec v2" → "Spec v3"."""
    ref = str(reference or "").strip()
    if not ref:
        return DEFAULT_REFERENCE
    match = _VERSIONED.match(ref)
    if match:
        return f"{match.group(1)}{match.group(2)}.{int(match.group(3)) + 1}"
    match = _PLAIN.match(ref)
    if match:
        return f"{match.group(1)}{int(match.group(2)) + 1}"
    return f"{ref} v0.2"


def _version_of(reference: str) -> str:
    """The "v0.2" at the end of a reference, for sentences."""
    match = re.search(r"v\d+(?:\.\d+)?\s*$", str(reference or ""))
    return match.group(0).strip() if match else str(reference or "")


# ── reading ───────────────────────────────────────────────────────────────────────────────────

def _definition(connection: Connection, project_id: str, *, workflow_key: Optional[str] = None,
                definition_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    if definition_id:
        row = connection.execute(text(
            "SELECT * FROM workflow_definitions WHERE id = :i AND project_id = :p"),
            {"i": definition_id, "p": project_id}).mappings().first()
    else:
        row = connection.execute(text(
            "SELECT * FROM workflow_definitions WHERE workflow_key = :k AND project_id = :p"),
            {"k": workflow_key, "p": project_id}).mappings().first()
    return dict(row) if row else None


def _lineage(connection: Connection, definition: Mapping[str, Any]) -> list[str]:
    """This definition, then the one it was copied from, and so on (at most 5 deep)."""
    out = [str(definition["id"])]
    forked = definition.get("forked_from_version_id")
    while forked and len(out) < 5:
        row = connection.execute(text(
            "SELECT d.id, d.forked_from_version_id FROM workflow_versions v "
            "JOIN workflow_definitions d ON d.id = v.workflow_definition_id WHERE v.id = :v"),
            {"v": forked}).mappings().first()
        if row is None or str(row["id"]) in out:
            break
        out.append(str(row["id"]))
        forked = row["forked_from_version_id"]
    return out


def _active_spec(connection: Connection, definition: Mapping[str, Any]) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT spec FROM workflow_versions WHERE workflow_definition_id = :d AND status = 'published' "
        "ORDER BY (id = :a) DESC, version DESC LIMIT 1"),
        {"d": definition["id"], "a": definition.get("active_version_id") or ""}).first()
    if row is None and definition.get("forked_from_version_id"):
        row = connection.execute(text("SELECT spec FROM workflow_versions WHERE id = :v"),
                                 {"v": definition["forked_from_version_id"]}).first()
    spec = row[0] if row else {}
    return spec if isinstance(spec, dict) else {}


def _records(connection: Connection, project_id: str, lineage: list[str]) -> dict[str, list[dict]]:
    """{stage_id: [versions, newest first]} from the nearest definition in the lineage that has
    any for that stage."""
    if not lineage:
        return {}
    rows = connection.execute(text(
        "SELECT * FROM workflow_stage_designs WHERE project_id = :p AND workflow_definition_id = ANY(:d) "
        "ORDER BY design_version DESC"), {"p": project_id, "d": lineage}).mappings().all()
    by: dict[str, dict[str, list[dict]]] = {}
    for row in rows:
        by.setdefault(str(row["stage_id"]), {}).setdefault(str(row["workflow_definition_id"]), []).append(dict(row))
    out: dict[str, list[dict]] = {}
    for stage, per_def in by.items():
        for definition_id in lineage:
            if per_def.get(definition_id):
                out[stage] = per_def[definition_id]
                break
    return out


def _steps(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [s for s in (spec.get("steps") or []) if isinstance(s, dict) and s.get("id")]


def _declared(step: Mapping[str, Any]) -> dict[str, Any]:
    contract = step.get("contract") if isinstance(step.get("contract"), Mapping) else {}
    design = contract.get("design") if isinstance(contract.get("design"), Mapping) else {}
    return dict(design)


def _default_status(step: Mapping[str, Any]) -> str:
    return "human_step" if step.get("kind") == "decision" else "not_reviewed"


def _public(row: Mapping[str, Any]) -> dict[str, Any]:
    at = row.get("locked_at")
    return {
        "version": int(row["design_version"]), "reference": row["reference"], "status": row["status"],
        "locked_by": row.get("locked_by_name") or None,
        "locked_at": at.isoformat() if hasattr(at, "isoformat") else at,
    }


def stage_design(step: Mapping[str, Any], versions: list[dict]) -> dict[str, Any]:
    """One stage's design status as the console shows it (no ids, no actor keys)."""
    declared = _declared(step)
    contract = step.get("contract")
    latest = versions[0] if versions else None
    locked = next((v for v in versions if v["status"] == "locked"), None)
    if latest is not None:
        status, reference, source = latest["status"], latest["reference"], "review"
        revision = latest.get("under_revision") or declared.get("under_revision")
    else:
        status = str(declared.get("status") or "") or _default_status(step)
        reference, source = str(declared.get("reference") or ""), (
            "contract" if declared.get("status") else "default")
        revision = declared.get("under_revision")
    reviewable = status in ("not_reviewed", "in_review", "locked") and bool(contract)
    return {
        "status": status,
        "reference": reference,
        "under_revision": revision or None,
        "source": source,
        "version": int(latest["design_version"]) if latest else None,
        # The version a person locked, while a later one is in review ("v0.1 locked").
        "locked": _public(locked) if locked else (
            {"version": None, "reference": reference, "status": "locked", "locked_by": None,
             "locked_at": None} if status == "locked" else None),
        "can": {
            "send_for_review": reviewable and status == "not_reviewed",
            "lock": reviewable and status == "in_review",
            "revise": reviewable and status == "locked",
        },
        "changed_since_lock": bool(locked and contract_hash(contract) != locked["contract_hash"]),
    }


def designs(connection: Connection, project_id: str, workflow_key: str) -> Optional[dict[str, Any]]:
    """Every stage of the workflow's running version, with its design status and reference, and
    the project's graph versions for the band titles. None when there is no such workflow."""
    from .graph_versions import versions as graph_versions

    definition = _definition(connection, project_id, workflow_key=workflow_key)
    if definition is None:
        return None
    spec = _active_spec(connection, definition)
    records = _records(connection, project_id, _lineage(connection, definition))
    return {
        "workflow_key": workflow_key,
        "stages": {str(s["id"]): stage_design(s, records.get(str(s["id"]), [])) for s in _steps(spec)},
        "graph": graph_versions(connection, project_id),
        "kinds": kinds_meta(connection, project_id, spec),
    }


def kinds_meta(connection: Connection, project_id: str, spec: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Every kind the workflow's contracts name: its label, its domain colour (the project's
    ontology pack, else the core legend; None when neither colours it) and its stored layer —
    what the agent read/write view colours and bands by."""
    from . import scopes
    from .view_context import kind_meta, legend, words

    try:
        with connection.begin_nested():
            own = legend(connection, project_id)
    except Exception:  # noqa: BLE001 - the shipped packs still colour the kinds
        own = {}
    named: set[str] = set()
    for step in _steps(spec):
        contract = step.get("contract") if isinstance(step.get("contract"), Mapping) else {}
        for side in ("reads", "writes"):
            for entry in contract.get(side) or []:
                if isinstance(entry, Mapping) and str(entry.get("kind") or "").strip():
                    named.add(str(entry["kind"]).strip())
    out: dict[str, dict[str, Any]] = {}
    for kind in sorted(named):
        meta = {**(kind_meta(kind) or {}), **own.get(kind, {})}
        out[kind] = {"label": str(meta.get("label") or words(kind)), "color": meta.get("color") or None,
                     "layer": scopes.layer_of(kind)}
    return out


# ── transitions ───────────────────────────────────────────────────────────────────────────────

def _stage(connection: Connection, project_id: str, workflow_key: str, stage_id: str
           ) -> tuple[dict[str, Any], dict[str, Any], list[dict]]:
    definition = _definition(connection, project_id, workflow_key=workflow_key)
    if definition is None:
        raise LookupError(f"no workflow '{workflow_key}' in this project")
    step = next((s for s in _steps(_active_spec(connection, definition)) if s["id"] == stage_id), None)
    if step is None:
        raise LookupError(f"this workflow has no stage '{stage_id}'")
    if not isinstance(step.get("contract"), Mapping):
        raise DesignRefused("This stage declares no contract yet, so there is no design to review. "
                            "Add its contract first.")
    versions = _records(connection, project_id, [str(definition["id"])]).get(stage_id, [])
    if not versions:
        # A copy reviews on top of the design it inherited.
        versions = _records(connection, project_id, _lineage(connection, definition)).get(stage_id, [])
    return definition, step, versions


def _title(step: Mapping[str, Any]) -> str:
    return str(step.get("title") or str(step.get("id")).replace("_", " ").capitalize())


def _insert(connection: Connection, project_id: str, definition: Mapping[str, Any], step: Mapping[str, Any],
            *, version: int, reference: str, status: str, actor: str, under_revision: Optional[str]
            ) -> dict[str, Any]:
    row = connection.execute(text(
        """
        INSERT INTO workflow_stage_designs (
          id, project_id, workflow_definition_id, stage_id, design_version, reference, status,
          contract_hash, contract, under_revision, created_by, sent_for_review_by, sent_for_review_at)
        VALUES (:id, :p, :d, :s, :v, :ref, :st, :h, CAST(:c AS JSONB), :rev, :actor,
                CASE WHEN :st = 'in_review' THEN :actor END,
                CASE WHEN :st = 'in_review' THEN now() END)
        RETURNING *
        """), {
            "id": make_id("wsd"), "p": project_id, "d": definition["id"], "s": step["id"],
            "v": version, "ref": reference, "st": status, "h": contract_hash(step.get("contract")),
            "c": json.dumps(step.get("contract") or {}), "rev": under_revision, "actor": actor,
        }).mappings().one()
    return dict(row)


def _event(connection: Connection, project_id: str, kind: str, actor: str, row: Mapping[str, Any],
           step: Mapping[str, Any], workflow_key: str) -> None:
    emit_event(connection, project_id, f"workflow.design_{kind}", actor, "workflow_stage_design",
               str(row["id"]), {"workflow_key": workflow_key, "stage_id": str(step["id"]),
                                "stage": _title(step), "reference": row["reference"],
                                "design_version": int(row["design_version"]), "status": row["status"]})


def send_for_review(connection: Connection, project_id: str, *, workflow_key: str, stage_id: str,
                    actor: str) -> dict[str, Any]:
    definition, step, versions = _stage(connection, project_id, workflow_key, stage_id)
    current = stage_design(step, versions)
    if current["status"] == "in_review":
        raise DesignRefused(f"{_title(step)} is already in review ({current['reference']}).")
    if current["status"] == "locked":
        raise DesignRefused(f"{_title(step)} is locked at {current['reference']}; "
                            "press Revise to open the next version for review.")
    if current["status"] not in ("not_reviewed",):
        raise DesignRefused(f"{_title(step)} is a {current['status'].replace('_', ' ')}, "
                            "which is not reviewed as an agent design.")
    declared = _declared(step)
    reference = str(declared.get("reference") or "") or (
        next_reference(versions[0]["reference"]) if versions else DEFAULT_REFERENCE)
    row = _insert(connection, project_id, definition, step,
                  version=(int(versions[0]["design_version"]) + 1) if versions else 1,
                  reference=reference, status="in_review", actor=actor,
                  under_revision=declared.get("under_revision"))
    _event(connection, project_id, "sent_for_review", actor, row, step, workflow_key)
    return stage_design(step, [row, *versions])


def lock(connection: Connection, project_id: str, *, workflow_key: str, stage_id: str, actor: str,
         actor_kind: str, actor_name: Optional[str] = None) -> dict[str, Any]:
    """Lock the stage's design in review. A person only."""
    if actor_kind != "person":
        raise PermissionError("Only a person can lock an agent design; agents propose, people lock.")
    definition, step, versions = _stage(connection, project_id, workflow_key, stage_id)
    current = stage_design(step, versions)
    if current["status"] == "locked":
        raise DesignRefused(f"{_title(step)} is already locked at {current['reference']}.")
    if current["status"] != "in_review":
        raise DesignRefused(f"Send {_title(step)} for review before locking it.")
    own = versions and str(versions[0]["workflow_definition_id"]) == str(definition["id"])
    if versions and versions[0]["status"] == "in_review" and own:
        row = connection.execute(text(
            "UPDATE workflow_stage_designs SET status = 'locked', locked_by = :a, locked_by_name = :n, "
            "locked_at = now(), contract_hash = :h, contract = CAST(:c AS JSONB), updated_at = now() "
            "WHERE id = :i RETURNING *"), {
                "a": actor, "n": actor_name, "i": versions[0]["id"],
                "h": contract_hash(step.get("contract")), "c": json.dumps(step.get("contract") or {}),
            }).mappings().one()
        row = dict(row)
        rest = versions[1:]
    else:
        # In review by declaration (an imported design), or inherited from the standard: this
        # workflow's own first record, locked in the same act.
        row = _insert(connection, project_id, definition, step,
                      version=(int(versions[0]["design_version"]) + 1) if versions else 1,
                      reference=current["reference"] or DEFAULT_REFERENCE, status="in_review",
                      actor=actor, under_revision=None)
        row = dict(connection.execute(text(
            "UPDATE workflow_stage_designs SET status = 'locked', locked_by = :a, locked_by_name = :n, "
            "locked_at = now() WHERE id = :i RETURNING *"),
            {"a": actor, "n": actor_name, "i": row["id"]}).mappings().one())
        rest = list(versions)
    _event(connection, project_id, "locked", actor, row, step, workflow_key)
    return stage_design(step, [row, *rest])


def revise(connection: Connection, project_id: str, *, workflow_key: str, stage_id: str, actor: str,
           note: Optional[str] = None) -> dict[str, Any]:
    """Open the next design version in review; the locked one stays on record."""
    definition, step, versions = _stage(connection, project_id, workflow_key, stage_id)
    current = stage_design(step, versions)
    if current["status"] != "locked":
        raise DesignRefused(f"Only a locked design is revised; {_title(step)} is "
                            f"{current['status'].replace('_', ' ')}.")
    if not versions:
        # Locked by declaration: record that locked version first, so the history says it.
        base = _insert(connection, project_id, definition, step, version=1,
                       reference=current["reference"] or DEFAULT_REFERENCE, status="in_review",
                       actor=actor, under_revision=None)
        base = dict(connection.execute(text(
            "UPDATE workflow_stage_designs SET status = 'locked', locked_at = now() WHERE id = :i "
            "RETURNING *"), {"i": base["id"]}).mappings().one())
        versions = [base]
    reference = next_reference(versions[0]["reference"])
    row = _insert(connection, project_id, definition, step,
                  version=int(versions[0]["design_version"]) + 1, reference=reference,
                  status="in_review", actor=actor,
                  under_revision=(str(note).strip() or None) if note else None)
    _event(connection, project_id, "revised", actor, row, step, workflow_key)
    return stage_design(step, [row, *versions])


# ── publish ───────────────────────────────────────────────────────────────────────────────────

def publish_refusals(connection: Connection, project_id: str, definition: Mapping[str, Any],
                     spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The stages a publish would change while their design is locked, each as a problem on its
    step's contract. Empty for BOSS's own standards and when nothing locked changes."""
    if definition.get("is_catalogue"):
        return []
    from ..workflows import problem

    try:
        with connection.begin_nested():
            records = _records(connection, project_id, _lineage(connection, definition))
    except Exception:  # noqa: BLE001 - no design table yet: nothing is locked
        return []
    previous = {str(s["id"]): s for s in _steps(_active_spec(connection, definition))}
    new = {str(s["id"]): s for s in _steps(spec)}
    problems: list[dict[str, Any]] = []
    for stage_id, before in previous.items():
        versions = records.get(stage_id, [])
        latest = versions[0] if versions else None
        if latest is not None:
            if latest["status"] != "locked":
                continue
            locked_hash, reference = latest["contract_hash"], latest["reference"]
        elif str(_declared(before).get("status") or "") == "locked":
            locked_hash, reference = contract_hash(before.get("contract")), str(
                _declared(before).get("reference") or DEFAULT_REFERENCE)
        else:
            continue
        after = new.get(stage_id)
        title = _title(before)
        if after is None:
            problems.append(problem(stage_id, "contract", (
                f"step '{stage_id}' {title} is locked at {reference}; press Revise on its design "
                f"to open {_version_of(next_reference(reference))} for review before removing it.")))
            continue
        if contract_hash(after.get("contract")) != locked_hash:
            problems.append(problem(stage_id, "contract", (
                f"step '{stage_id}' {title} is locked at {reference}, so its contract cannot "
                f"change; press Revise on its design to open {_version_of(next_reference(reference))} "
                "for review, then save.")))
    return problems
