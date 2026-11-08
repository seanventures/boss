"""The work board: ambient work items, their lifecycle, owner leases and links (BOSSAPP-908 core).

THE BOARD IS work_items (decision recorded in docs/product/AUTONOMOUS_AGENTS_SPEC.md §2)
---------------------------------------------------------------------------------------
work_orchestration already keeps a ranked, scored, admitted portfolio of work items that
"almost nothing feeds" (design §3.2), and BOSSAPP-830 makes the work item the canonical node.
A second backlog table would split the portfolio in two and make "what is BOSS working on" two
answers again. So an ambient item is an ordinary work item, created through
WorkOrchestrationService.upsert_work_item (the same scoring, the same `work.priority_scored`
event, the same evaluate_admission and rank_by_value_density), plus the ambient columns:
fingerprint, lifecycle, task class, autonomy level, the owner lease and the loop-guard fields.
Its portfolio `status` is 'active' while it is live and 'done' once it ends, so admission and
release planning read it with no change.

LIFECYCLE
---------
    proposed ──claim──► claimed ──start──► in_progress ──► resolved | failed
        │                  │                    │
        └──────────────────┴────────────────────┴──► needs_you ──answer──► (back to work or end)
    any live state ──dismiss──► dismissed

`needs_you` is the one state a person must move: an ambient question or approval, linked to a
Decisions row so it shows in Command Center's Needs you (one queue, design §11.5).

LEASES AND THE PERSON WHO TAKES OVER (design §2.2 pattern 7, §4 L4)
------------------------------------------------------------------
An agent works an item only while it holds the item's lease: `claim` hands back a fencing
token, `renew` extends it, and `assert_lease` is what an agent calls before every external
effect. A person's action on the item or on its subject (`take_over`, `on_person_action`)
replaces the agent's lease with the person's at once — the agent's next `assert_lease` fails
with LeaseLost and it stops. That is the fix for Codex's "keeps firing after I took over"
(openai/codex #57009): BOSS never keeps working on something a person picked up.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import guards, policy
from . import task_classes as tc
from ._common import clean_text, emit, now, party, party_kind, person, serialize_row
from .signals import SEVERITIES

LIFECYCLES = ("proposed", "claimed", "in_progress", "needs_you", "resolved", "dismissed",
              "failed")
LIVE = ("proposed", "claimed", "in_progress", "needs_you")
TERMINAL = ("resolved", "dismissed", "failed")
#: Who may move an item where. A person may also dismiss or resolve from any live state.
TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"claimed", "needs_you", "dismissed", "resolved"}),
    "claimed": frozenset({"in_progress", "proposed", "needs_you", "dismissed", "resolved",
                          "failed"}),
    "in_progress": frozenset({"needs_you", "resolved", "failed", "proposed", "dismissed"}),
    "needs_you": frozenset({"proposed", "claimed", "in_progress", "resolved", "dismissed",
                            "failed"}),
}
LINK_KINDS = ("signal", "workflow_run", "code_task", "decision", "act", "incident", "meeting",
              "pull_request", "reservation", "alert", "recommendation")
DEFAULT_LEASE_MINUTES = 30
MAX_LEASE_MINUTES = 240


class LeaseLost(RuntimeError):
    """The caller no longer holds this item's lease (expired, released, or a person took over).
    Stop work on the item; do not retry the effect."""


class Conflict(RuntimeError):
    """The item is not in a state that allows this move."""


# ── reading ──────────────────────────────────────────────────────────────────────────────────

def get(connection: Connection, item_id: str, *, project_id: Optional[str] = None,
        for_update: bool = False) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT * FROM work_items WHERE id = :id AND ambient "
        "AND (CAST(:p AS TEXT) IS NULL OR project_id = :p)"
        + (" FOR UPDATE" if for_update else "")),
        {"id": item_id, "p": project_id}).mappings().first()
    return _out(row) if row is not None else None


def _must(connection: Connection, item_id: str, *, for_update: bool = True) -> dict[str, Any]:
    item = get(connection, item_id, for_update=for_update)
    if item is None:
        raise LookupError(f"no ambient work item {item_id}")
    return item


def live_for_fingerprint(connection: Connection, project_id: str,
                         fingerprint: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT * FROM work_items WHERE project_id = :p AND fingerprint = :f "
        "AND lifecycle = ANY(:live)"),
        {"p": project_id, "f": fingerprint, "live": list(LIVE)}).mappings().first()
    return _out(row) if row is not None else None


def links(connection: Connection, item_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT kind, ref, note, created_by, created_at FROM work_item_links "
        "WHERE work_item_id = :w ORDER BY created_at, kind, ref"), {"w": item_id}).mappings()
    return [serialize_row(r) for r in rows]


def _out(row: Mapping[str, Any]) -> dict[str, Any]:
    from ..work_value_scoring import annotate_work_item_scores

    data = serialize_row(row)
    data.pop("lease_token", None)          # a fencing token is the holder's alone
    return annotate_work_item_scores(data)


# ── opening an item ──────────────────────────────────────────────────────────────────────────

def open_item(connection: Connection, settings: Any = None, *, project_id: str,
              fingerprint: str, title: str, task_class: str, subject_ref: Optional[str] = None,
              app: Optional[str] = None, signal_ids: Iterable[str] = (),
              severity: str = "medium", description: str = "", causal_depth: int = 0,
              caused_by_run_id: Optional[str] = None, actor: str = "agent:chief_of_staff",
              scores: Optional[Mapping[str, int]] = None) -> dict[str, Any]:
    """Open the ONE live item for a problem, or join the one already open.

    Returns {"item", "outcome": "opened" | "joined" | "refused", "guard"?, "reason"?}. Joining
    links the new signals to the live item (the problem is the same; the evidence grows). A
    refusal is a loop guard's (guards.admit_new_item) and is evented; nothing is opened.
    `scores` are work_orchestration's drivers (strategic_value, customer_impact, urgency,
    cost_of_delay, risk_reduction, dependency_pressure, confidence: 0-100).
    """
    from ..work_orchestration import WorkOrchestrationService
    from ...config import get_settings

    klass = tc.task_class(task_class)
    fingerprint = clean_text(fingerprint, 200)
    if not fingerprint:
        raise ValueError("an ambient item needs the fingerprint of the problem it is about")
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                       {"k": f"boss-ambient-item:{project_id}:{fingerprint}"})
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
    signal_ids = list(dict.fromkeys(str(s) for s in signal_ids if s))
    if signal_ids:
        # Only this project's signals, or install-wide ones, may be held by its items.
        found = set(connection.execute(text(
            "SELECT id FROM signals WHERE id = ANY(:ids) AND (project_id = :p OR project_id IS "
            "NULL)"), {"ids": signal_ids, "p": project_id}).scalars())
        missing = [s for s in signal_ids if s not in found]
        if missing:
            raise LookupError(f"no signal {missing[0]} in this project")
    existing = live_for_fingerprint(connection, project_id, fingerprint)
    if existing is not None:
        for sid in signal_ids:
            link(connection, existing["id"], "signal", sid, actor=_actor_word(actor))
            _triage_signal(connection, sid, existing["id"])
        return {"item": get(connection, existing["id"]), "outcome": "joined"}
    verdict = guards.admit_new_item(connection, project_id=project_id, fingerprint=fingerprint,
                                    task_class=klass.key, causal_depth=causal_depth)
    if not verdict["ok"]:
        return {"item": None, "outcome": "refused", "guard": verdict["guard"],
                "reason": verdict["reason"]}
    level = policy.level_for(connection, project_id, klass.key)["level"]
    placeholder = f"ambient_{uuid.uuid4().hex}"
    service = WorkOrchestrationService(settings or get_settings())
    created = service.upsert_work_item(
        connection, project_id=project_id, subject_type="ambient", subject_id=placeholder,
        title=clean_text(title, 300) or klass.title, description=clean_text(description, 4000),
        rationale=f"{klass.title} · {level}", workload_class="ambient",
        evidence=[{"kind": "signal", "ref": s} for s in signal_ids], source="ambient",
        actor=_actor_word(actor), **{k: int(v) for k, v in (scores or {}).items()})
    item_id = created["id"]
    connection.execute(text(
        "UPDATE work_items SET subject_id = :id, ambient = true, fingerprint = :f, "
        "lifecycle = 'proposed', task_class = :c, autonomy_level = :l, risk = :r, severity = :s, "
        "subject_ref = :sr, app = :app, causal_depth = :d, caused_by_run_id = :cb "
        "WHERE id = :id"),
        {"id": item_id, "f": fingerprint, "c": klass.key, "l": level, "r": klass.risk,
         "s": severity, "sr": clean_text(subject_ref, 300) or None,
         "app": clean_text(app, 120) or None, "d": int(causal_depth), "cb": caused_by_run_id})
    for sid in signal_ids:
        link(connection, item_id, "signal", sid, actor=_actor_word(actor))
        _triage_signal(connection, sid, item_id)
    emit(connection, project_id, "ambient.item_opened", _actor_word(actor), "work_item", item_id,
         {"fingerprint": fingerprint, "task_class": klass.key, "autonomy_level": level,
          "signal_ids": signal_ids, "severity": severity})
    return {"item": get(connection, item_id), "outcome": "opened"}


def _triage_signal(connection: Connection, signal_id: str, item_id: str) -> None:
    connection.execute(text(
        "UPDATE signals SET work_item_id = :w, status = CASE WHEN status = 'new' THEN 'triaged' "
        "ELSE status END, updated_at = now() WHERE id = :s"), {"w": item_id, "s": signal_id})


def _actor_word(actor: str) -> str:
    """Event/created_by actor: the party string as written ('agent:sre', 'person:ceo', 'boss')."""
    return party(actor)


def link(connection: Connection, item_id: str, kind: str, ref: str, *, actor: str = "boss",
         note: Optional[str] = None) -> None:
    """Record what an item touched (idempotent). The read model draws these."""
    if kind not in LINK_KINDS:
        raise ValueError(f"link kind must be one of {', '.join(LINK_KINDS)}")
    project_id = connection.execute(text("SELECT project_id FROM work_items WHERE id = :w"),
                                    {"w": item_id}).scalar()
    if project_id is None:
        raise LookupError(f"no work item {item_id}")
    connection.execute(text(
        "INSERT INTO work_item_links (work_item_id, kind, ref, project_id, note, created_by) "
        "VALUES (:w, :k, :r, :p, :n, :a) ON CONFLICT DO NOTHING"),
        {"w": item_id, "k": kind, "r": str(ref), "p": project_id,
         "n": clean_text(note, 300) or None, "a": actor})


# ── leases ───────────────────────────────────────────────────────────────────────────────────

def claim(connection: Connection, item_id: str, *, owner: str,
          minutes: int = DEFAULT_LEASE_MINUTES) -> dict[str, Any]:
    """An agent takes the item. Returns {"item", "lease_token"}; the token fences every effect.

    Refused (Conflict) when ambient work is stopped, the item is not proposed/claimed, or someone
    else holds a lease that has not expired. A person's lease never expires: once a person has
    the item, no agent claims it back."""
    owner = party(owner, allow_boss=False)
    if party_kind(owner) != "agent":
        raise ValueError("an agent claims an item; a person takes it over (take_over)")
    item = _must(connection, item_id)
    if policy.is_stopped(connection, item["project_id"]):
        raise Conflict("ambient work is stopped here")
    if item["lifecycle"] not in ("proposed", "claimed"):
        raise Conflict(f"the item is {item['lifecycle']}; only a proposed item can be claimed")
    holder = connection.execute(text(
        "SELECT lease_owner, lease_kind, lease_expires_at FROM work_items WHERE id = :id"),
        {"id": item_id}).mappings().one()
    if holder["lease_kind"] == "person":
        raise Conflict(f"{holder['lease_owner']} has taken this over")
    if (holder["lease_owner"] and holder["lease_owner"] != owner
            and holder["lease_expires_at"] and holder["lease_expires_at"] > now()):
        raise Conflict(f"{holder['lease_owner']} holds this item")
    token = uuid.uuid4().hex
    minutes = max(1, min(int(minutes), MAX_LEASE_MINUTES))
    connection.execute(text(
        "UPDATE work_items SET lifecycle = CASE WHEN lifecycle = 'proposed' THEN 'claimed' ELSE "
        "lifecycle END, lease_owner = :o, lease_kind = 'agent', lease_token = :t, "
        "lease_expires_at = now() + make_interval(mins => :m), owner_actor = :o, "
        "attempts = attempts + 1, updated_at = now() WHERE id = :id"),
        {"o": owner, "t": token, "m": minutes, "id": item_id})
    _squad_add(connection, item_id, owner)
    from .collab import post

    post(connection, item_id, kind="claim", sender=owner, body={"lease_minutes": minutes},
         _internal=True)
    emit(connection, item["project_id"], "ambient.item_claimed", owner, "work_item", item_id,
         {"owner": owner, "lease_minutes": minutes})
    return {"item": get(connection, item_id), "lease_token": token}


def assert_lease(connection: Connection, item_id: str, *, owner: str, token: str,
                 lock: bool = False) -> dict[str, Any]:
    """Raise LeaseLost unless `owner` still holds a live lease on the item with `token`. Call it
    in the same transaction as the effect it guards (lock=True holds the row for that)."""
    row = connection.execute(text(
        "SELECT * FROM work_items WHERE id = :id AND ambient" + (" FOR UPDATE" if lock else "")),
        {"id": item_id}).mappings().first()
    if row is None:
        raise LeaseLost(f"no ambient work item {item_id}")
    if row["lease_owner"] != owner or row["lease_token"] != token:
        who = row["lease_owner"] or "nobody"
        raise LeaseLost(f"{owner} no longer holds {item_id} (held by {who})")
    if row["lease_kind"] == "agent" and (row["lease_expires_at"] is None
                                         or row["lease_expires_at"] <= now()):
        raise LeaseLost(f"{owner}'s lease on {item_id} expired")
    if row["lifecycle"] in TERMINAL:
        raise LeaseLost(f"{item_id} is {row['lifecycle']}")
    return _out(row)


def renew(connection: Connection, item_id: str, *, owner: str, token: str,
          minutes: int = DEFAULT_LEASE_MINUTES) -> dict[str, Any]:
    assert_lease(connection, item_id, owner=owner, token=token, lock=True)
    minutes = max(1, min(int(minutes), MAX_LEASE_MINUTES))
    connection.execute(text(
        "UPDATE work_items SET lease_expires_at = now() + make_interval(mins => :m), "
        "updated_at = now() WHERE id = :id"), {"m": minutes, "id": item_id})
    return _must(connection, item_id, for_update=False)


def release(connection: Connection, item_id: str, *, owner: str, token: str,
            reason: str = "") -> dict[str, Any]:
    """The holder lets go. A claimed (not started) item goes back to proposed."""
    item = assert_lease(connection, item_id, owner=owner, token=token, lock=True)
    _clear_lease(connection, item_id, back_to_proposed=item["lifecycle"] in ("claimed",
                                                                            "in_progress"))
    emit(connection, item["project_id"], "ambient.lease_released", owner, "work_item", item_id,
         {"owner": owner, "reason": reason})
    return _must(connection, item_id, for_update=False)


def _clear_lease(connection: Connection, item_id: str, *, back_to_proposed: bool) -> None:
    connection.execute(text(
        "UPDATE work_items SET lease_owner = NULL, lease_kind = NULL, lease_token = NULL, "
        "lease_expires_at = NULL, lifecycle = CASE WHEN :back THEN 'proposed' ELSE lifecycle END, "
        "updated_at = now() WHERE id = :id"), {"back": back_to_proposed, "id": item_id})


def take_over(connection: Connection, item_id: str, *, actor: str,
              reason: str = "") -> dict[str, Any]:
    """A person picks the item up: the agent's lease is cancelled at once and the person holds
    it (no expiry). The agent's next assert_lease raises LeaseLost."""
    who = person(actor)
    item = _must(connection, item_id)
    if item["lifecycle"] in TERMINAL:
        raise Conflict(f"the item is already {item['lifecycle']}")
    previous = item.get("lease_owner")
    connection.execute(text(
        "UPDATE work_items SET lease_owner = :o, lease_kind = 'person', lease_token = :t, "
        "lease_expires_at = NULL, owner_actor = :o, updated_at = now() WHERE id = :id"),
        {"o": who, "t": uuid.uuid4().hex, "id": item_id})
    _squad_add(connection, item_id, who)
    from .collab import post

    post(connection, item_id, kind="handoff", sender="boss",
         body={"to": who, "from": previous, "summary": clean_text(reason, 300) or
               "A person took this over"})
    emit(connection, item["project_id"], "ambient.lease_taken_over", who, "work_item", item_id,
         {"from": previous, "to": who, "reason": reason})
    return _must(connection, item_id, for_update=False)


def on_person_action(connection: Connection, *, project_id: str, subject_ref: str, actor: str,
                     what: str) -> list[str]:
    """A person acted on a SUBJECT (committed to the branch, commented on the PR, cancelled the
    run): every live ambient item on that subject whose lease an agent holds is taken over.
    Returns the item ids. This is the hook webhooks and cancel paths call (design §4 L4)."""
    ids = connection.execute(text(
        "SELECT id FROM work_items WHERE project_id = :p AND ambient AND subject_ref = :s "
        "AND lifecycle = ANY(:live) AND COALESCE(lease_kind, '') <> 'person'"),
        {"p": project_id, "s": subject_ref, "live": list(LIVE)}).scalars().all()
    for item_id in ids:
        take_over(connection, item_id, actor=actor, reason=f"{person(actor)} {what}")
    return list(ids)


def cancel_agent_leases(connection: Connection, *, project_id: Optional[str], actor: str,
                        reason: str) -> list[dict[str, Any]]:
    """Every agent lease (in one project, or everywhere) cancelled: the stop's teeth."""
    rows = connection.execute(text(
        "UPDATE work_items SET lease_owner = NULL, lease_kind = NULL, lease_token = NULL, "
        "lease_expires_at = NULL, lifecycle = CASE WHEN lifecycle IN ('claimed', 'in_progress') "
        "THEN 'proposed' ELSE lifecycle END, updated_at = now() WHERE ambient AND "
        "lease_kind = 'agent' AND lifecycle = ANY(:live) "
        "AND (CAST(:p AS TEXT) IS NULL OR project_id = :p) RETURNING id, project_id"),
        {"p": project_id, "live": list(LIVE)}).mappings().all()
    for row in rows:
        emit(connection, row["project_id"], "ambient.lease_released", actor, "work_item",
             row["id"], {"reason": reason, "cancelled": True})
    return [dict(r) for r in rows]


def expire_leases(connection: Connection, *, project_id: Optional[str] = None) -> list[str]:
    """Agent leases past their time: the item goes back to proposed for the next claim."""
    rows = connection.execute(text(
        "UPDATE work_items SET lease_owner = NULL, lease_kind = NULL, lease_token = NULL, "
        "lease_expires_at = NULL, lifecycle = CASE WHEN lifecycle IN ('claimed', 'in_progress') "
        "THEN 'proposed' ELSE lifecycle END, updated_at = now() WHERE ambient AND "
        "lease_kind = 'agent' AND lease_expires_at <= now() AND lifecycle = ANY(:live) "
        "AND (CAST(:p AS TEXT) IS NULL OR project_id = :p) RETURNING id, project_id"),
        {"p": project_id, "live": list(LIVE)}).mappings().all()
    for row in rows:
        emit(connection, row["project_id"], "ambient.lease_expired", "boss", "work_item",
             row["id"], {})
    return [r["id"] for r in rows]


def _squad_add(connection: Connection, item_id: str, who: str) -> None:
    connection.execute(text(
        "UPDATE work_items SET squad = CASE WHEN squad @> CAST(:one AS JSONB) THEN squad ELSE "
        "squad || CAST(:one AS JSONB) END WHERE id = :id"),
        {"one": json.dumps([who]), "id": item_id})


# ── moving an item ───────────────────────────────────────────────────────────────────────────

def start(connection: Connection, item_id: str, *, owner: str, token: str) -> dict[str, Any]:
    """claimed -> in_progress, under the concurrency guard."""
    item = assert_lease(connection, item_id, owner=owner, token=token, lock=True)
    if item["lifecycle"] == "in_progress":
        return item
    if item["lifecycle"] != "claimed":
        raise Conflict(f"the item is {item['lifecycle']}; claim it first")
    verdict = guards.admit_start(connection, project_id=item["project_id"],
                                 task_class=item.get("task_class"))
    if not verdict["ok"]:
        raise Conflict(verdict["reason"])
    return _transition(connection, item, "in_progress", actor=owner)


def needs_you(connection: Connection, item_id: str, *, actor: str, question: str,
              recommendation: Mapping[str, Any], options: Iterable[Mapping[str, Any]] = (),
              risk_tier: int = 1, cost_of_waiting: str = "",
              token: Optional[str] = None) -> dict[str, Any]:
    """Put the item in front of a person: one Decisions row (Command Center's Needs you) linked to
    the item. `recommendation` is {action, ...}; options are [{key, label}]. An agent holding the
    lease passes its token; BOSS's own gate (actor 'boss') does not need one."""
    from ..core import create_blocking_decision

    item = _must(connection, item_id)
    if party_kind(party(actor)) == "agent":
        if not token:
            raise LeaseLost("an agent asks a person only while it holds the item")
        assert_lease(connection, item_id, owner=actor, token=token)
    if item["lifecycle"] in TERMINAL:
        raise Conflict(f"the item is already {item['lifecycle']}")
    subject = plain_subject(item)
    asked = clean_text(question, 300)
    decision_id = create_blocking_decision(
        connection, item["project_id"], None,
        f"{asked} — {subject}" if subject and subject.lower() not in asked.lower() else asked,
        {**dict(recommendation), "work_item_id": item_id, "task_class": item["task_class"],
         "autonomy_level": item["autonomy_level"], "asked_by": actor,
         "cost_of_waiting": clean_text(cost_of_waiting, 300)},
        {"ambient_cost_usd_micros": int(item.get("cost_usd_micros") or 0)},
        "ambient_needs_you", risk_tier=int(risk_tier), default_action="hold")
    option_list = [{"key": str(o.get("key")), "label": clean_text(o.get("label"), 80)}
                   for o in options if o.get("key")]
    if option_list:
        connection.execute(text("UPDATE decisions SET options = CAST(:o AS JSONB) WHERE id = :d"),
                           {"o": json.dumps(option_list), "d": decision_id})
    link(connection, item_id, "decision", decision_id, actor=actor)
    connection.execute(text(
        "UPDATE work_items SET needs_you_decision_id = :d, needs_you_at = now(), "
        "answered_at = NULL WHERE id = :id"), {"d": decision_id, "id": item_id})
    moved = _transition(connection, _must(connection, item_id), "needs_you", actor=actor,
                        extra={"decision_id": decision_id})
    return {**moved, "needs_you_decision_id": decision_id}


_RAW_NAME = re.compile(r"^[a-z]{2,6}_[0-9A-Za-z]{6,}$")


def plain_subject(item: Mapping[str, Any]) -> Optional[str]:
    """What an item is about, in the words a person uses: a file's short name, a repository's
    name, an app, a pull request number. Never a raw key (`module:acme/web:src/a/b.jsx`,
    `run:wfr_…`, `subject:stage:solo`): a subject with no plain name gives None, and the
    question stands on its own words."""
    ref = str(item.get("subject_ref") or "")
    kind, _, rest = ref.partition(":")
    if kind == "app" or (not ref and item.get("app")):
        name = clean_text(item.get("app") or rest, 80)
        # A raw id ("svc_0d4d…", "deptgt_…") is not a name a person knows.
        return None if not name or _RAW_NAME.match(name) else name
    if kind == "module":
        path = rest.split(":", 1)[-1]
        return clean_text(path.rstrip("/").rsplit("/", 1)[-1], 80) or None
    if kind == "repo":
        return clean_text(rest.rstrip("/").rsplit("/", 1)[-1], 80) or None
    if kind == "pr":
        number = rest.rsplit("#", 1)[-1] if "#" in rest else ""
        return f"PR #{number}" if number.isdigit() else None
    if kind == "dep":
        return clean_text(rest, 80) or None
    if kind == "host":
        return "this install" if rest in ("install", "") else clean_text(rest, 80)
    return None


def needs_you_batch(connection: Connection, held: Iterable[tuple[str, str]], *, actor: str,
                    question: str, recommendation: Mapping[str, Any],
                    options: Iterable[Mapping[str, Any]] = (), risk_tier: int = 1,
                    cost_of_waiting: str = "") -> dict[str, Any]:
    """Put SEVERAL items in front of a person on ONE Decisions row: one card, one answer, every
    item linked to it and waiting on it. `held` is [(item_id, lease token)] — the asker holds
    each item while it asks. The recommendation names the lead item (`work_item_id`) and every
    item (`work_item_ids`); the per-item detail rides in it (`batch`)."""
    from ..core import create_blocking_decision

    held = list(held)
    if not held:
        raise ValueError("a card asks about at least one item")
    items = []
    for item_id, token in held:
        item = _must(connection, item_id)
        assert_lease(connection, item_id, owner=actor, token=token)
        if item["lifecycle"] in TERMINAL:
            raise Conflict(f"the item is already {item['lifecycle']}")
        items.append(item)
    lead = items[0]
    decision_id = create_blocking_decision(
        connection, lead["project_id"], None, clean_text(question, 300),
        {**dict(recommendation), "work_item_id": lead["id"],
         "work_item_ids": [i["id"] for i in items], "task_class": lead["task_class"],
         "autonomy_level": lead["autonomy_level"], "asked_by": actor,
         "cost_of_waiting": clean_text(cost_of_waiting, 300)},
        {"ambient_cost_usd_micros": sum(int(i.get("cost_usd_micros") or 0) for i in items)},
        "ambient_needs_you", risk_tier=int(risk_tier), default_action="hold")
    option_list = [{"key": str(o.get("key")), "label": clean_text(o.get("label"), 80)}
                   for o in options if o.get("key")]
    if option_list:
        connection.execute(text("UPDATE decisions SET options = CAST(:o AS JSONB) WHERE id = :d"),
                           {"o": json.dumps(option_list), "d": decision_id})
    for item in items:
        link(connection, item["id"], "decision", decision_id, actor=actor)
        connection.execute(text(
            "UPDATE work_items SET needs_you_decision_id = :d, needs_you_at = now(), "
            "answered_at = NULL WHERE id = :id"), {"d": decision_id, "id": item["id"]})
        _transition(connection, _must(connection, item["id"]), "needs_you", actor=actor,
                    extra={"decision_id": decision_id})
    return {"needs_you_decision_id": decision_id, "items": [i["id"] for i in items]}


def unask(connection: Connection, item_id: str, *, actor: str, reason: str) -> dict[str, Any]:
    """The question an item waits on was WITHDRAWN (nobody answered it; the policy no longer
    asks): the item stops waiting on a person and goes back on the board, unanswered."""
    item = _must(connection, item_id)
    if item["lifecycle"] != "needs_you":
        return item
    # BACK ON THE BOARD MEANS FREE. The lease of the turn that asked (an agent's, never a
    # person's) is let go with the question, so whoever works it next — the squad's owed turn,
    # the Chief of Staff settling it — claims it on the next tick instead of waiting out a lease
    # taken before the ask (boss-dogfood 2026-11-07: a 60-minute lease from 23:05 outlived the
    # withdrawal at 23:28).
    connection.execute(text(
        "UPDATE work_items SET needs_you_decision_id = NULL, needs_you_at = NULL, "
        "lease_owner = CASE WHEN lease_kind = 'agent' THEN NULL ELSE lease_owner END, "
        "lease_token = CASE WHEN lease_kind = 'agent' THEN NULL ELSE lease_token END, "
        "lease_expires_at = CASE WHEN lease_kind = 'agent' THEN NULL ELSE lease_expires_at END, "
        "lease_kind = CASE WHEN lease_kind = 'agent' THEN NULL ELSE lease_kind END "
        "WHERE id = :id"), {"id": item_id})
    return _transition(connection, _must(connection, item_id), "proposed", actor=actor,
                       extra={"withdrawn": clean_text(reason, 300)})


def answered(connection: Connection, item_id: str, *, actor: str,
             to: Optional[str] = None) -> dict[str, Any]:
    """A person answered the item's Needs-you decision: record when, and move it on — back to
    the agent still holding a live lease ('claimed'), else to 'proposed' for the next claim, or
    straight to an end (`to` = resolved / dismissed / failed)."""
    item = _must(connection, item_id)
    if item["lifecycle"] != "needs_you":
        raise Conflict(f"the item is {item['lifecycle']}, not waiting on a person")
    connection.execute(text("UPDATE work_items SET answered_at = now() WHERE id = :id"),
                       {"id": item_id})
    if to in TERMINAL:
        return finish(connection, item_id, actor=person(actor), lifecycle=to,
                      outcome=f"{to} by {person(actor)}")
    held = connection.execute(text(
        "SELECT lease_kind = 'agent' AND lease_expires_at > now() FROM work_items "
        "WHERE id = :id"), {"id": item_id}).scalar()
    return _transition(connection, _must(connection, item_id), to or (
        "claimed" if held else "proposed"), actor=person(actor))


def finish(connection: Connection, item_id: str, *, actor: str, lifecycle: str,
           outcome: str = "", token: Optional[str] = None) -> dict[str, Any]:
    """End an item: resolved, failed or dismissed. An agent must hold the lease; a person or BOSS
    may end any live item. Ending releases the lease."""
    if lifecycle not in TERMINAL:
        raise ValueError(f"an item ends as {', '.join(TERMINAL)}")
    who = party(actor)
    item = _must(connection, item_id)
    if party_kind(who) == "agent":
        if not token:
            raise LeaseLost("an agent ends an item only while it holds it")
        assert_lease(connection, item_id, owner=who, token=token, lock=True)
    if lifecycle not in TRANSITIONS.get(item["lifecycle"], frozenset()):
        raise Conflict(f"an item cannot go from {item['lifecycle']} to {lifecycle}")
    connection.execute(text(
        "UPDATE work_items SET outcome = :o, resolved_by = :a, resolved_at = now(), "
        "lease_owner = NULL, lease_kind = NULL, lease_token = NULL, lease_expires_at = NULL "
        "WHERE id = :id"), {"o": clean_text(outcome, 300) or lifecycle, "a": who, "id": item_id})
    moved = _transition(connection, item, lifecycle, actor=who)
    from .collab import post

    post(connection, item_id, kind="resolve", sender=who, _internal=True,
         body={"outcome": lifecycle, "summary": clean_text(outcome, 300)})
    connection.execute(text(
        "UPDATE signals SET status = 'done', status_reason = :r, updated_at = now() "
        "WHERE work_item_id = :w AND status IN ('new', 'triaged')"),
        {"r": f"work item {lifecycle}", "w": item_id})
    if lifecycle == "failed":
        guards.check_breaker(connection, item["project_id"])
    return moved


def dismiss(connection: Connection, item_id: str, *, actor: str, reason: str) -> dict[str, Any]:
    """A person says "not worth doing": the item ends dismissed, and its fingerprint cools down."""
    if not str(reason or "").strip():
        raise ValueError("say why it is dismissed: the reason teaches the triage (design §4 L6)")
    return finish(connection, item_id, actor=person(actor), lifecycle="dismissed",
                  outcome=f"dismissed: {clean_text(reason, 280)}")


def _transition(connection: Connection, item: Mapping[str, Any], to: str, *, actor: str,
                extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    current = item["lifecycle"]
    if to == current:
        return _must(connection, item["id"], for_update=False)
    if to not in TRANSITIONS.get(current, frozenset()):
        raise Conflict(f"an item cannot go from {current} to {to}")
    connection.execute(text(
        "UPDATE work_items SET lifecycle = :to, status = CASE WHEN :to = ANY(:terminal) THEN "
        "'done' ELSE 'active' END, updated_at = now() WHERE id = :id"),
        {"to": to, "terminal": list(TERMINAL), "id": item["id"]})
    emit(connection, item["project_id"], "ambient.item_transitioned", actor, "work_item",
         item["id"], {"from": current, "to": to, **(extra or {})})
    return _must(connection, item["id"], for_update=False)


def add_cost(connection: Connection, item_id: str, usd_micros: int) -> None:
    """Attribute spend to the item (the run settles; the board shows the total)."""
    connection.execute(text(
        "UPDATE work_items SET cost_usd_micros = cost_usd_micros + :c WHERE id = :id"),
        {"c": max(0, int(usd_micros)), "id": item_id})


def list_items(connection: Connection, project_id: str, *, lifecycle: Iterable[str] = (),
               task_class: Optional[str] = None, limit: int = 50,
               before: Optional[str] = None) -> list[dict[str, Any]]:
    states = [s for s in lifecycle if s in LIFECYCLES] or list(LIFECYCLES)
    rows = connection.execute(text(
        "SELECT * FROM work_items WHERE project_id = :p AND ambient AND lifecycle = ANY(:s) "
        "AND (CAST(:c AS TEXT) IS NULL OR task_class = :c) "
        "AND (CAST(:b AS TIMESTAMPTZ) IS NULL OR updated_at < :b) "
        "ORDER BY updated_at DESC, id DESC LIMIT :n"),
        {"p": project_id, "s": states, "c": task_class, "b": before,
         "n": max(1, min(int(limit), 200))}).mappings()
    return [_out(r) for r in rows]


__all__ = [
    "LIFECYCLES", "LIVE", "TERMINAL", "LeaseLost", "Conflict", "get", "live_for_fingerprint",
    "links", "open_item", "link", "claim", "assert_lease", "renew", "release", "take_over",
    "on_person_action", "cancel_agent_leases", "expire_leases", "start", "needs_you", "answered",
    "needs_you_batch", "unask", "plain_subject",
    "finish", "dismiss", "add_cost", "list_items",
]
