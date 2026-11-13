"""DECISION OPTIONS WITH TYPED, SIGNED EFFECTS (BOSSAPP-879).

A decision used to carry ONE executable recommendation: Approve ran it, every other option was
words. An agent that wanted to offer real alternatives ("cancel the epic", "reassign it to QA",
"split it into two tasks") could only describe them, and a person picking one still had to do it
by hand.

Now a decision can carry up to EIGHT options (``recommendation.action == "choose_option"``), each
a list of TYPED effects on the project's work-item graph:

* ``comment``       {target, body}                      — a note on the item (its activity)
* ``create``        {title, description?, parent?, assignee?} — a new work item
* ``update_status`` {target, status}                    — the done door for ``done``, else set
* ``assign``        {target, assignee}                  — who works it
* ``cancel_tree``   {target}                            — the item and every open descendant

SIGNED (bound like BOSSAPP-816's held tool calls). When the decision is raised each option gets a
``target_snapshot`` (status, assignee and updated_at of every item its effects touch, read at that
moment) and ``sig``: ``hs1:<HMAC>`` under the identity server key over the decision id, the
option id, label, effects and snapshot. Choosing an option (``transition(..., choose=<id>)``)
re-verifies that signature and FAILS CLOSED — no signature (no server key when it was raised), a
signature that does not verify, an option id the card did not carry — and the approval is not
recorded. So what runs is exactly what the person was shown.

STALE TARGETS ARE SKIPPED, NOT GUESSED AT. Every target is compared with its snapshot before
anything runs; an effect whose target changed since the question was asked is recorded
``skipped_stale`` and does nothing (the others still run). Every effect's outcome is a row in
``decision_effect_runs`` — done | skipped_stale | failed — so the decision's page says exactly
what the choice did.

WHO. The asker (an agent through the ``offer_options`` tool, or a person through
POST /v1/decisions/options) needs ``work_item:manage``; the chooser needs ``decision:approve`` on
the card (the transition's own checks). The effects are attributed to the person who chose.
"""
from __future__ import annotations

import hmac
import json
import re
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import create_blocking_decision, emit_event, make_id

ACTION = "choose_option"
MAX_OPTIONS = 8
MAX_EFFECTS = 10
EFFECT_TYPES = ("comment", "create", "update_status", "assign", "cancel_tree")
#: The signature scheme (BOSSAPP-816's): HMAC-SHA256 under the identity server key.
SIGNATURE_SCHEME = "hs1"
_LABEL = "decision-option"
_OPTION_ID = re.compile(r"^[a-z0-9_-]{1,40}$")


class OptionError(ValueError):
    """An option set or a choice BOSS refuses (bad effect, unknown target, bad signature)."""


# ── shaping what an asker sends ──────────────────────────────────────────────────────────────

def _target_ids(effect: Mapping[str, Any]) -> list[str]:
    ids = []
    for key in ("target", "parent"):
        value = effect.get(key)
        if value:
            ids.append(str(value))
    return ids


def normalize_effect(connection: Connection, project_id: str, raw: Any) -> dict[str, Any]:
    from . import work_items

    if not isinstance(raw, Mapping):
        raise OptionError("each effect is an object with a type")
    kind = str(raw.get("type") or "").strip()
    if kind not in EFFECT_TYPES:
        raise OptionError(f"effect type must be one of {', '.join(EFFECT_TYPES)}")
    effect: dict[str, Any] = {"type": kind}
    if kind in ("comment", "update_status", "assign", "cancel_tree"):
        target = str(raw.get("target") or "").strip()
        if not target:
            raise OptionError(f"a {kind} effect names its target work item")
        effect["target"] = target
    if kind == "comment":
        body = str(raw.get("body") or "").strip()
        if not body:
            raise OptionError("a comment effect says something")
        effect["body"] = body[:2000]
    elif kind == "create":
        title = str(raw.get("title") or "").strip()
        if not title:
            raise OptionError("a create effect needs a title")
        effect["title"] = title[:300]
        if raw.get("description"):
            effect["description"] = str(raw["description"])[:4000]
        if raw.get("parent"):
            effect["parent"] = str(raw["parent"]).strip()
        if raw.get("assignee"):
            effect["assignee"] = work_items._party(raw["assignee"], "assignee")
    elif kind == "update_status":
        status = str(raw.get("status") or "").strip().lower()
        if status not in (*work_items.SETTABLE, "done"):
            raise OptionError(
                f"status must be one of {', '.join((*work_items.SETTABLE, 'done'))}")
        effect["status"] = status
    elif kind == "assign":
        effect["assignee"] = work_items._party(raw.get("assignee"), "assignee")
    for item_id in _target_ids(effect):
        if connection.execute(text(
                "SELECT 1 FROM work_items WHERE id = :i AND project_id = :p"),
                {"i": item_id, "p": project_id}).first() is None:
            raise OptionError(f"no work item {item_id} in this project")
    return effect


def normalize_options(connection: Connection, project_id: str, raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise OptionError("offer at least one option")
    if len(raw) > MAX_OPTIONS:
        raise OptionError(f"offer at most {MAX_OPTIONS} options")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, option in enumerate(raw):
        if not isinstance(option, Mapping):
            raise OptionError("each option is an object with a label and effects")
        label = str(option.get("label") or "").strip()
        if not label:
            raise OptionError("each option has a label")
        oid = str(option.get("id") or f"o{i + 1}").strip().lower()
        if not _OPTION_ID.match(oid) or oid in seen:
            raise OptionError(f"option id {oid!r} must be unique: a-z, 0-9, _ or -")
        seen.add(oid)
        effects = option.get("effects") or []
        if not isinstance(effects, list) or len(effects) > MAX_EFFECTS:
            raise OptionError(f"an option has at most {MAX_EFFECTS} effects")
        out.append({"id": oid, "label": label[:120],
                    "detail": str(option.get("detail") or "").strip()[:500],
                    "effects": [normalize_effect(connection, project_id, e) for e in effects]})
    return out


# ── snapshot + signature ─────────────────────────────────────────────────────────────────────

def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def snapshot(connection: Connection, project_id: str, effects: list[dict]) -> dict[str, Any]:
    """{item_id: {status, assignee, updated_at}} for every item the effects touch, read now."""
    ids = sorted({i for e in effects for i in _target_ids(e)})
    if not ids:
        return {}
    rows = connection.execute(text(
        "SELECT id, status, assignee, updated_at FROM work_items "
        "WHERE project_id = :p AND id = ANY(:ids)"), {"p": project_id, "ids": ids}).mappings()
    return {r["id"]: {"status": r["status"], "assignee": r["assignee"],
                      "updated_at": _iso(r["updated_at"])} for r in rows}


def _payload(decision_id: str, option: Mapping[str, Any]) -> str:
    return json.dumps({"v": 1, "decision_id": decision_id, "option_id": option.get("id"),
                       "label": option.get("label"), "effects": option.get("effects"),
                       "target_snapshot": option.get("target_snapshot")},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sign_option(decision_id: str, option: Mapping[str, Any]) -> Optional[str]:
    """``hs1:<hex>``, or None when the server key cannot be read (the option then never runs)."""
    try:
        from ..identity.authn_support import keyed_hash

        return f"{SIGNATURE_SCHEME}:{keyed_hash(_LABEL, _payload(decision_id, option))}"
    except Exception:  # noqa: BLE001 — no key: unsigned, so it fails closed at choose time
        return None


def signature_valid(decision_id: str, option: Mapping[str, Any]) -> bool:
    given = option.get("sig")
    if not isinstance(given, str) or given.partition(":")[0] != SIGNATURE_SCHEME:
        return False
    expected = sign_option(decision_id, option)
    return expected is not None and hmac.compare_digest(expected, given)


# ── asking ───────────────────────────────────────────────────────────────────────────────────

def offer(connection: Connection, *, project_id: str, question: str, options: Any, asker: str,
          work_item_id: Optional[str] = None, context: str = "", risk_tier: int = 1,
          task_id: Optional[str] = None) -> dict[str, Any]:
    """Raise a choose-one decision whose options carry signed effects. Returns the card."""
    question = str(question or "").strip()
    if not question:
        raise OptionError("ask a question")
    shaped = normalize_options(connection, project_id, options)
    if work_item_id and connection.execute(text(
            "SELECT 1 FROM work_items WHERE id = :w AND project_id = :p"),
            {"w": work_item_id, "p": project_id}).first() is None:
        raise LookupError(f"no work item {work_item_id} in this project")
    for option in shaped:
        option["target_snapshot"] = snapshot(connection, project_id, option["effects"])
    recommendation = {
        "action": "choose_option",
        "asker": asker,
        "about_work_item_id": work_item_id,
        "summary": (str(context or "").strip()[:1000]
                    or f"{asker} offers {len(shaped)} options; pick one to carry it out"),
        "effect_options": shaped,
        "binding_version": 2,
    }
    decision_id = create_blocking_decision(
        connection, project_id, task_id, question[:500], recommendation, {},
        "agent_offered_options", risk_tier=max(0, min(int(risk_tier), 3)), default_action="hold")
    for option in shaped:
        sig = sign_option(decision_id, option)
        if sig:
            option["sig"] = sig
    connection.execute(text(
        "UPDATE decisions SET recommendation = COALESCE(recommendation, '{}'::jsonb) || "
        "jsonb_build_object('effect_options', CAST(:o AS JSONB)) WHERE id = :id"),
        {"o": json.dumps(shaped, default=str), "id": decision_id})
    emit_event(connection, project_id, "decision.options_offered", asker, "decision", decision_id,
               {"options": [o["id"] for o in shaped], "work_item_id": work_item_id,
                "signed": all("sig" in o for o in shaped)})
    return {"decision_id": decision_id, "options": shaped}


# ── choosing (DecisionService.transition) ───────────────────────────────────────────────────

def _rec(decision: Mapping[str, Any]) -> dict[str, Any]:
    rec = decision.get("recommendation")
    if isinstance(rec, str):
        try:
            rec = json.loads(rec)
        except ValueError:
            rec = {}
    return rec if isinstance(rec, dict) else {}


def is_option_decision(decision: Mapping[str, Any]) -> bool:
    return _rec(decision).get("action") == ACTION


def options_of(decision: Mapping[str, Any]) -> list[dict[str, Any]]:
    opts = _rec(decision).get("effect_options")
    return [o for o in opts if isinstance(o, dict)] if isinstance(opts, list) else []


def chosen(decision: Mapping[str, Any], choose: Optional[str]) -> dict[str, Any]:
    """The option the person picked, verified. Raises OptionError (the approval is refused)."""
    if not choose:
        raise OptionError("pick one of the options")
    option = next((o for o in options_of(decision) if o.get("id") == choose), None)
    if option is None:
        raise OptionError(f"this decision has no option {choose!r}")
    if not signature_valid(str(decision["id"]), option):
        raise OptionError("this option's effects are not the ones that were offered "
                          "(its signature does not verify), so nothing was carried out")
    return option


def _stale(connection: Connection, project_id: str, option: Mapping[str, Any]) -> set[str]:
    then = option.get("target_snapshot") or {}
    now = snapshot(connection, project_id, list(option.get("effects") or []))
    return {i for i in set(then) | set(now) if then.get(i) != now.get(i)}


def _descendants(connection: Connection, item_id: str) -> list[str]:
    return list(connection.execute(text(
        "WITH RECURSIVE down(id) AS (SELECT id FROM work_items WHERE parent_id = :i "
        "  UNION SELECT w.id FROM work_items w JOIN down ON w.parent_id = down.id) "
        "SELECT id FROM down"), {"i": item_id}).scalars())


def _run_effect(connection: Connection, settings: Any, project_id: str, effect: dict,
                actor: str, decision_id: str) -> dict[str, Any]:
    from . import work_items as wi

    kind = effect["type"]
    if kind == "comment":
        emit_event(connection, project_id, "work_item.commented", actor, "work_item",
                   effect["target"], {"body": effect["body"], "decision_id": decision_id})
        return {"commented": effect["target"]}
    if kind == "create":
        made = wi.create_item(connection, settings, project_id=project_id, title=effect["title"],
                              description=effect.get("description") or "",
                              parent_id=effect.get("parent"), assignee=effect.get("assignee"),
                              actor=actor)
        return {"created": made["id"], "human_key": made.get("human_key")}
    if kind == "update_status":
        wi.update_item(connection, settings, project_id=project_id, item_id=effect["target"],
                       fields={"status": effect["status"]}, actor=actor)
        return {"status": effect["status"]}
    if kind == "assign":
        wi.update_item(connection, settings, project_id=project_id, item_id=effect["target"],
                       fields={"assignee": effect["assignee"]}, actor=actor)
        return {"assignee": effect["assignee"]}
    if kind == "cancel_tree":
        cancelled = []
        for item_id in [effect["target"], *_descendants(connection, effect["target"])]:
            status = connection.execute(text("SELECT status FROM work_items WHERE id = :i"),
                                        {"i": item_id}).scalar()
            if status in wi.CLOSED:
                continue
            wi.set_status(connection, settings, project_id=project_id, item_id=item_id,
                          status="cancelled", actor=actor)
            cancelled.append(item_id)
        return {"cancelled": cancelled}
    raise OptionError(f"unknown effect {kind}")


def carry_out(connection: Connection, settings: Any, decision: Mapping[str, Any], *,
              choose: Optional[str], actor: str) -> list[dict[str, Any]]:
    """Run the chosen option's effects (the approve follow-on). Stale targets are skipped; an
    effect that fails is recorded failed (inside a savepoint, so it leaves nothing half done)."""
    option = chosen(decision, choose)
    project_id = str(decision["project_id"])
    decision_id = str(decision["id"])
    stale = _stale(connection, project_id, option)
    runs: list[dict[str, Any]] = []
    for index, effect in enumerate(option.get("effects") or []):
        touched = set(_target_ids(effect))
        if touched & stale:
            status, detail = "skipped_stale", {"changed": sorted(touched & stale)}
        else:
            try:
                with connection.begin_nested():
                    detail = _run_effect(connection, settings, project_id, effect, actor,
                                         decision_id)
                status = "done"
            except Exception as error:  # noqa: BLE001 — recorded as failed, never silent
                status, detail = "failed", {"error": f"{type(error).__name__}: {error}"[:400]}
        connection.execute(text(
            "INSERT INTO decision_effect_runs (id, project_id, decision_id, option_id, "
            "effect_index, effect_type, target_id, status, detail, actor) VALUES (:i, :p, :d, "
            ":o, :n, :t, :tg, :s, CAST(:det AS JSONB), :a)"),
            {"i": make_id("dfx"), "p": project_id, "d": decision_id, "o": option["id"],
             "n": index, "t": effect["type"], "tg": effect.get("target") or effect.get("parent"),
             "s": status, "det": json.dumps(detail, default=str), "a": actor})
        runs.append({"index": index, "type": effect["type"], "status": status, **detail})
    connection.execute(text(
        "UPDATE decisions SET recommendation = COALESCE(recommendation, '{}'::jsonb) || "
        "jsonb_build_object('chosen_option', CAST(:o AS TEXT)) WHERE id = :id"),
        {"o": option["id"], "id": decision_id})
    emit_event(connection, project_id, "decision.option_carried_out", actor, "decision",
               decision_id, {"option": option["id"], "label": option.get("label"),
                             "done": sum(r["status"] == "done" for r in runs),
                             "skipped_stale": sum(r["status"] == "skipped_stale" for r in runs),
                             "failed": sum(r["status"] == "failed" for r in runs)})
    return runs


def effect_runs(connection: Connection, decision_id: str) -> list[dict[str, Any]]:
    return [{**dict(r), "created_at": _iso(r["created_at"])} for r in connection.execute(text(
        "SELECT option_id, effect_index, effect_type, target_id, status, detail, actor, "
        "created_at FROM decision_effect_runs WHERE decision_id = :d "
        "ORDER BY effect_index"), {"d": decision_id}).mappings()]


#: Few words per effect, for the card and the Approve button's tooltip.
def describe_effect(effect: Mapping[str, Any]) -> str:
    kind = effect.get("type")
    if kind == "comment":
        return f"Comment on {effect.get('target')}"
    if kind == "create":
        return f"Create “{effect.get('title')}”"
    if kind == "update_status":
        return f"Set {effect.get('target')} to {effect.get('status')}"
    if kind == "assign":
        return f"Assign {effect.get('target')} to {effect.get('assignee')}"
    if kind == "cancel_tree":
        return f"Cancel {effect.get('target')} and its open children"
    return str(kind)


__all__ = ["ACTION", "EFFECT_TYPES", "MAX_OPTIONS", "OptionError", "carry_out", "chosen",
           "describe_effect", "effect_runs", "is_option_decision", "offer", "options_of",
           "sign_option", "signature_valid"]
