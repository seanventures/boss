"""Signed work: one ledger of who did what, who handed it to them, and how (spec §4.3, §5.1).

WHY ONE LEDGER
--------------
"Who did this" had eight answers in eight tables: a step's `output.ran_as`, a build stage's
`llm_calls.stage` pushed through STAGE_ROLES, a panel's `meeting_runs.role_keys`, a chat's
`agent_messages.agent_role`, a gate's `decisions.approved_by`, an A2A envelope's sender. A
screen that wanted to draw a run's chain, an agent's history or "who is working right now" had
to stitch all eight at read time, and every screen stitched them differently. `agent_acts` is
the one place they meet: every unit of work writes ONE row, with the edge that brought the work
to it (`parent_act_id`, `handed_by_*`, `how`), and every reader reads that row.

THE WRITE POINTS, AND WHY MOST OF THEM ARE A SYNC
-------------------------------------------------
A workflow step changes state in twenty places (claim, complete, fail, park, gate, skip, retry,
force-complete, sweep ...). Hooking each one is how a write point gets missed and a chain gets a
hole. So a run's acts are materialised from the run's own rows by ONE idempotent function,
`sync_run`, called where a run moves (start, every tick, a decision answered, a read of the
chain). It never invents: every act it writes is the image of a `workflow_step_runs` row, its
`ran_as`, and the spec's edges, and the parent is resolved when the act is first written — the
act of the step whose completion fired this step's edge — and stored, never re-derived.

The places whose truth is NOT on a step row write directly through `open_act` / `close_act`:
build stages (`stage_act`, from the telemetry chokepoint every model call already passes),
panel seats (`sync_meeting`), dock chat turns (`open_chat_act`) and agent-to-agent handoffs
(`record_handoff`, the ONLY path that may write `asked_by_agent`).

COST lands where it was spent: `llm_calls.act_id` is written at the chokepoint, and closing an
act rolls its calls up. An act's cost is its own calls only, so a chain's total is a plain sum
and nothing is counted twice.

Nothing here is allowed to break the work it records: every hook is wrapped by its caller in a
savepoint and a failure is logged, never raised into a build, a run or a chat turn.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .agent_persona import persona_of
from .agent_world_types import ALIASES, EDGE_LABELS

log = logging.getLogger("boss.agent_acts")

KINDS = frozenset({"run", "step", "stage", "seat", "chat", "approval", "handoff"})
STATUSES = frozenset({"pending", "active", "awaiting_input", "complete", "error", "stale",
                      "skipped"})
OPEN_STATUSES = frozenset({"pending", "active", "awaiting_input"})
HOWS = frozenset(EDGE_LABELS)
HANDED_BY = frozenset({"person", "agent", "boss"})
LINKS = ("workflow_run_id", "step_id", "code_task_run_id", "meeting_run_id", "thread_id",
         "decision_id", "receipt_id")

#: The step kinds that are WORK. condition / parallel / join / loop / timer are plumbing: they
#: have no agent and no act, and a parent is found by walking back through them.
ACTED_STEP_KINDS = frozenset({"capability", "decision", "subworkflow"})

#: How long an open act may sit with nothing recording it before presence stops calling its
#: agent busy. A crashed worker leaves an act open; "Working" for ever would be a lie. A wait
#: for a person (awaiting_input) is exempt: an approval can honestly wait a week.
LIVE_HORIZON_HOURS = 12

#: Build stages, in the order the governed pipeline runs them. Opening a stage closes the open
#: stages of other phases (the pipeline has moved on); stages of one phase run together (the
#: review panel) and stay open side by side. A stage opened again after it closed is a new act:
#: that is a repair round, and the chain shows it as one.
STAGE_PHASES: dict[str, int] = {
    "standards": 0, "spec": 0, "plan": 0, "review_plan": 0, "design": 0,
    "write_tests": 1,
    "implement": 2, "solo": 2, "guest": 2,
    "check_tests": 3, "run_all_tests": 3,
    "review_code": 4, "consistency_check": 4, "security_check": 4,
    "repair": 5,
}
STAGE_TITLES: dict[str, str] = {
    "standards": "Standards", "spec": "Spec", "plan": "Plan", "review_plan": "Plan review",
    "design": "Design", "write_tests": "Write tests", "implement": "Implement", "solo": "Build",
    "guest": "Build", "check_tests": "Run tests", "run_all_tests": "Run tests",
    "review_code": "Code review", "consistency_check": "Consistency check",
    "security_check": "Security check", "repair": "Repair",
}
#: The stages an operator's choice of engineer takes. A build step that NAMES an agent names who
#: builds it — not who reviews it: crediting the review to the author would be the one lie a
#: review exists to prevent. The reviewers, testers and architects stay their stage's role.
ENGINEERING_STAGES = frozenset({"implement", "repair", "solo", "guest"})

_ACT_REF = re.compile(r"\[act:(act_[0-9A-Za-z]{10,40})\]")


# ── ids and labels ───────────────────────────────────────────────────────────────────────────

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_act_id() -> str:
    """'act_' + a ULID: time-ordered, so act ids sort the way the work happened."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "act_" + "".join(reversed(chars))


def edge_label(how: str, considered_n: int = 0, *, skill: Optional[str] = None,
               captain: Optional[str] = None, actor: Optional[str] = None,
               agent: Optional[str] = None) -> str:
    """The §9 words for an edge. The single source: the web renders this string, never its own.

    `considered_n` is how many agents BOSS weighed; "over n" counts the ones it did NOT pick, so
    a pick between three reads `Picked by BOSS over 2`. A person is "you" unless a name is given:
    `_label_for` gives one whenever the person is not the one reading.
    """
    template = EDGE_LABELS.get(how)
    if template is None:
        raise ValueError(f"unknown how {how!r}")
    if how == "model_pick" and int(considered_n or 0) <= 1:
        return "Picked by BOSS"  # an aggregate over picks of different sizes has no one n
    if actor and actor != "you" and how in _PERSON_WORDS:
        return _PERSON_WORDS[how].format(actor=actor)
    return template.format(
        n=max(int(considered_n or 0) - 1, 0),
        skill=skill or "its skill",
        captain=captain or "a captain",
        actor=actor or "you",
        agent=agent or "an agent",
    )


#: The edges a PERSON makes, worded for someone other than the reader. EDGE_LABELS (frozen in
#: phase 0) words them for the person who did it ("Approved by you"); a teammate reading the
#: same act must read whose approval it was, never their own name's stand-in.
_PERSON_WORDS: dict[str, str] = {
    "run_actor": "Ran as {actor}",
    "asked_by_person": "Asked by {actor}",
    "approved": "Approved by {actor}",
    "started": "Started by {actor}",
}

#: Who is accountable when no person started the work: BOSS's own machinery (a sweep, a retry,
#: the shepherd) or a build or panel whose starter was never recorded. Said as what it is —
#: never a made-up person such as "operator".
BOSS = "boss"


def _person_word(act: dict[str, Any], viewer: Optional[str]) -> str:
    """Who to name on a person's edge: "you" only when the reader IS that person."""
    if act.get("handed_by_kind") == "boss":
        return "BOSS"
    person = act.get("handed_by_id") or act.get("accountable")
    if not person or person == BOSS:
        return "BOSS"
    return "you" if viewer and str(person) == str(viewer) else str(person)


def _label_for(act: dict[str, Any], viewer: Optional[str] = None) -> str:
    how = str(act.get("how") or "")
    scope = act.get("scope") if isinstance(act.get("scope"), dict) else {}
    considered = act.get("considered") if isinstance(act.get("considered"), list) else []
    handed = act.get("handed_by_id")
    return edge_label(
        how, len(considered),
        skill=", ".join((scope.get("skills") or []) + (scope.get("mcp") or [])) or None,
        captain=handed if how == "captain" else None,
        # "you" from the person's own seat; anyone else reads that person's name.
        actor=_person_word(act, viewer) if how in _PERSON_WORDS else None,
        agent=handed if how == "asked_by_agent" else None,
    )


# ── where the work came from ─────────────────────────────────────────────────────────────────
#
# A build or a panel is started by a TOOL, in the tool's own transaction, while the step or the
# chat turn that called it is still uncommitted in another. So the starter cannot be found by
# looking: it is handed over. Whoever is about to call a tool that starts work says so with
# `origin(...)`; the tool writes it into the work's finalize_ctx["origin"]; the ledger reads it
# there. {act_id} for a chat turn, {step_run_id} for a workflow step, and always the
# accountable person as {actor}.

_ORIGIN: ContextVar[Optional[dict[str, Any]]] = ContextVar("boss_agent_acts_origin",
                                                           default=None)


@contextmanager
def origin(**fields: Any) -> Iterator[None]:
    """The work started inside this block was started by this act / step / person."""
    token = _ORIGIN.set({k: str(v) for k, v in fields.items() if v not in (None, "")})
    try:
        yield
    finally:
        _ORIGIN.reset(token)


def current_origin(actor: Optional[str] = None) -> dict[str, Any]:
    """The origin a tool should record, falling back to the person calling it."""
    found = dict(_ORIGIN.get() or {})
    if actor and not found.get("actor"):
        found["actor"] = str(actor)
    return found


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def resolve_role(key_or_title: Optional[str]) -> Optional[str]:
    """One namespace for seat titles, captain ids and stage roles (agent_identity.resolve); a
    custom agent's own key is not in the shipped table and passes through."""
    raw = str(key_or_title or "").strip()
    if not raw:
        return None
    from .agent_identity import resolve  # lazy: agent_identity reads this module's table

    found = resolve(raw)
    if found:
        return found
    lowered = raw.lower()
    return ALIASES.get(lowered) or lowered


def quietly(connection: Connection, what: str, fn, *args: Any, **kwargs: Any) -> Any:
    """Run a ledger write in its own savepoint. The work it records always wins: a failure here
    is logged and rolled back to the savepoint, never raised into a run, a build or a chat."""
    try:
        with connection.begin_nested():
            return fn(connection, *args, **kwargs)
    except Exception as error:  # noqa: BLE001 — the ledger must never break the work
        log.warning("agent acts: could not %s: %s: %s", what, type(error).__name__, error)
        return None


# ── the two writers every hook goes through ──────────────────────────────────────────────────


def _identity(connection: Connection, project_id: str,
              role_key: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[int]]:
    """The definition that acted: agent_id, fingerprint, version at THIS moment."""
    if not role_key:
        return None, None, None
    row = connection.execute(
        text("SELECT agent_id, fingerprint, identity_version FROM roles "
             "WHERE project_id = :p AND role_key = :r"),
        {"p": project_id, "r": role_key},
    ).mappings().first()
    if not row:
        return None, None, None
    return row["agent_id"], row["fingerprint"], row["identity_version"]


def _act_row(connection: Connection, act_id: Optional[str]) -> Optional[dict[str, Any]]:
    if not act_id:
        return None
    row = connection.execute(text("SELECT * FROM agent_acts WHERE act_id = :a"),
                             {"a": act_id}).mappings().first()
    return dict(row) if row else None


def _scope_ok(scope: dict[str, Any], parent: Optional[dict[str, Any]]) -> Optional[bool]:
    """scope ⊆ the parent's scope. None when the parent recorded no scope to compare with — a
    person, BOSS, or a step that names no skills — because "unknown" is not "fine"."""
    if not parent:
        return None
    theirs = _json(parent.get("scope"), {})
    mine_skills = set(scope.get("skills") or [])
    mine_mcp = set(scope.get("mcp") or [])
    if not mine_skills and not mine_mcp:
        return True
    if not (theirs.get("skills") or theirs.get("mcp")):
        return None
    return mine_skills <= set(theirs.get("skills") or []) and \
        mine_mcp <= set(theirs.get("mcp") or [])


def open_act(connection: Connection, *, project_id: str, kind: str, role_key: Optional[str],
             how: str, handed_by_kind: str, handed_by_id: Optional[str] = None,
             parent_act_id: Optional[str] = None, root_act_id: Optional[str] = None,
             why: Optional[str] = None, considered: Iterable[str] = (),
             scope: Optional[dict] = None, accountable: str, subject_kind: Optional[str] = None,
             subject_id: Optional[str] = None, label: str, status: str = "active",
             started_at: Any = None, reconstructed: bool = False,
             artifacts: Iterable[dict] = (), envelope_id: Optional[str] = None,
             act_id: Optional[str] = None, seat: Optional[int] = None,
             **links: Any) -> str:
    """Write one act; returns its act_id. `seat` is a review panel seat (1-based).

    `asked_by_agent` needs `envelope_id`: an agent is only ever said to have asked when an A2A
    envelope with sender_kind=agent proves it did. Everything else that moves work between
    agents is BOSS, a captain, or a person, and is labelled so.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown act kind {kind!r}")
    if how not in HOWS:
        raise ValueError(f"unknown how {how!r}")
    if handed_by_kind not in HANDED_BY:
        raise ValueError(f"unknown handed_by_kind {handed_by_kind!r}")
    if status not in STATUSES:
        raise ValueError(f"unknown act status {status!r}")
    if how == "asked_by_agent" and not envelope_id:
        raise ValueError("asked_by_agent needs the A2A envelope that proves an agent asked")
    unknown = set(links) - set(LINKS)
    if unknown:
        raise ValueError(f"unknown act links {sorted(unknown)}")
    if not str(accountable or "").strip():
        raise ValueError("an act needs the accountable human")
    act_id = act_id or new_act_id()
    parent = _act_row(connection, parent_act_id) if parent_act_id else None
    if parent_act_id and parent is None:
        raise ValueError(f"parent act {parent_act_id} does not exist")
    root = root_act_id or (parent or {}).get("root_act_id") or act_id
    scope = {"skills": list((scope or {}).get("skills") or []),
             "mcp": list((scope or {}).get("mcp") or []),
             "tools": list((scope or {}).get("tools") or [])}
    agent_id, fingerprint, version = _identity(connection, project_id, role_key)
    shipped = list(artifacts or [])
    if envelope_id:
        shipped.append({"kind": "a2a", "ref": envelope_id})
    connection.execute(
        text(
            """
            INSERT INTO agent_acts (
                act_id, project_id, role_key, agent_id, fingerprint, identity_version, kind,
                status, parent_act_id, root_act_id, handed_by_kind, handed_by_id, how, why,
                considered, scope, scope_ok, accountable, subject_kind, subject_id,
                workflow_run_id, step_id, code_task_run_id, meeting_run_id, thread_id,
                decision_id, receipt_id, label, artifacts, reconstructed, started_at, seat
            ) VALUES (
                :act_id, :project_id, :role_key, :agent_id, :fingerprint, :version, :kind,
                :status, :parent, :root, :hbk, :hbi, :how, :why,
                CAST(:considered AS JSONB), CAST(:scope AS JSONB), :scope_ok, :accountable,
                :subject_kind, :subject_id, :workflow_run_id, :step_id, :code_task_run_id,
                :meeting_run_id, :thread_id, :decision_id, :receipt_id, :label,
                CAST(:artifacts AS JSONB), :reconstructed, COALESCE(:started_at, now()), :seat
            )
            """
        ),
        {
            "act_id": act_id, "project_id": project_id, "role_key": role_key or None,
            "agent_id": agent_id, "fingerprint": fingerprint, "version": version,
            "kind": kind, "status": status, "parent": parent_act_id, "root": root,
            "hbk": handed_by_kind, "hbi": handed_by_id, "how": how, "why": why,
            "considered": json.dumps([str(c) for c in considered or ()]),
            "scope": json.dumps(scope), "scope_ok": _scope_ok(scope, parent),
            "accountable": str(accountable).strip(), "subject_kind": subject_kind,
            "subject_id": subject_id, "label": (label or kind)[:200],
            "artifacts": json.dumps(shipped), "reconstructed": bool(reconstructed),
            "started_at": started_at, "seat": int(seat) if seat else None,
            **{k: (str(links[k]) if links.get(k) not in (None, "") else None) for k in LINKS},
        },
    )
    return act_id


def close_act(connection: Connection, act_id: str, *, status: str,
              artifacts: Iterable[dict] = (), tokens_in: int = 0, tokens_out: int = 0,
              cost_usd: float = 0, ended_at: Any = None) -> None:
    """Close an open act once. Its model calls are rolled up from llm_calls by act_id, on top of
    whatever the caller measured itself (an agent loop's usage, which writes no llm_calls)."""
    if status in OPEN_STATUSES or status not in STATUSES:
        raise ValueError(f"an act closes as complete, error, stale or skipped, not {status!r}")
    connection.execute(
        text(
            """
            UPDATE agent_acts a SET
                status = :status,
                ended_at = COALESCE(:ended_at, now()),
                artifacts = a.artifacts || CAST(:artifacts AS JSONB),
                tokens_in = a.tokens_in + :ti + COALESCE(c.pt, 0),
                tokens_out = a.tokens_out + :to + COALESCE(c.ct, 0),
                cost_usd = a.cost_usd + :cost + COALESCE(c.micros, 0) / 1000000.0
            FROM (SELECT sum(prompt_tokens) AS pt, sum(completion_tokens) AS ct,
                         -- money only: a subscription or local call is worth its list price
                         -- but costs nothing (included_spend.py)
                         sum(cost_usd_micros) FILTER (WHERE NOT included_in_subscription)
                             AS micros
                  FROM llm_calls WHERE act_id = :act_id) c
            WHERE a.act_id = :act_id AND a.ended_at IS NULL
            """
        ),
        {"status": status, "ended_at": ended_at, "artifacts": json.dumps(list(artifacts or [])),
         "ti": int(tokens_in or 0), "to": int(tokens_out or 0), "cost": float(cost_usd or 0),
         "act_id": act_id},
    )


def _set_status(connection: Connection, act_id: str, status: str) -> None:
    connection.execute(
        text("UPDATE agent_acts SET status = :s WHERE act_id = :a AND ended_at IS NULL"),
        {"s": status, "a": act_id},
    )


def _fill_links(connection: Connection, act_id: str, **links: Any) -> None:
    """A link that only became known later (the code task a build step started, the panel a
    meeting step convened) is written once, into a NULL column. Never overwritten."""
    for key, value in links.items():
        if key in LINKS and value:
            connection.execute(
                text(f"UPDATE agent_acts SET {key} = :v WHERE act_id = :a AND {key} IS NULL"),
                {"v": str(value), "a": act_id},
            )


def _lock(connection: Connection, key: str, *, wait: bool = True) -> bool:
    """Serialise writers of one subject so two writers never open the same act twice. Held to
    the end of the caller's transaction. `wait=False` is for every caller inside a long
    transaction (the tick, the backfill) or with a person waiting (a read of the chain, an
    approval): if another transaction holds the subject, they skip — every sync is idempotent
    and the holder signs the same rows — rather than wait out a step that may run minutes."""
    if wait:
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                           {"k": f"agent_acts:{key}"})
        return True
    return bool(connection.execute(text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"),
                                   {"k": f"agent_acts:{key}"}).scalar())


def _close_open_children(connection: Connection, act_id: str, status: str) -> None:
    """A step that ended cannot still have a stage or a seat working under it."""
    open_children = connection.execute(
        text("SELECT act_id FROM agent_acts WHERE parent_act_id = :a AND ended_at IS NULL"),
        {"a": act_id},
    ).scalars().all()
    for child in open_children:
        _close_open_children(connection, child, status)
        close_act(connection, child, status=status)


# ── 1-3. a workflow run: its root, its steps, its gates ──────────────────────────────────────


def _run_row(connection: Connection, workflow_run_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text(
            """
            SELECT r.id, r.project_id, r.actor, r.status, r.parent_run_id, r.created_at,
                   r.completed_at, r.updated_at, r.workflow_version_id, r.input,
                   d.workflow_key, d.name AS workflow_name,
                   COALESCE(d.namespace, 'system') AS namespace
            FROM workflow_runs r JOIN workflow_definitions d ON d.id = r.workflow_definition_id
            WHERE r.id = :id
            """
        ),
        {"id": workflow_run_id},
    ).mappings().first()
    return dict(row) if row else None


def _run_label(run: dict[str, Any]) -> str:
    """The run's name in every act label: command_center.run_name — its subject, else its
    workflow's name — so Directory, Organization, Activity, Live and Approvals name a run the
    same way (agents-home D9). Never `<workflow> #<hash>`."""
    from .command_center import run_name

    data = run.get("input")
    if isinstance(data, str):
        data = _json(data, {})
    return run_name({**run, "input": data if isinstance(data, dict) else {}})


def root_for_run(connection: Connection, workflow_run_id: str, *,
                 reconstructed: bool = False) -> Optional[str]:
    """The run's kind='run' root act, created on first use.

    The person who started the run is at the root: every act under it is accountable to them.
    A child run ("Run a workflow") hangs off the step that started it, so a parent and its
    child are one connected chain. BOSS's own system workflows are not agent work and get none.
    """
    existing = connection.execute(
        text("SELECT act_id FROM agent_acts WHERE kind = 'run' AND workflow_run_id = :r "
             "ORDER BY started_at LIMIT 1"),
        {"r": workflow_run_id},
    ).scalar()
    if existing:
        return str(existing)
    run = _run_row(connection, workflow_run_id)
    if not run or run["namespace"] != "product":
        return None
    _lock(connection, f"run:{workflow_run_id}")
    existing = connection.execute(
        text("SELECT act_id FROM agent_acts WHERE kind = 'run' AND workflow_run_id = :r "
             "ORDER BY started_at LIMIT 1"),
        {"r": workflow_run_id},
    ).scalar()
    if existing:
        return str(existing)
    parent_act = None
    if run.get("parent_run_id"):
        parent_act = connection.execute(
            text(
                """
                SELECT a.act_id FROM workflow_step_runs s
                JOIN agent_acts a ON a.subject_kind = 'workflow_step_run' AND a.subject_id = s.id
                WHERE s.child_workflow_run_id = :r
                ORDER BY a.started_at DESC LIMIT 1
                """
            ),
            {"r": workflow_run_id},
        ).scalar()
    return open_act(
        connection, project_id=run["project_id"], kind="run", role_key=None, how="started",
        handed_by_kind="person", handed_by_id=run["actor"], parent_act_id=parent_act,
        accountable=run["actor"], subject_kind="workflow_run", subject_id=run["id"],
        label=_run_label(run), started_at=run["created_at"], workflow_run_id=run["id"],
        reconstructed=reconstructed,
    )


def _spec_of(connection: Connection, version_id: str) -> dict[str, Any]:
    raw = connection.execute(text("SELECT spec FROM workflow_versions WHERE id = :v"),
                             {"v": version_id}).scalar()
    spec = _json(raw, {})
    return spec if isinstance(spec, dict) else {}


def _predecessors(spec: dict[str, Any]) -> dict[str, list[str]]:
    from .workflows import _targets

    preds: dict[str, list[str]] = {}
    for step in spec.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for target in _targets(step):
            preds.setdefault(target, []).append(str(step.get("id")))
    return preds


def _depths(spec: dict[str, Any]) -> dict[str, int]:
    """How far each step sits from the entry, along its longest path, with loop-back edges cut.
    Acts are written in this order, so a step's act is always written after the act of the step
    that released it, whatever the timestamps say."""
    from .workflows import _targets

    steps = [s for s in spec.get("steps") or [] if isinstance(s, dict)]
    succ = {str(s.get("id")): _targets(s) for s in steps}
    back: set[tuple[str, str]] = set()
    seen: set[str] = set()
    on_path: set[str] = set()
    postorder: list[str] = []

    def walk(node: str) -> None:
        seen.add(node)
        on_path.add(node)
        for target in succ.get(node, []):
            if target in on_path:
                back.add((node, target))
            elif target not in seen and target in succ:
                walk(target)
        on_path.discard(node)
        postorder.append(node)

    entry = str(spec.get("entry_step") or (steps[0].get("id") if steps else ""))
    for node in [entry] + list(succ):
        if node in succ and node not in seen:
            walk(node)
    depth = {node: 0 for node in succ}
    for node in reversed(postorder):
        for target in succ.get(node, []):
            if target in depth and (node, target) not in back:
                depth[target] = max(depth[target], depth[node] + 1)
    return depth


def _step_name(step: dict[str, Any], step_id: str) -> str:
    from .workflows import step_title

    titled = step_title(step)
    if titled:
        return titled
    binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
    key = str(binding.get("capability") or "")
    if key:
        try:
            from .workflow_capabilities import get_capability

            return get_capability(key).label
        except Exception:  # noqa: BLE001 — a label is never worth a failure
            pass
    if binding.get("agent_work"):
        return "Agent task"
    if step.get("kind") == "decision":
        return "Approval"
    raw = str(binding.get("custom_step") or step_id)
    return raw.replace("_", " ").replace("-", " ").strip().capitalize() or step_id


def _is_gate(step: dict[str, Any]) -> bool:
    if step.get("kind") == "decision":
        return True
    binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
    key = str(binding.get("capability") or "")
    if not key:
        return False
    try:
        from .workflow_capabilities import CAPABILITY_KEYS, get_capability

        return key in CAPABILITY_KEYS and bool(get_capability(key).is_gate)
    except Exception:  # noqa: BLE001
        return False


def _step_state(row: dict[str, Any], output: dict[str, Any]) -> Optional[str]:
    """The act status a step row means. None = it has not started, so there is no act yet."""
    status = str(row.get("status") or "")
    if status in ("pending", "ready"):
        return None
    if status in ("leased", "running"):
        return "active"
    if status == "waiting":
        awaited = (output.get("await") or {}) if isinstance(output.get("await"), dict) else {}
        if row.get("decision_id") or output.get("status") == "needs_decision" \
                or awaited.get("kind") in ("agent_run", "decision"):
            return "awaiting_input"
        return "active"
    return {"succeeded": "complete", "skipped": "skipped", "failed": "error",
            "deadlettered": "error", "cancelled": "stale"}.get(status, "active")


#: ran_as.how → the act's how. The runner speaks a few more words than the ledger: a model pick
#: and its word-overlap fallback are both BOSS picking; the workflow running a step, or the
#: decision model answering one, is BOSS's own machinery acting for the person who started it.
_HOW_FROM_RAN_AS = {
    "named": "named", "only_holder": "only_holder", "only_candidate": "only_candidate",
    "model": "model_pick", "word_overlap": "model_pick", "model_pick": "model_pick",
    "captain": "captain", "run_actor": "run_actor", "workflow_runner": "run_actor",
    "legacy_workflow_runner": "run_actor", "decision_model": "run_actor",
}

#: What a failed step's agent was, when the failure came before its output could be written.
#: Filled by `note_ran_as` (the runner, at the moment it knows) and read once by the next sync.
_PENDING_RAN_AS: dict[str, dict[str, Any]] = {}


def note_ran_as(step_run_id: str, ran_as: Optional[dict[str, Any]]) -> None:
    if step_run_id and isinstance(ran_as, dict) and ran_as.get("agent"):
        if len(_PENDING_RAN_AS) > 512:
            _PENDING_RAN_AS.clear()
        _PENDING_RAN_AS[step_run_id] = dict(ran_as)


def _who(row: dict[str, Any], step: dict[str, Any], output: dict[str, Any],
         run: dict[str, Any], connection: Connection, project_id: str) -> Optional[dict[str, Any]]:
    """Who did this step and how the work reached them, from what the run recorded."""
    if _is_gate(step):
        named = str(step.get("captain") or "").strip().lower()
        return {"role_key": None, "how": "named" if named == "human" else "run_actor",
                "handed_by_kind": "boss", "handed_by_id": None,
                "why": "an approval waits for a person", "considered": [], "scope": {}}
    ran_as = output.get("ran_as") if isinstance(output.get("ran_as"), dict) else None
    if ran_as is None:
        ran_as = _PENDING_RAN_AS.pop(str(row["id"]), None)
    if ran_as is None:
        if step.get("kind") == "subworkflow":
            ran_as = {"agent": run["actor"], "how": "run_actor",
                      "why": "BOSS started the child workflow for the person who ran this one"}
        else:
            from .step_agents import named_agent

            named = named_agent(step) if isinstance(step, dict) else ""
            if not named or named == "human":
                return None  # nobody was ever given it: there is no work to sign
            ran_as = {"agent": named, "how": "named", "why": "named on the step"}
    how = _HOW_FROM_RAN_AS.get(str(ran_as.get("how") or ""), "named")
    agent = str(ran_as.get("agent") or "").strip().lower() or None
    role_key = None if how == "run_actor" else agent
    scope = {"skills": [str(s) for s in ran_as.get("skills_offered") or []],
             "mcp": [str(m) for m in ran_as.get("mcp_offered") or []],
             "tools": [str(ran_as["tool"])] if ran_as.get("tool") else []}
    return {
        "role_key": role_key, "how": how,
        "handed_by_kind": "person" if how == "run_actor" else (
            "agent" if how == "captain" else "boss"),
        "handed_by_id": run["actor"] if how == "run_actor" else (
            ran_as.get("assigned_by") if how == "captain" else None),
        "why": str(ran_as.get("why") or "")[:500] or None,
        "considered": [str(c) for c in ran_as.get("considered") or []],
        "scope": scope,
    }


def _awaited(output: dict[str, Any], kind: str) -> Optional[str]:
    awaited = output.get("await") if isinstance(output.get("await"), dict) else {}
    if awaited.get("kind") == kind and awaited.get("id"):
        return str(awaited["id"])
    field = {"code_task": "code_task_run_id", "meeting": "meeting_run_id"}[kind]
    value = output.get(field)
    return str(value) if value else None


def _agent_run_usage(connection: Connection, output: dict[str, Any]) -> tuple[int, int, float]:
    run_id = output.get("agent_run_id")
    if not run_id:
        return 0, 0, 0.0
    usage = _json(connection.execute(text("SELECT usage FROM agent_runs WHERE id = :i"),
                                     {"i": str(run_id)}).scalar(), {})
    return (int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0),
            int(usage.get("cost_usd_micros") or 0) / 1_000_000)


def _parent_for(step_id: str, preds: dict[str, list[str]],
                acts_by_step: dict[str, list[dict[str, Any]]], root: str) -> str:
    """The act of the step whose completion fired this step's edge.

    Walks back through the spec's edges; a control step (condition, parallel, join, loop,
    timer) has no act, so the walk goes on through it. Among the predecessors that finished,
    the one that finished LAST is the one that released this step. None finished: the root."""
    best: Optional[dict[str, Any]] = None
    stack = list(preds.get(step_id, []))
    seen: set[str] = set()
    while stack:
        candidate = stack.pop()
        if candidate in seen:
            continue
        seen.add(candidate)
        acts = acts_by_step.get(candidate) or []
        done = [a for a in acts if a.get("status") in ("complete", "skipped") and a.get("ended_at")]
        if done:
            last = max(done, key=lambda a: a["ended_at"])
            if best is None or last["ended_at"] > best["ended_at"]:
                best = last
        elif not acts:
            stack.extend(preds.get(candidate, []))
    return str(best["act_id"]) if best else root


#: Decision statuses that answer a gate with no: the work it held does not go on.
REFUSED_DECISIONS = ("rejected", "cancelled")


def sync_run(connection: Connection, workflow_run_id: str, *, wait: bool = True,
             reconstructed: bool = False, _depth: int = 0) -> list[str]:
    """Bring a run's acts up to date with its rows. Idempotent; returns the acts it touched.

    `wait=False` is for every caller inside a long transaction (the tick) or with a person
    waiting (a read, an approval): if another transaction holds this run, skip — the holder signs
    the same rows, and the next sync catches up. `reconstructed` is the backfill: its acts say
    so, and the historical step rows are read, never rewritten."""
    if not _lock(connection, f"run:{workflow_run_id}", wait=wait):
        return []
    root = root_for_run(connection, workflow_run_id, reconstructed=reconstructed)
    if not root:
        return []
    run = _run_row(connection, workflow_run_id)
    if not run:
        return []
    project_id = run["project_id"]
    spec = _spec_of(connection, run["workflow_version_id"])
    steps = {str(s.get("id")): s for s in spec.get("steps") or [] if isinstance(s, dict)}
    preds = _predecessors(spec)
    touched: list[str] = []

    acts = [dict(r) for r in connection.execute(
        text("SELECT * FROM agent_acts WHERE workflow_run_id = :r AND kind = 'step' "
             "ORDER BY started_at, act_id"),
        {"r": workflow_run_id},
    ).mappings().all()]
    by_subject: dict[str, list[dict[str, Any]]] = {}
    by_step: dict[str, list[dict[str, Any]]] = {}
    for act in acts:
        by_subject.setdefault(str(act["subject_id"]), []).append(act)
        by_step.setdefault(str(act["step_id"]), []).append(act)

    depths = _depths(spec)
    rows = [dict(r) for r in connection.execute(
        text(
            """
            SELECT id, step_id, step_kind, status, output, decision_id, child_workflow_run_id,
                   iteration, started_at, completed_at, created_at, updated_at,
                   (run_after IS NOT NULL AND run_after > now()) AS resumes_later
            FROM workflow_step_runs WHERE workflow_run_id = :r
            -- The order the work happened in. A gate is never claimed, so it has no started_at;
            -- it began when it went waiting (updated_at), after the step that released it.
            ORDER BY COALESCE(started_at, updated_at, created_at), id
            """
        ),
        {"r": workflow_run_id},
    ).mappings().all()]
    # CAUSAL ORDER, NOT CLOCK ORDER. A claimed step's started_at is clock_timestamp() and a
    # gate's updated_at is the transaction's now(), so the clocks disagree inside one tick. A
    # predecessor always sits nearer the entry than what it releases, so depth decides.
    rows.sort(key=lambda r: (int(r.get("iteration") or 0), depths.get(str(r["step_id"]), 0),
                             r.get("started_at") or r.get("updated_at") or r.get("created_at")))

    # A GATE SOMEBODY SAID NO TO IS OVER, whatever its row still says. Rejecting a gate leaves
    # the run blocked and the step row waiting, so its act stayed awaiting_input and presence,
    # the teams and Activity went on saying "needs you" while no card asked anything.
    refused = {str(d) for d in connection.execute(
        text("SELECT id FROM decisions WHERE id = ANY(:ids) AND status = ANY(:no)"),
        {"ids": [str(r["decision_id"]) for r in rows if r.get("decision_id")],
         "no": list(REFUSED_DECISIONS)},
    ).scalars().all()} if any(r.get("decision_id") for r in rows) else set()

    for row in rows:
        if row["step_kind"] not in ACTED_STEP_KINDS:
            continue
        output = _json(row["output"], {})
        output = output if isinstance(output, dict) else {}
        step = steps.get(str(row["step_id"])) or {"id": row["step_id"], "kind": row["step_kind"]}
        state = _step_state(row, output)
        if state == "awaiting_input" and str(row.get("decision_id") or "") in refused:
            state = "error"
        history = by_subject.get(str(row["id"])) or []
        latest = history[-1] if history else None
        terminal = state is not None and state not in OPEN_STATUSES

        if latest and latest["ended_at"] is None:
            # Released by its own approval and queued to run, or put back to wait out the
            # budget (workflows.wait_step: not a failure, not an attempt): the same work, still
            # coming.
            if state is None and row.get("decision_id") and _is_wait_act(latest):
                # THE PERSON'S WAIT IS OVER (agents-home D4): they answered, and the work the
                # gate held is the agent's own act, opened when it runs.
                close_act(connection, latest["act_id"], status="complete")
                latest.update(status="complete", ended_at=_now())
                touched.append(latest["act_id"])
                continue
            if state is None and (row.get("decision_id") or (
                    row.get("resumes_later") and isinstance(output.get("waiting"), dict))):
                if latest["status"] != "pending":
                    _set_status(connection, latest["act_id"], "pending")
                    latest["status"] = "pending"
                continue
            if state is None:
                # Put back to ready (a retry, a reopened step): this attempt is over.
                close_act(connection, latest["act_id"], status="stale")
                _close_open_children(connection, latest["act_id"], "stale")
                touched.append(latest["act_id"])
                continue
            _fill_links(connection, latest["act_id"],
                        code_task_run_id=_awaited(output, "code_task"),
                        meeting_run_id=_awaited(output, "meeting"),
                        decision_id=row.get("decision_id"))
            _adopt_build(connection, latest, _awaited(output, "code_task"))
            if terminal:
                ti, to, cost = _agent_run_usage(connection, output)
                close_act(connection, latest["act_id"], status=state, tokens_in=ti,
                          tokens_out=to, cost_usd=cost, ended_at=row.get("completed_at"),
                          artifacts=_artifacts_of(output))
                _close_open_children(connection, latest["act_id"],
                                     "complete" if state == "complete" else "stale")
                latest.update(status=state, ended_at=row.get("completed_at") or _now())
                touched.append(latest["act_id"])
            elif latest["status"] != state:
                _set_status(connection, latest["act_id"], state)
                latest["status"] = state
                touched.append(latest["act_id"])
        elif state is not None and not (latest and (
                latest["status"] == state or state == "skipped")):
            # No act yet, or the last attempt ended differently (a retry that now runs or
            # finished): a new act for this attempt.
            if state == "skipped" and not latest:
                continue  # never ran: a branch not taken is not work
            if state == "awaiting_input" and row.get("decision_id"):
                # A WAIT FOR A PERSON IS THE PERSON'S (agents-home D4): a step held at its
                # approval before it ran is not its agent's work — the agent did nothing yet,
                # and crediting the wait drew it amber beside the `You` hop.
                who = _who(row, {**step, "kind": "decision"}, output, run, connection,
                           project_id)
            else:
                who = _who(row, step, output, run, connection, project_id)
            if who is None:
                continue
            parent = _parent_for(str(row["step_id"]), preds, by_step, root)
            act_id = open_act(
                connection, project_id=project_id, kind="step", role_key=who["role_key"],
                how=who["how"], handed_by_kind=who["handed_by_kind"],
                handed_by_id=who["handed_by_id"], parent_act_id=parent, root_act_id=None,
                why=who["why"], considered=who["considered"], scope=who["scope"],
                accountable=run["actor"], subject_kind="workflow_step_run",
                subject_id=row["id"],
                label=f"{_step_name(step, str(row['step_id']))} · {_run_label(run)}",
                status=state if not terminal else "active",
                started_at=row.get("started_at") or row.get("created_at"),
                reconstructed=reconstructed,
                workflow_run_id=workflow_run_id, step_id=row["step_id"],
                code_task_run_id=_awaited(output, "code_task"),
                meeting_run_id=_awaited(output, "meeting"),
                decision_id=row.get("decision_id"),
            )
            act = _act_row(connection, act_id) or {}
            _adopt_build(connection, act, _awaited(output, "code_task"))
            if terminal:
                ti, to, cost = _agent_run_usage(connection, output)
                close_act(connection, act_id, status=state, tokens_in=ti, tokens_out=to,
                          cost_usd=cost, ended_at=row.get("completed_at"),
                          artifacts=_artifacts_of(output))
                act.update(status=state, ended_at=row.get("completed_at") or _now())
            by_subject.setdefault(str(row["id"]), []).append(act)
            by_step.setdefault(str(row["step_id"]), []).append(act)
            touched.append(act_id)
            if not reconstructed:
                _stamp_output(connection, row["id"], act_id)

        current = (by_subject.get(str(row["id"])) or [None])[-1]
        if current and row.get("decision_id"):
            approved = _record_approval(connection, run, current, str(row["decision_id"]),
                                        reconstructed=reconstructed)
            if approved:
                touched.append(approved)
        meeting = _awaited(output, "meeting")
        if meeting:
            touched.extend(sync_meeting(connection, meeting, wait=wait,
                                        reconstructed=reconstructed))
        if row.get("child_workflow_run_id") and _depth < 3:
            touched.extend(sync_run(connection, str(row["child_workflow_run_id"]),
                                    wait=wait, reconstructed=reconstructed, _depth=_depth + 1))

    _close_root(connection, root, run)
    return touched


def _artifacts_of(output: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for key, kind in (("pull_request_url", "pr"), ("pr_url", "pr"),
                      ("minutes_artifact_id", "minutes"), ("report_artifact_id", "report")):
        value = output.get(key)
        if value:
            item = {"kind": kind, "ref": str(value)}
            if str(value).startswith("http"):
                item["url"] = str(value)
            found.append(item)
    return found


def _stamp_output(connection: Connection, step_run_id: str, act_id: str) -> None:
    """act_id is written into the step's output next to ran_as, so a reader of the run holds
    the key to its signature without a second query."""
    connection.execute(
        text(
            """
            UPDATE workflow_step_runs SET output =
                COALESCE(output, '{}'::jsonb) || jsonb_build_object('act_id', CAST(:a AS text))
                || CASE WHEN jsonb_typeof(output->'ran_as') = 'object'
                        THEN jsonb_build_object('ran_as',
                             (output->'ran_as') || jsonb_build_object('act_id', CAST(:a AS text)))
                        ELSE '{}'::jsonb END
            WHERE id = :i
            """
        ),
        {"a": act_id, "i": step_run_id},
    )


def _record_approval(connection: Connection, run: dict[str, Any], step_act: dict[str, Any],
                     decision_id: str, *, reconstructed: bool = False) -> Optional[str]:
    """The person's answer, as a child of the step it held: an approval, or a rejection.
    Labelled with the step and the run (`Publish · Release notes 2.9`); the verb says which."""
    exists = connection.execute(
        text("SELECT act_id FROM agent_acts WHERE kind = 'approval' AND decision_id = :d "
             "AND parent_act_id = :p"),
        {"d": decision_id, "p": step_act["act_id"]},
    ).scalar()
    if exists:
        return None
    decision = connection.execute(
        text("SELECT status, approved_by, approved_at, executed_by, executed_at, question "
             "FROM decisions WHERE id = :d"),
        {"d": decision_id},
    ).mappings().first()
    if not decision or str(decision["status"]) not in ("approved", "executed", "rejected"):
        return None
    rejected = str(decision["status"]) == "rejected"
    by = decision["approved_by"] or decision["executed_by"] or run["actor"]
    at = decision["approved_at"] or decision["executed_at"]
    if rejected:
        # A NO IS AN ANSWER TOO (agents-home D4): "You rejected …" in Activity. Its status is
        # `error` — the work it held did not go on — and work_verb says Rejected, never Stuck.
        at = connection.execute(text("SELECT updated_at FROM decisions WHERE id = :d"),
                                {"d": decision_id}).scalar() or _now()
    act_id = open_act(
        connection, project_id=run["project_id"], kind="approval", role_key=None,
        how="approved", handed_by_kind="person", handed_by_id=by,
        parent_act_id=step_act["act_id"], accountable=run["actor"], subject_kind="decision",
        subject_id=decision_id,
        label=_step_part(step_act.get("label")) + f" · {_run_part(step_act.get('label'))}",
        started_at=at, workflow_run_id=run["id"], step_id=step_act.get("step_id"),
        decision_id=decision_id, reconstructed=reconstructed,
    )
    close_act(connection, act_id, status="error" if rejected else "complete", ended_at=at)
    return act_id


def _is_wait_act(act: Optional[dict[str, Any]]) -> bool:
    """A step act that is a person's wait at a gate (no agent), not an agent's work."""
    return bool(act and act.get("kind") == "step" and not act.get("role_key")
                and act.get("decision_id"))


def _step_part(label: Any) -> str:
    return str(label or "").split(" · ")[0].strip() or "Approval"


def _run_part(label: Any) -> str:
    parts = str(label or "").split(" · ", 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _close_root(connection: Connection, root: str, run: dict[str, Any]) -> None:
    # A STOPPED RUN IS NOT OVER (agents-home D11): it waits on its operator's Retry or Skip, so
    # its starter stays open — never "You, stuck, Started". Only a run that is really over
    # closes it.
    status = {"completed": "complete", "cancelled": "stale",
              "compensated": "stale"}.get(str(run["status"]))
    if status:
        close_act(connection, root, status=status,
                  ended_at=run.get("completed_at") or run.get("updated_at"))


def on_decision(connection: Connection, decision_id: str) -> list[str]:
    """A decision was answered: sign the approval now, not on the next tick."""
    runs = connection.execute(
        text("SELECT DISTINCT workflow_run_id FROM workflow_step_runs WHERE decision_id = :d"),
        {"d": decision_id},
    ).scalars().all()
    touched: list[str] = []
    for run_id in runs:
        touched.extend(sync_run(connection, str(run_id), wait=False))
    return touched


def reconcile(connection: Connection, project_id: str, *, limit: int = 25) -> int:
    """Close what finished elsewhere: runs with open acts are re-synced, and stages and seats
    whose code task or panel has ended are closed. Cheap and idempotent; the tick calls it.

    NEVER WAITS. The tick is one long transaction and every lock taken here is held until it
    commits — through agent and model steps that run for minutes. A run or panel another
    transaction holds is skipped (that holder signs it), so a second tick, the console's
    `advance`, or a panel's own worker is never queued behind this one, and two ticks that meet
    the same runs in a different order cannot deadlock."""
    touched = 0
    runs = connection.execute(
        text("SELECT DISTINCT workflow_run_id FROM agent_acts WHERE project_id = :p "
             "AND ended_at IS NULL AND workflow_run_id IS NOT NULL LIMIT :n"),
        {"p": project_id, "n": int(limit)},
    ).scalars().all()
    for run_id in runs:
        touched += len(sync_run(connection, str(run_id), wait=False))
    tasks = connection.execute(
        text("SELECT DISTINCT a.code_task_run_id FROM agent_acts a "
             "JOIN code_task_runs t ON t.id = a.code_task_run_id "
             "WHERE a.project_id = :p AND a.kind = 'stage' AND a.ended_at IS NULL "
             "AND t.status NOT IN ('running', 'queued') LIMIT :n"),
        {"p": project_id, "n": int(limit)},
    ).scalars().all()
    for task in tasks:
        touched += close_code_task(connection, str(task))
    meetings = connection.execute(
        text("SELECT DISTINCT a.meeting_run_id FROM agent_acts a "
             "JOIN meeting_runs m ON m.id = a.meeting_run_id "
             "WHERE a.project_id = :p AND a.kind = 'seat' AND a.ended_at IS NULL "
             "AND m.status <> 'running' LIMIT :n"),
        {"p": project_id, "n": int(limit)},
    ).scalars().all()
    for meeting in meetings:
        touched += len(sync_meeting(connection, str(meeting), wait=False))
    touched += prune_chats(connection, project_id)
    return touched


#: Chat turns are kept this long (spec §4.3); every other kind of act is kept for ever.
CHAT_RETENTION_DAYS = 90


def prune_chats(connection: Connection, project_id: str, *,
                days: int = CHAT_RETENTION_DAYS, limit: int = 500) -> int:
    """Drop chat acts past retention that nothing hangs off. Bounded per call."""
    return int(connection.execute(
        text(
            """
            DELETE FROM agent_acts a WHERE a.act_id IN (
                SELECT c.act_id FROM agent_acts c
                WHERE c.project_id = :p AND c.kind = 'chat' AND c.ended_at IS NOT NULL
                  AND c.ended_at < now() - make_interval(days => :d)
                  AND NOT EXISTS (SELECT 1 FROM agent_acts k WHERE k.parent_act_id = c.act_id)
                LIMIT :n)
            """
        ),
        {"p": project_id, "d": int(days), "n": int(limit)},
    ).rowcount or 0)


# ── 4. build stages, from the telemetry chokepoint ───────────────────────────────────────────


def _code_task(connection: Connection, code_task_run_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text("SELECT id, project_id, title, status, finalize_ctx, spec, created_at "
             "FROM code_task_runs WHERE id = :i"),
        {"i": code_task_run_id},
    ).mappings().first()
    return dict(row) if row else None


def _origin_of(ctx: dict[str, Any]) -> dict[str, Any]:
    found = ctx.get("origin") if isinstance(ctx.get("origin"), dict) else {}
    return dict(found)


def _in_project(act: Optional[dict[str, Any]], project_id: str) -> Optional[dict[str, Any]]:
    return act if act and act.get("project_id") == project_id else None


def _step_run_act(connection: Connection, project_id: str,
                  step_run_id: Optional[str]) -> Optional[dict[str, Any]]:
    if not step_run_id:
        return None
    row = connection.execute(
        text("SELECT * FROM agent_acts WHERE subject_kind = 'workflow_step_run' "
             "AND subject_id = :s AND project_id = :p ORDER BY started_at DESC LIMIT 1"),
        {"s": str(step_run_id), "p": project_id},
    ).mappings().first()
    return dict(row) if row else None


def _run_actor(connection: Connection, project_id: str,
               workflow_run_id: Optional[str]) -> Optional[str]:
    if not workflow_run_id:
        return None
    actor = connection.execute(
        text("SELECT actor FROM workflow_runs WHERE id = :r AND project_id = :p"),
        {"r": str(workflow_run_id), "p": project_id},
    ).scalar()
    return str(actor) if actor else None


def _starter(connection: Connection, project_id: str, ctx: dict[str, Any]) -> Optional[str]:
    """The person who started this work, as recorded where it was started. None = nobody was
    recorded; the caller then says BOSS, never a made-up person."""
    found = _origin_of(ctx)
    for value in (found.get("actor"), ctx.get("actor"), ctx.get("requested_by")):
        if value and str(value).strip():
            return str(value).strip()
    return _run_actor(connection, project_id,
                      found.get("workflow_run_id") or ctx.get("workflow_run_id"))


def _code_task_parent(connection: Connection, task: dict[str, Any],
                      _depth: int = 0) -> Optional[dict[str, Any]]:
    """The act a build's stages hang off: the step or chat turn that started it, from what
    started it recorded (finalize_ctx.origin), else from the step that waits on it."""
    project_id = task["project_id"]
    found = connection.execute(
        text("SELECT * FROM agent_acts WHERE kind = 'step' AND code_task_run_id = :c "
             "AND project_id = :p ORDER BY started_at DESC LIMIT 1"),
        {"c": task["id"], "p": project_id},
    ).mappings().first()
    if found:
        return dict(found)
    fctx = _json(task.get("finalize_ctx"), {})
    started_by = _origin_of(fctx)
    act = (_in_project(_act_row(connection, started_by.get("act_id")), project_id)
           or _step_run_act(connection, project_id, started_by.get("step_run_id")))
    if act:
        return act
    workflow_run_id = fctx.get("workflow_run_id") or started_by.get("workflow_run_id")
    if workflow_run_id:
        step_run = connection.execute(
            text(
                """
                SELECT id FROM workflow_step_runs WHERE workflow_run_id = :r AND (
                    output->'await'->>'id' = :c OR output->>'code_task_run_id' = :c
                    OR output->>'run_id' = :c)
                ORDER BY updated_at DESC LIMIT 1
                """
            ),
            {"r": str(workflow_run_id), "c": task["id"]},
        ).scalar()
        act = _step_run_act(connection, project_id, step_run)
        if act:
            return act
    parent_run = fctx.get("parent_run_id") or fctx.get("retry_of")
    if parent_run and _depth < 4:
        earlier = _code_task(connection, str(parent_run))
        if earlier and earlier["project_id"] == project_id:
            return _code_task_parent(connection, earlier, _depth + 1)
    return None


def _origin_pending(fctx: dict[str, Any]) -> bool:
    """The work says it was started by a step or a chat turn whose act is not visible yet: that
    act is still in its own uncommitted transaction. Never answered with a root of its own."""
    started_by = _origin_of(fctx)
    return bool(started_by.get("act_id") or started_by.get("step_run_id")
                or started_by.get("workflow_run_id") or fctx.get("workflow_run_id"))


def _code_task_root(connection: Connection, task: dict[str, Any], *,
                    reconstructed: bool = False) -> str:
    """A build nobody's workflow or chat started (the API, BOSS's own sweeps): its own root,
    the person who started it on it — or BOSS, said as BOSS, when nobody was recorded."""
    existing = connection.execute(
        text("SELECT act_id FROM agent_acts WHERE kind = 'run' AND subject_kind = 'code_task_run' "
             "AND subject_id = :c LIMIT 1"),
        {"c": task["id"]},
    ).scalar()
    if existing:
        return str(existing)
    fctx = _json(task.get("finalize_ctx"), {})
    who = _starter(connection, task["project_id"], fctx)
    parent_run = fctx.get("parent_run_id") or fctx.get("retry_of")
    if who is None and parent_run:
        # A retry is the same person's build again.
        who = connection.execute(
            text("SELECT accountable FROM agent_acts WHERE kind = 'run' "
                 "AND subject_kind = 'code_task_run' AND subject_id = :c LIMIT 1"),
            {"c": str(parent_run)},
        ).scalar()
    return open_act(
        connection, project_id=task["project_id"], kind="run", role_key=None, how="started",
        handed_by_kind="person" if who and who != BOSS else "boss",
        handed_by_id=who if who != BOSS else None, accountable=who or BOSS,
        subject_kind="code_task_run", subject_id=task["id"],
        label=f"Build · {str(task.get('title') or 'untitled')[:80]}",
        started_at=task.get("created_at"), code_task_run_id=task["id"],
        reconstructed=reconstructed,
    )


def _adopt_build(connection: Connection, parent: Optional[dict[str, Any]],
                 code_task_run_id: Optional[str]) -> None:
    """Stages a build signed before the act that started it was visible take that act as their
    parent once it is: the edge was declared by the build's origin, not guessed."""
    if not parent or not parent.get("act_id") or not code_task_run_id:
        return
    connection.execute(
        text(
            """
            UPDATE agent_acts SET parent_act_id = :parent, root_act_id = :root,
                   accountable = :accountable,
                   workflow_run_id = COALESCE(workflow_run_id, :wfr)
            WHERE code_task_run_id = :c AND kind = 'stage' AND parent_act_id IS NULL
              AND project_id = :p AND act_id <> :parent
            """
        ),
        {"parent": parent["act_id"], "root": parent.get("root_act_id") or parent["act_id"],
         "accountable": parent.get("accountable") or BOSS,
         "wfr": parent.get("workflow_run_id"), "c": str(code_task_run_id),
         "p": parent.get("project_id")},
    )


def stage_role(stage: str, parent: Optional[dict[str, Any]]) -> Optional[str]:
    """Who a stage is credited to: the agent the operator put on the build for the stages that
    build, otherwise the stage's own role. STAGE_ROLES is the fallback, never the override."""
    from .llm_telemetry import canonical_role, role_for_stage

    if parent and stage in ENGINEERING_STAGES and parent.get("kind") == "step" \
            and parent.get("how") in ("named", "captain") and parent.get("role_key"):
        return str(parent["role_key"])
    return canonical_role(role_for_stage(stage))


def stage_act(connection: Connection, *, project_id: Optional[str], code_task_run_id: str,
              stage: Optional[str], reconstructed: bool = False,
              seat: Optional[int] = None) -> Optional[dict[str, Any]]:
    """The act a model call in this build stage belongs to, opened on first use.

    Called where every model call is recorded (llm_telemetry), so a stage cannot spend without
    being signed. A stage outside the pipeline's map (quality, research, the model gateway)
    belongs to the build itself: its calls are signed by the act that started the build.

    A build started by a step or a chat turn can spend before that act is committed (the tool
    that started it commits on its own). Its stages are then signed with no parent, the
    starter's person accountable, and adopted by the starting act as soon as it is visible —
    never hung off a root of their own.

    A review panel's seats each sign their own work (`seat`, 1-based): the act keys on
    (stage, seat), so every seat has its own face, tool calls and findings (factory §6.4)."""
    task = _code_task(connection, code_task_run_id)
    seat = int(seat) if seat else None
    if not task:
        return None
    project_id = project_id or task["project_id"]
    stage_key = str(stage or "").strip().lower()
    _lock(connection, f"task:{code_task_run_id}")
    parent = _code_task_parent(connection, task)
    fctx = _json(task.get("finalize_ctx"), {})
    pending = parent is None and _origin_pending(fctx)
    if parent:
        _adopt_build(connection, parent, code_task_run_id)
    if stage_key not in STAGE_PHASES:
        if parent:
            return {"act_id": parent["act_id"], "role_key": parent.get("role_key")}
        if pending:
            return None  # its starter's act will sign it; nothing to hang a guess on
        root = _code_task_root(connection, task, reconstructed=reconstructed)
        return {"act_id": root, "role_key": None}

    # A stage working again ends a budget wait (factory spec §6.4). One conditional write.
    connection.execute(
        text("UPDATE code_task_runs SET finalize_ctx = finalize_ctx - 'budget_wait_since' "
             "WHERE id = :c AND finalize_ctx ? 'budget_wait_since'"),
        {"c": code_task_run_id},
    )
    open_now = [dict(r) for r in connection.execute(
        text("SELECT act_id, step_id, role_key, seat FROM agent_acts WHERE kind = 'stage' "
             "AND code_task_run_id = :c AND ended_at IS NULL"),
        {"c": code_task_run_id},
    ).mappings().all()]
    for act in open_now:
        if act["step_id"] == stage_key and (act.get("seat") or None) == seat:
            return {"act_id": act["act_id"], "role_key": act["role_key"]}
    phase = STAGE_PHASES[stage_key]
    for act in open_now:
        if STAGE_PHASES.get(str(act["step_id"]), -1) != phase:
            close_act(connection, act["act_id"], status="complete")

    if parent:
        parent_id, parent_row = parent["act_id"], parent
    elif pending:
        parent_id, parent_row = None, None
    else:
        parent_id = _code_task_root(connection, task, reconstructed=reconstructed)
        parent_row = _act_row(connection, parent_id)
    role = stage_role(stage_key, parent)
    accountable = ((parent_row or {}).get("accountable")
                   or _starter(connection, project_id, fctx) or BOSS)
    act_id = open_act(
        connection, project_id=project_id, kind="stage", role_key=role, how="next_stage",
        handed_by_kind="boss", parent_act_id=parent_id,
        why=(f"{STAGE_TITLES.get(stage_key, stage_key)} is {role}'s stage of the build"
             if role else None),
        accountable=accountable, subject_kind="code_task_run", subject_id=code_task_run_id,
        label=f"{STAGE_TITLES.get(stage_key, stage_key.title())}"
              f"{f' · seat {seat}' if seat else ''} · "
              f"{str(task.get('title') or 'untitled')[:60]}",
        code_task_run_id=code_task_run_id, step_id=stage_key, reconstructed=reconstructed,
        seat=seat,
        workflow_run_id=((parent_row or {}).get("workflow_run_id") or fctx.get("workflow_run_id")
                         or _origin_of(fctx).get("workflow_run_id")),
    )
    return {"act_id": act_id, "role_key": role}


_TASK_ENDED = {"completed": "complete", "succeeded": "complete", "failed": "error",
               # A stopped build's open stages did not fail and are not stale: they were
               # skipped (factory spec §6.5).
               "cancelled": "skipped", "simulated": "complete"}


def close_code_task(connection: Connection, code_task_run_id: str) -> int:
    """The build ended (or is waiting on a person): its open stages say so, and so does the
    build's own root when nothing else started it."""
    status = connection.execute(text("SELECT status FROM code_task_runs WHERE id = :i"),
                                {"i": code_task_run_id}).scalar()
    mapped = _TASK_ENDED.get(str(status or ""))
    rows = connection.execute(
        text("SELECT act_id FROM agent_acts WHERE code_task_run_id = :c AND kind = 'stage' "
             "AND ended_at IS NULL"),
        {"c": code_task_run_id},
    ).scalars().all()
    for act_id in rows:
        if mapped:
            close_act(connection, act_id, status=mapped)
        elif str(status or "") in ("blocked", "awaiting_approval"):
            _set_status(connection, act_id, "awaiting_input")
    pr = _pull_request_of(connection, code_task_run_id) if mapped else None
    if pr:
        # THE PULL REQUEST IS WHAT THE BUILD MADE, so "PR #842 opened" is credited to the
        # agent who built it: the build's newest engineering stage (implement, repair, solo),
        # else its newest stage. Once per build — a second settle adds nothing (spec §6.5).
        already = connection.execute(
            text("SELECT 1 FROM agent_acts WHERE code_task_run_id = :c AND kind = 'stage' "
                 "AND artifacts @> CAST(:pr AS JSONB) LIMIT 1"),
            {"c": code_task_run_id, "pr": json.dumps([{"kind": "pr"}])},
        ).scalar()
        target = None if already else connection.execute(
            text("SELECT act_id FROM agent_acts WHERE code_task_run_id = :c AND kind = 'stage' "
                 "ORDER BY (step_id = ANY(:eng)) DESC, started_at DESC LIMIT 1"),
            {"c": code_task_run_id, "eng": sorted(ENGINEERING_STAGES)},
        ).scalar()
        if target:
            connection.execute(
                text("UPDATE agent_acts SET artifacts = artifacts || CAST(:a AS JSONB) "
                     "WHERE act_id = :i"),
                {"a": json.dumps([pr]), "i": target},
            )
    if mapped:
        for root in connection.execute(
            text("SELECT act_id FROM agent_acts WHERE kind = 'run' "
                 "AND subject_kind = 'code_task_run' AND subject_id = :c AND ended_at IS NULL"),
            {"c": code_task_run_id},
        ).scalars().all():
            close_act(connection, root, status=mapped)
    return len(rows) if mapped else 0


def _pull_request_of(connection: Connection,
                     code_task_run_id: str) -> Optional[dict[str, Any]]:
    """The build's pull request as an artifact, from code_task_runs — or None when it opened
    none. `ref` is `#n` (the number the run recorded, else the one in the URL)."""
    row = connection.execute(
        text("SELECT pull_request_url, pull_request_number FROM code_task_runs WHERE id = :i"),
        {"i": code_task_run_id},
    ).mappings().first()
    if not row or not (row["pull_request_url"] or row["pull_request_number"]):
        return None
    number = row["pull_request_number"]
    if not number and row["pull_request_url"]:
        found = re.search(r"/pull/(\d+)", str(row["pull_request_url"]))
        number = int(found.group(1)) if found else None
    item: dict[str, Any] = {"kind": "pr", "ref": f"#{number}" if number else
                            str(row["pull_request_url"])}
    if row["pull_request_url"]:
        item["url"] = str(row["pull_request_url"])
    return item


def settle_code_task(engine: Any, code_task_run_id: str) -> None:
    """For a build's own `finally`: close its stages in their own short transaction. Never
    raises — a build's outcome is already decided by the time this runs."""
    try:
        from ..db import begin

        with begin(engine) as c:
            close_code_task(c, code_task_run_id)
    except Exception as error:  # noqa: BLE001
        log.warning("agent acts: could not close the stages of %s: %s", code_task_run_id, error)
    try:
        from ..db import begin
        from .run_decisions import withdraw_superseded_pr_questions

        with begin(engine) as c:
            withdraw_superseded_pr_questions(c, code_task_run_id)
    except Exception as error:  # noqa: BLE001
        log.warning("could not supersede earlier PR decisions of %s: %s", code_task_run_id, error)
    # ITS RUN SCORECARD, with the final numbers (docs/product/RUN_SCORECARDS.md). Every build
    # runner's `finally` comes through here, so no terminal path is missed. Never raises.
    from .scorecards import settle as _settle_scorecard

    _settle_scorecard(engine, code_task_run_id)


def _agent_name(connection: Connection, project_id: str, role_key: str) -> str:
    row = connection.execute(
        text("SELECT display_name, spec FROM roles WHERE project_id = :p AND role_key = :r"),
        {"p": project_id, "r": role_key},
    ).mappings().first()
    if not row:
        return role_key.replace("_", " ").title()
    persona = persona_of({"role_key": role_key, "spec": _json(row["spec"], {}) or {}})
    return str(persona.get("callsign") or row["display_name"] or role_key)


def trailers(connection: Connection, code_task_run_id: str) -> dict[str, Any]:
    """Who made a build, from its acts: the agents in the order they first acted, the act that
    started the build, its run, and the person accountable. The human stays the git author."""
    task = _code_task(connection, code_task_run_id)
    if not task:
        return {}
    parent = _code_task_parent(connection, task)
    rows = connection.execute(
        text("SELECT role_key, agent_id, fingerprint, identity_version, step_id, accountable "
             "FROM agent_acts WHERE code_task_run_id = :c AND kind = 'stage' "
             "AND role_key IS NOT NULL ORDER BY started_at"),
        {"c": code_task_run_id},
    ).mappings().all()
    agents: list[dict[str, Any]] = []
    seen: set[str] = set()
    lead = None
    for row in rows:
        if row["role_key"] not in seen:
            seen.add(row["role_key"])
            agents.append(dict(row))
        if row["step_id"] in ENGINEERING_STAGES and lead is None:
            lead = dict(row)
    if parent and parent.get("role_key") and parent["role_key"] not in seen:
        agents.insert(0, parent)
    lead = lead or (agents[0] if agents else None)
    return {
        "agents": [{"role_key": a["role_key"], "agent_id": a.get("agent_id"),
                    "name": _agent_name(connection, task["project_id"], a["role_key"])}
                   for a in agents],
        "lead": lead,
        "act_id": (parent or {}).get("act_id"),
        "workflow_run_id": (parent or {}).get("workflow_run_id"),
        "accountable": ((parent or {}).get("accountable")
                        or (rows[0]["accountable"] if rows else None)),
    }


def pr_trailers(connection: Connection, code_task_run_id: str) -> str:
    """The PR body's footer and git trailers (spec §5.3). "" when nothing is signed.
    Never raises."""
    try:
        made = trailers(connection, code_task_run_id)
    except Exception as error:  # noqa: BLE001 — a footer is never worth a failed PR
        log.warning("agent acts: no trailers for %s: %s", code_task_run_id, error)
        made = None
    identity = identity_trailers(connection, code_task_run_id)
    if not made or not made.get("agents"):
        return "\n\n" + identity if identity else ""
    count = len(made["agents"])
    accountable = made.get("accountable") or BOSS
    lines = ["", "", f"Made by {count} agent{'s' if count != 1 else ''} · "
                     f"accountable: {'BOSS' if accountable == BOSS else accountable}", ""]
    for agent in made["agents"]:
        lines.append(f"BOSS-Agent: {agent['name']} ({agent['role_key']}"
                     f"{', ' + agent['agent_id'] if agent.get('agent_id') else ''})")
    lead = made.get("lead") or {}
    if lead.get("fingerprint"):
        lines.append(f"BOSS-Definition: v{lead.get('identity_version') or 1} "
                     f"{str(lead['fingerprint'])[:8]}")
    if made.get("act_id"):
        lines.append(f"BOSS-Act: {made['act_id']}")
    if made.get("workflow_run_id"):
        base = os.environ.get("BOSS_CONSOLE_URL", "").rstrip("/")
        lines.append(f"BOSS-Run: {base}/workflows?wfr={made['workflow_run_id']}" if base
                     else f"BOSS-Run: {made['workflow_run_id']}")
    if identity:
        lines.append(identity)
    return "\n".join(lines)


def identity_trailers(connection: Connection, code_task_run_id: str) -> str:
    """IDENTITY.md §7.9: who a build's commits and PR were made FOR, and under which grant —
    `On-behalf-of: Alice <alice@acme.com>` and `Boss-Lineage: dlg_…/run_…` — from the authority
    the build was STARTED under (runs.original_build_authority), plus a link to the grant's
    lineage when the console's URL is known. "" for BOSS's own work. Never raises."""
    try:
        from ..identity import github_app, runs

        authority = runs.original_build_authority(connection, code_task_run_id)
        if authority is None or authority.system or not authority.principal_id.startswith("usr_"):
            return ""
        person = connection.execute(text(
            "SELECT p.display_name, u.email FROM identity_principals p LEFT JOIN identity_users u "
            "ON u.principal_id = p.id WHERE p.id = :p"), {"p": authority.principal_id}).mappings().first()
        if person is None:
            return ""
        out = github_app.commit_trailers(person_name=person["display_name"] or authority.principal_id,
                                         person_email=person["email"], grant_id=authority.grant_id,
                                         run_id=code_task_run_id)
        base = os.environ.get("BOSS_CONSOLE_URL", "").rstrip("/")
        if base and authority.grant_id:
            out += f"\nBOSS-Lineage-Link: {base}/identity/agents/delegations/{authority.grant_id}"
        return out
    except Exception as error:  # noqa: BLE001 — a footer is never worth a failed PR
        log.warning("agent acts: no identity trailers for %s: %s", code_task_run_id, type(error).__name__)
        return ""


_SIGNED: dict[str, str] = {}


def _current_code_run() -> Optional[str]:
    """The code-task run this thread is working for (owned_process's owner scope), if any."""
    from . import owned_process

    owner = owned_process.current_owner()
    if owner is None:
        return None
    if owner.kind == "code_run":
        return owner.id
    return next((i for i in reversed(owner.ids()) if str(i).startswith("crun_")), None)


def sign_commit_message(message: str, run_id: Optional[str] = None) -> str:
    """EVERY COMMIT BOSS MAKES FOR A PERSON'S BUILD SAYS WHO IT WAS FOR (IDENTITY.md §7.9).

    The PR body carried `On-behalf-of:` and `Boss-Lineage:` (pr_trailers), but the commits — the
    checkpoint after each stage, the proposal's file updates — did not: whoever reads `git log`
    (or a branch protection rule that checks trailers) could not tell whose authority wrote them.
    Every repository host's file commit (github_adapter, bitbucket_adapter, mcp_repo_host) passes
    its message through here; inside a code-task run (the owner scope) the run's identity trailers
    are appended once. Outside a run, for BOSS's own work, or when nothing is known, the message is
    unchanged. Never raises."""
    try:
        rid = run_id or _current_code_run()
        if not rid or "On-behalf-of:" in (message or ""):
            return message
        trailers = _SIGNED.get(rid)
        if trailers is None:
            from ..db import begin

            with begin() as connection:
                trailers = identity_trailers(connection, rid)
            if trailers:
                if len(_SIGNED) > 512:
                    _SIGNED.clear()
                _SIGNED[rid] = trailers
        return f"{message.rstrip()}\n\n{trailers}" if trailers else message
    except Exception as error:  # noqa: BLE001 — a trailer is never worth a failed commit
        log.warning("agent acts: commit not signed: %s", type(error).__name__)
        return message


# ── 5. panel seats ───────────────────────────────────────────────────────────────────────────


_PENDING = object()


def _meeting_parent(connection: Connection, meeting: dict[str, Any],
                    fctx: dict[str, Any]) -> Any:
    """The act that convened this panel: the workflow step that waits on it, or what its origin
    names (a step, a chat turn). `_PENDING` when the origin names one that is not visible yet —
    the tool that convened the panel commits before the step or turn that called it — so the
    seats wait for it rather than hang off a root of their own. None: a standalone panel."""
    project_id = meeting["project_id"]
    found = connection.execute(
        text("SELECT * FROM agent_acts WHERE kind = 'step' AND meeting_run_id = :m "
             "AND project_id = :p ORDER BY started_at DESC LIMIT 1"),
        {"m": meeting["id"], "p": project_id},
    ).mappings().first()
    if found:
        return dict(found)
    started_by = _origin_of(fctx)
    named = _in_project(_act_row(connection, started_by.get("act_id") or fctx.get("act_id")),
                        project_id)
    if named:
        return named
    step_act = _step_run_act(connection, project_id, started_by.get("step_run_id"))
    if step_act:
        _fill_links(connection, step_act["act_id"], meeting_run_id=meeting["id"])
        return step_act
    if started_by.get("act_id") or fctx.get("act_id"):
        return _PENDING
    if started_by.get("step_run_id") and connection.execute(
            text("SELECT 1 FROM workflow_step_runs s JOIN workflow_runs r "
                 "ON r.id = s.workflow_run_id WHERE s.id = :s AND r.project_id = :p"),
            {"s": started_by["step_run_id"], "p": project_id}).scalar():
        return _PENDING
    return None


def _meeting_root(connection: Connection, meeting: dict[str, Any], fctx: dict[str, Any], *,
                  reconstructed: bool) -> dict[str, Any]:
    """A panel nothing else convened (the Meetings screen, the API, Plane): its own root, the
    person who convened it on it — BOSS, said as BOSS, when nobody was recorded."""
    from .meeting_service import meeting_title

    existing = _meeting_root_id(connection, meeting["id"])
    if not existing:
        who = _starter(connection, meeting["project_id"], fctx)
        existing = open_act(
            connection, project_id=meeting["project_id"], kind="run", role_key=None,
            how="started", handed_by_kind="person" if who and who != BOSS else "boss",
            handed_by_id=who if who != BOSS else None, accountable=who or BOSS,
            subject_kind="meeting_run", subject_id=meeting["id"],
            label=f"Panel · {meeting_title(meeting['title'], meeting['directive'])[:80]}",
            started_at=meeting["created_at"], meeting_run_id=meeting["id"],
            reconstructed=reconstructed)
    return _act_row(connection, existing) or {}


def _meeting_root_id(connection: Connection, meeting_run_id: str) -> Optional[str]:
    found = connection.execute(
        text("SELECT act_id FROM agent_acts WHERE kind = 'run' "
             "AND subject_kind = 'meeting_run' AND subject_id = :m LIMIT 1"),
        {"m": meeting_run_id},
    ).scalar()
    return str(found) if found else None


def sync_meeting(connection: Connection, meeting_run_id: str, *, wait: bool = True,
                 reconstructed: bool = False) -> list[str]:
    """One seat act per seat on a panel, under the step (or chat turn) that convened it.

    A panel a workflow step convened is signed under that step, and only once the step's act
    exists: until then nothing is written, and the run's own sync signs the seats when the step
    settles. A seat's parent is decided once, from the panel's real origin, never guessed."""
    meeting = connection.execute(
        text("SELECT id, project_id, status, role_keys, finalize_ctx, title, directive, "
             "minutes_artifact_id, created_at, updated_at FROM meeting_runs WHERE id = :i"),
        {"i": meeting_run_id},
    ).mappings().first()
    if not meeting:
        return []
    from .meeting_service import meeting_title

    if not _lock(connection, f"meeting:{meeting_run_id}", wait=wait):
        return []
    meeting = dict(meeting)
    fctx = _json(meeting["finalize_ctx"], {})
    fctx = fctx if isinstance(fctx, dict) else {}
    status = {"completed": "complete", "simulated": "complete",
              "failed": "error", "cancelled": "stale"}.get(str(meeting["status"]))
    touched: list[str] = []
    existing = {str(r["role_key"]): dict(r) for r in connection.execute(
        text("SELECT act_id, role_key, ended_at FROM agent_acts WHERE kind = 'seat' "
             "AND meeting_run_id = :m"),
        {"m": meeting_run_id},
    ).mappings().all()}
    parent = None
    if not existing:
        parent = _meeting_parent(connection, meeting, fctx)
        if parent is _PENDING:
            return []
        if parent is None:
            parent = _meeting_root(connection, meeting, fctx, reconstructed=reconstructed)
    seats: list[str] = []
    for key in _json(meeting["role_keys"], []) or []:
        role = resolve_role(str(key))
        if role and role not in seats:
            seats.append(role)
    title = meeting_title(meeting["title"], meeting["directive"])[:80]
    for role in seats:
        act = existing.get(role)
        if act is None:
            if parent is None:
                # A seat added after the others: it sits where they sit.
                first = next(iter(existing.values()))
                row = _act_row(connection, first["act_id"]) or {}
                parent = _act_row(connection, row.get("parent_act_id")) or {}
            act_id = open_act(
                connection, project_id=meeting["project_id"], kind="seat", role_key=role,
                how="seated", handed_by_kind="boss", parent_act_id=parent.get("act_id"),
                accountable=parent.get("accountable") or BOSS, subject_kind="meeting_run",
                subject_id=meeting_run_id, label=f"Panel · {title}",
                started_at=meeting["created_at"], meeting_run_id=meeting_run_id,
                workflow_run_id=parent.get("workflow_run_id"), reconstructed=reconstructed)
            touched.append(act_id)
            act = {"act_id": act_id, "ended_at": None}
        if status and act["ended_at"] is None:
            close_act(connection, act["act_id"], status=status, ended_at=meeting["updated_at"],
                      artifacts=([{"kind": "minutes", "ref": meeting["minutes_artifact_id"]}]
                                 if meeting["minutes_artifact_id"] else []))
            touched.append(act["act_id"])
    root = _meeting_root_id(connection, meeting_run_id)
    if root and status:
        close_act(connection, root, status=status, ended_at=meeting["updated_at"])
    return touched


# ── 6. dock chat turns ───────────────────────────────────────────────────────────────────────


def open_chat_act(connection: Connection, *, project_id: str, actor: str, role_key: str,
                  thread_id: Optional[str], message: str,
                  context: Optional[dict[str, Any]] = None) -> Optional[str]:
    """A person asked an agent something. Parented to what the dock was opened about."""
    if not role_key:
        return None
    context = context or {}
    parent = _act_row(connection, context.get("act_id")) if context.get("act_id") else None
    if parent is None and context.get("workflow_run_id") and context.get("step_id"):
        found = connection.execute(
            text("SELECT * FROM agent_acts WHERE workflow_run_id = :r AND step_id = :s "
                 "AND kind = 'step' ORDER BY started_at DESC LIMIT 1"),
            {"r": str(context["workflow_run_id"]), "s": str(context["step_id"])},
        ).mappings().first()
        parent = dict(found) if found else None
    if parent is not None and parent.get("project_id") != project_id:
        parent = None
    said = " ".join(str(message or "").split())
    return open_act(
        connection, project_id=project_id, kind="chat", role_key=role_key,
        how="asked_by_person", handed_by_kind="person", handed_by_id=actor,
        parent_act_id=parent["act_id"] if parent else None, accountable=actor,
        subject_kind="agent_thread", subject_id=thread_id,
        label=f"Chat · {said[:60] or 'a question'}", thread_id=thread_id,
        workflow_run_id=(parent or {}).get("workflow_run_id"),
    )


def cited_acts(connection: Connection, project_id: str, reply: str) -> list[str]:
    """The [act:…] references in a reply that are real acts of this project, in order."""
    wanted = list(dict.fromkeys(_ACT_REF.findall(reply or "")))
    if not wanted:
        return []
    real = set(connection.execute(
        text("SELECT act_id FROM agent_acts WHERE project_id = :p AND act_id = ANY(:a)"),
        {"p": project_id, "a": wanted},
    ).scalars().all())
    return [a for a in wanted if a in real]


# ── 8. an agent asking an agent ──────────────────────────────────────────────────────────────


def record_handoff(connection: Connection, *, project_id: str, envelope_id: str,
                   sender_id: str, recipient: dict[str, Any], actor: str,
                   lineage: Optional[dict[str, Any]] = None,
                   message_kind: str = "") -> Optional[str]:
    """An A2A envelope sent BY an agent TO an agent: the one path that writes asked_by_agent."""
    target = None
    recipient = recipient if isinstance(recipient, dict) else {}
    if str(recipient.get("kind") or "") in ("agent", "role") and recipient.get("id"):
        target = resolve_role(str(recipient["id"]))  # the envelope's own {kind, id} shape
    for field in ("role_key", "agent", "role", "agent_role"):
        if target is None and recipient.get(field):
            target = resolve_role(str(recipient[field]))
    sender = resolve_role(sender_id)
    if not target or not sender:
        return None
    # THE PARENT IS WHAT THE ENVELOPE SAYS, OR NOTHING. An agent with two runs and a panel
    # seat open is in three pieces of work; "its newest open act" is a guess that hangs this
    # ask — its cost, its accountable person — on whichever run happened to start last. The
    # envelope's own lineage names the act it was sent from, or the run it belongs to; it is
    # only believed for this project.
    lineage = lineage if isinstance(lineage, dict) else {}
    parent = _in_project(_act_row(connection, str(lineage["act_id"])), project_id) \
        if lineage.get("act_id") else None
    if parent is None and lineage.get("workflow_run_id"):
        root = connection.execute(
            text("SELECT * FROM agent_acts WHERE kind = 'run' AND workflow_run_id = :r "
                 "AND project_id = :p ORDER BY started_at LIMIT 1"),
            {"r": str(lineage["workflow_run_id"]), "p": project_id},
        ).mappings().first()
        parent = dict(root) if root else None
    act_id = open_act(
        connection, project_id=project_id, kind="handoff", role_key=target,
        how="asked_by_agent", handed_by_kind="agent", handed_by_id=sender,
        parent_act_id=parent["act_id"] if parent else None,
        accountable=(parent or {}).get("accountable") or actor,
        subject_kind="a2a_envelope", subject_id=envelope_id,
        label=f"Asked by {sender} · {message_kind or 'message'}", envelope_id=envelope_id,
        workflow_run_id=(parent or {}).get("workflow_run_id"),
    )
    return act_id


# ── the work verb (agents-home spec §4.5): one word for what an act is doing ─────────────────

#: Build stages → the verb. Data the pipeline already has (STAGE_PHASES), never a label.
STAGE_VERBS: dict[str, str] = {
    "spec": "Planning", "plan": "Planning", "review_plan": "Planning", "standards": "Planning",
    "design": "Designing",
    "implement": "Coding", "repair": "Coding", "solo": "Coding", "guest": "Coding",
    "review_code": "Reviewing", "consistency_check": "Reviewing", "security_check": "Reviewing",
    "write_tests": "Testing", "check_tests": "Testing", "run_all_tests": "Testing",
}
#: A capability's `family` (workflow_capabilities.Capability.family) → the verb.
FAMILY_VERBS: dict[str, str] = {
    "deploy": "Deploying", "publish": "Publishing", "monitor": "Monitoring",
    "research": "Researching",
}
#: Every verb work_verb can say. The web renders these words and maps none of its own.
VERBS = frozenset({"Needs you", "Stuck", "Approved", "Rejected", "In a panel", "Talking",
                   "Waiting", "Blocked", "Working", *STAGE_VERBS.values(),
                   *FAMILY_VERBS.values()})


def capability_family(capability: Optional[str]) -> str:
    """The registry's own `family` for a capability key; "" when it has none or is unknown."""
    if not capability:
        return ""
    try:
        from .workflow_capabilities import CAPABILITIES

        found = CAPABILITIES.get(str(capability))
    except Exception:  # noqa: BLE001 — a verb is never worth failing a read for
        return ""
    return str(getattr(found, "family", "") or "") if found is not None else ""


def work_verb(act: dict[str, Any], *, capability: Optional[str] = None,
              waiting: bool = False, run_status: Optional[str] = None) -> str:
    """THE ONE MAP from an act to the word every agent surface says (spec §4.5).

    Live's lanes, Activity's river, presence lines and the workforce card all read this, so
    no screen words work its own way. It reads the act's status, kind, stage and its step's
    capability family — never its label: a label is a name somebody typed, and "deploy" in a
    build's title does not make the build a deploy.

    `capability` is the step's capability key (callers that hold the run spec pass it; the
    readers below look it up in one query). `waiting` is a step waiting on something that is
    not a person (a timer, a sub-run, a build). `run_status` lets a run's own act say Blocked.
    """
    status = str(act.get("status") or "")
    kind = str(act.get("kind") or "")
    if kind == "approval":
        # A person's answer: `error` is a rejection (the work it held did not go on).
        return "Rejected" if status in ("error", "rejected") else "Approved"
    if status == "awaiting_input":
        return "Needs you"
    if kind == "run":
        # The starter is never Stuck (agents-home D11): a stopped run waits on its operator.
        return "Blocked" if str(run_status or "") == "blocked" else "Started"
    if status in ("error", "stale"):
        return "Stuck"
    if kind == "seat":
        return "In a panel"
    if kind == "chat":
        return "Talking"
    if kind == "stage":
        return STAGE_VERBS.get(str(act.get("step_id") or "").strip().lower(), "Working")
    if kind == "step":
        if waiting and status in OPEN_STATUSES:
            return "Waiting"
        return FAMILY_VERBS.get(capability_family(capability), "Working")
    return "Working"


def _step_capabilities(connection: Connection,
                       rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    """{act_id: capability key} for the step acts among `rows`, from their run's spec — one
    query for all of them, reading only the steps' ids and capabilities."""
    wanted: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("kind") == "step" and row.get("workflow_run_id") and row.get("step_id"):
            wanted.setdefault(str(row["workflow_run_id"]), []).append(row)
    if not wanted:
        return {}
    found: dict[tuple[str, str], str] = {}
    for r in connection.execute(
        text(
            """
            SELECT r.id AS run_id, s->>'id' AS step_id,
                   COALESCE(s->'binding'->>'capability', s->>'capability') AS capability
            FROM workflow_runs r
            JOIN workflow_versions v ON v.id = r.workflow_version_id
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(v.spec->'steps') = 'array' THEN v.spec->'steps'
                     ELSE '[]'::jsonb END) s
            WHERE r.id = ANY(:ids)
            """
        ),
        {"ids": list(wanted)},
    ).mappings().all():
        if r["capability"]:
            found[(str(r["run_id"]), str(r["step_id"]))] = str(r["capability"])
    return {str(a["act_id"]): found[(run_id, str(a["step_id"]))]
            for run_id, acts in wanted.items() for a in acts
            if (run_id, str(a["step_id"])) in found}


def verbs_for(connection: Connection, rows: list[dict[str, Any]]) -> dict[str, str]:
    """{act_id: work_verb} for a batch of act rows."""
    caps = _step_capabilities(connection, rows)
    return {str(r["act_id"]): work_verb(r, capability=caps.get(str(r["act_id"])))
            for r in rows}


# ── reading the ledger ───────────────────────────────────────────────────────────────────────


def _live_cost(connection: Connection, act_ids: list[str]) -> dict[str, tuple[int, int, float]]:
    """Model spend of OPEN acts, read live: an act's own columns are filled when it closes."""
    if not act_ids:
        return {}
    rows = connection.execute(
        text("SELECT act_id, sum(prompt_tokens) pt, sum(completion_tokens) ct, "
             "sum(cost_usd_micros) micros FROM llm_calls WHERE act_id = ANY(:a) GROUP BY act_id"),
        {"a": act_ids},
    ).mappings().all()
    return {r["act_id"]: (int(r["pt"] or 0), int(r["ct"] or 0), int(r["micros"] or 0) / 1e6)
            for r in rows}


def serialize(act: dict[str, Any], live: Optional[tuple[int, int, float]] = None, *,
              viewer: Optional[str] = None) -> dict[str, Any]:
    """The wire shape (snake_case; lib/agent-world.ts maps it to the web's Act).

    `viewer` is the person reading: an edge a person made reads "you" only for that person."""
    scope = _json(act.get("scope"), {})
    tokens_in, tokens_out = int(act.get("tokens_in") or 0), int(act.get("tokens_out") or 0)
    cost = float(act.get("cost_usd") or 0)
    if live and act.get("ended_at") is None:
        tokens_in, tokens_out, cost = tokens_in + live[0], tokens_out + live[1], cost + live[2]
    return {
        "act_id": act["act_id"],
        "role_key": act.get("role_key"),
        "agent_id": act.get("agent_id"),
        "kind": act["kind"],
        "status": act["status"],
        "parent_act_id": act.get("parent_act_id"),
        "root_act_id": act["root_act_id"],
        "handed_by": {"kind": act["handed_by_kind"],
                      **({"id": act["handed_by_id"]} if act.get("handed_by_id") else {})},
        "how": act["how"],
        "edge_label": _label_for(act, viewer),
        "why": act.get("why"),
        "considered": _json(act.get("considered"), []),
        "scope": {"skills": scope.get("skills") or [], "mcp": scope.get("mcp") or []},
        "scope_ok": act.get("scope_ok"),
        "accountable": act["accountable"],
        "label": act["label"],
        # What it is doing, in the one word every surface says (work_verb). The readers that
        # hold a connection refine it with the step's capability (verbs_for).
        "verb": work_verb(act),
        "subject": {"kind": act.get("subject_kind"), "id": act.get("subject_id")},
        "links": {k: act[k] for k in LINKS if act.get(k)},
        "artifacts": _json(act.get("artifacts"), []),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": round(cost, 6),
        "reconstructed": bool(act.get("reconstructed")),
        "seat": act.get("seat"),
        "started_at": _iso(act.get("started_at")),
        "ended_at": _iso(act.get("ended_at")),
        "version": act.get("identity_version"),
        "fingerprint": act.get("fingerprint"),
    }


def _serialized(connection: Connection, rows: list[dict[str, Any]], *,
                viewer: Optional[str] = None) -> list[dict[str, Any]]:
    live = _live_cost(connection, [r["act_id"] for r in rows if r.get("ended_at") is None])
    verbs = verbs_for(connection, rows)
    out = []
    for r in rows:
        wire = serialize(r, live.get(r["act_id"]), viewer=viewer)
        wire["verb"] = verbs.get(str(r["act_id"]), wire["verb"])
        out.append(wire)
    return out


def get_act(connection: Connection, project_id: str, act_id: str, *,
            viewer: Optional[str] = None) -> Optional[dict[str, Any]]:
    """One act, with what an Act card shows beside it: its last turns, receipt, and children."""
    row = connection.execute(
        text("SELECT * FROM agent_acts WHERE act_id = :a AND project_id = :p"),
        {"a": act_id, "p": project_id},
    ).mappings().first()
    if not row:
        return None
    act = _serialized(connection, [dict(row)], viewer=viewer)[0]
    turns = connection.execute(
        text("SELECT direction, content, created_at FROM agent_turns WHERE act_id = :a "
             "AND direction = 'completion' ORDER BY created_at DESC LIMIT 3"),
        {"a": act_id},
    ).mappings().all()
    if not turns and row["kind"] == "chat":
        turns = connection.execute(
            text("SELECT 'completion' AS direction, content, created_at FROM agent_messages "
                 "WHERE act_id = :a AND role = 'assistant' ORDER BY created_at DESC LIMIT 3"),
            {"a": act_id},
        ).mappings().all()
    children = connection.execute(
        text("SELECT kind, role_key, status FROM agent_acts WHERE parent_act_id = :a "
             "ORDER BY started_at"),
        {"a": act_id},
    ).mappings().all()
    by_kind: dict[str, int] = {}
    for child in children:
        by_kind[child["kind"]] = by_kind.get(child["kind"], 0) + 1
    parent = _act_row(connection, row["parent_act_id"]) if row["parent_act_id"] else None
    act.update({
        "from": _from_node(row, parent),
        "turns_excerpt": [{"text": str(t["content"] or "")[:400], "at": _iso(t["created_at"])}
                          for t in turns],
        "receipt": ({"id": row["receipt_id"], "href": f"/v1/receipts/{row['receipt_id']}"}
                    if row.get("receipt_id") else None),
        "children": {"count": len(children), "by_kind": by_kind,
                     "roles": list(dict.fromkeys(c["role_key"] for c in children
                                                 if c["role_key"]))},
    })
    return act


def _node(act: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Who an act stands for in a graph: its agent, else the person at it, else BOSS."""
    if act is None:
        return {"id": "boss", "kind": "boss"}
    if act.get("role_key"):
        return {"id": f"agent:{act['role_key']}", "kind": "agent", "role_key": act["role_key"]}
    person = act.get("handed_by_id") if act.get("handed_by_kind") == "person" else None
    person = person or act.get("accountable")
    return {"id": f"person:{person}", "kind": "person", "person": person}


def _from_node(act: dict[str, Any], parent: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Who handed this work over: the agent or person the act names, else whoever is at the
    work it came after (the previous step's agent, the person who started the run), else BOSS."""
    if act.get("handed_by_kind") == "agent" and act.get("handed_by_id"):
        return {"kind": "agent", "id": act["handed_by_id"], "role_key": act["handed_by_id"]}
    if act.get("handed_by_kind") == "person":
        return {"kind": "person", "id": act.get("handed_by_id") or act.get("accountable")}
    if parent is not None:
        node = _node(parent)
        return {"kind": node["kind"], "id": node.get("role_key") or node.get("person") or "boss",
                **({"role_key": node["role_key"]} if node.get("role_key") else {})}
    return {"kind": "boss", "id": "boss"}


def acts_for_code_task(connection: Connection, project_id: str, code_task_run_id: str, *,
                       viewer: Optional[str] = None) -> list[dict[str, Any]]:
    """A build's signed work, oldest first: its root (when nothing else started it), every
    stage and every review seat — for ANY build, inline ones included (factory spec §6.4).
    Faces used to exist only for builds a workflow started."""
    rows = [dict(r) for r in connection.execute(
        text("SELECT * FROM agent_acts WHERE project_id = :p AND code_task_run_id = :c "
             "AND kind IN ('run', 'stage') ORDER BY started_at, act_id"),
        {"p": project_id, "c": code_task_run_id},
    ).mappings().all()]
    return _serialized(connection, rows, viewer=viewer)


def acts_for_agent(connection: Connection, project_id: str, role_key: str, *,
                   kind: Optional[str] = None, days: int = 30, cursor: Optional[str] = None,
                   limit: int = 50, viewer: Optional[str] = None) -> dict[str, Any]:
    """An agent's signed work, newest first, each with who handed it over and who it handed to."""
    limit = max(1, min(int(limit), 200))
    params: dict[str, Any] = {"p": project_id, "r": role_key, "d": int(days), "n": limit + 1}
    where = ["a.project_id = :p", "a.role_key = :r",
             "a.started_at > now() - make_interval(days => :d)"]
    if kind:
        if kind not in KINDS:
            raise ValueError(f"unknown act kind {kind!r}")
        where.append("a.kind = :k")
        params["k"] = kind
    if cursor:
        at, _, after = cursor.partition("|")
        where.append("(a.started_at, a.act_id) < (CAST(:ca AS timestamptz), :cid)")
        params.update(ca=at, cid=after)
    rows = [dict(r) for r in connection.execute(
        text(f"SELECT a.* FROM agent_acts a WHERE {' AND '.join(where)} "
             "ORDER BY a.started_at DESC, a.act_id DESC LIMIT :n"),
        params,
    ).mappings().all()]
    more = len(rows) > limit
    rows = rows[:limit]
    parents = {r["act_id"]: dict(r) for r in connection.execute(
        text("SELECT * FROM agent_acts WHERE act_id = ANY(:a)"),
        {"a": [r["parent_act_id"] for r in rows if r.get("parent_act_id")]},
    ).mappings().all()} if rows else {}
    children: dict[str, list[dict[str, Any]]] = {}
    for child in connection.execute(
        text("SELECT parent_act_id, role_key, handed_by_id, handed_by_kind, accountable, kind "
             "FROM agent_acts WHERE parent_act_id = ANY(:a) ORDER BY started_at"),
        {"a": [r["act_id"] for r in rows]},
    ).mappings().all() if rows else []:
        children.setdefault(child["parent_act_id"], []).append(dict(child))
    items = []
    for act, wire in zip(rows, _serialized(connection, rows, viewer=viewer)):
        wire["from"] = _from_node(act, parents.get(act.get("parent_act_id")))
        wire["to"] = [{k: v for k, v in _node(c).items() if k != "person" or v}
                      for c in children.get(act["act_id"], [])]
        items.append(wire)
    nxt = f"{_iso(rows[-1]['started_at'])}|{rows[-1]['act_id']}" if more and rows else None
    return {"role_key": role_key, "items": items, "next": nxt}


#: The most acts the observed lineage reads. It is a picture of who hands this agent work, so
#: the newest few thousand acts say it; reading a busy project's whole year does not.
LINEAGE_ROW_CAP = 2000


def lineage(connection: Connection, project_id: str, role_key: str, *, days: int = 30,
            mode: str = "observed", max_nodes: int = 60,
            viewer: Optional[str] = None) -> dict[str, Any]:
    """Who hands this agent work and who it hands work to, from the acts (observed), or the
    reporting line and birth it declares (declared).

    Read as the agent's OWN acts (each with its parent) plus the CHILDREN of its own acts —
    two index reads that cost what this agent did, not what the whole project did."""
    centre = {"id": f"agent:{role_key}", "kind": "agent", "role_key": role_key}
    if mode == "declared":
        return _declared_lineage(connection, project_id, role_key, centre)
    if mode != "observed":
        raise ValueError("mode is observed or declared")
    rows = connection.execute(
        text(
            """
            WITH mine AS (
                SELECT * FROM agent_acts
                WHERE project_id = :p AND role_key = :r
                  AND started_at > now() - make_interval(days => :d)
                ORDER BY started_at DESC LIMIT :n
            ), handed AS (
                SELECT c.* FROM agent_acts c
                WHERE c.parent_act_id IN (SELECT act_id FROM mine)
                  AND c.project_id = :p AND c.role_key IS DISTINCT FROM :r
                  AND c.started_at > now() - make_interval(days => :d)
                ORDER BY c.started_at DESC LIMIT :n
            ), edges AS (
                SELECT * FROM mine UNION ALL SELECT * FROM handed
            )
            SELECT c.act_id, c.role_key, c.how, c.handed_by_kind, c.handed_by_id, c.accountable,
                   c.considered, c.scope, c.started_at, c.reconstructed, c.kind,
                   p.act_id AS p_act_id, p.role_key AS p_role_key,
                   p.handed_by_kind AS p_hbk, p.handed_by_id AS p_hbi,
                   p.accountable AS p_accountable
            FROM edges c LEFT JOIN agent_acts p ON p.act_id = c.parent_act_id
            """
        ),
        {"p": project_id, "d": int(days), "r": role_key, "n": LINEAGE_ROW_CAP},
    ).mappings().all()
    nodes: dict[str, dict[str, Any]] = {centre["id"]: centre}
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        child = {"role_key": r["role_key"], "handed_by_kind": r["handed_by_kind"],
                 "handed_by_id": r["handed_by_id"], "accountable": r["accountable"]}
        if r["handed_by_kind"] == "agent" and r["handed_by_id"]:
            src = {"id": f"agent:{r['handed_by_id']}", "kind": "agent",
                   "role_key": r["handed_by_id"]}
        elif r["handed_by_kind"] == "person":
            src = _node(child | {"role_key": None})
        elif r["p_act_id"]:
            src = _node({"role_key": r["p_role_key"], "handed_by_kind": r["p_hbk"],
                         "handed_by_id": r["p_hbi"], "accountable": r["p_accountable"]})
        else:
            src = {"id": "boss", "kind": "boss"}
        dst = _node(child)
        if src["id"] == dst["id"] or centre["id"] not in (src["id"], dst["id"]):
            continue
        for node in (src, dst):
            nodes.setdefault(node["id"], {k: v for k, v in node.items() if k != "person"})
        edge = edges.setdefault((src["id"], dst["id"]), {
            "from": src["id"], "to": dst["id"], "acts": 0, "hows": {}, "last_at": None,
            "reconstructed": True, "sample": dict(r)})
        edge["acts"] += 1
        edge["hows"][r["how"]] = edge["hows"].get(r["how"], 0) + 1
        if edge["last_at"] is None or r["started_at"] > edge["last_at"]:
            edge["last_at"] = r["started_at"]
            edge["sample"] = dict(r)
        edge["reconstructed"] = edge["reconstructed"] and bool(r["reconstructed"])
    ranked = sorted(edges.values(), key=lambda e: -e["acts"])
    keep_nodes = {centre["id"]}
    out_edges = []
    for edge in ranked:
        ids = {edge["from"], edge["to"]}
        if len(keep_nodes | ids) > max_nodes:
            continue
        keep_nodes |= ids
        how_top = max(edge["hows"].items(), key=lambda kv: kv[1])[0]
        sample = edge["sample"]
        label = _label_for({"how": how_top, "scope": _json(sample.get("scope"), {}),
                            "considered": _json(sample.get("considered"), []),
                            "handed_by_kind": sample.get("handed_by_kind"),
                            "handed_by_id": sample.get("handed_by_id"),
                            "accountable": sample.get("accountable")}, viewer)
        if edge["acts"] > 1:
            label = f"{label} ×{edge['acts']}"
        out_edges.append({"from": edge["from"], "to": edge["to"], "acts": edge["acts"],
                          "how_top": how_top, "label": label, "last_at": _iso(edge["last_at"]),
                          "reconstructed": edge["reconstructed"]})
    return {"role_key": role_key, "mode": "observed", "days": int(days),
            "nodes": [n for k, n in nodes.items() if k in keep_nodes], "edges": out_edges}


def _declared_lineage(connection: Connection, project_id: str, role_key: str,
                      centre: dict[str, Any]) -> dict[str, Any]:
    # ONE HOME FOR A REPORTING LINE: org_assignments, the table Organization › Lines edits.
    # This read roles.reports_to, a second store nothing kept in step with the editor, so the
    # Passport's declared line and the chart could name two different bosses (spec §4.6).
    row = connection.execute(
        text("SELECT r.born_by, r.created_at, o.reports_to, o.updated_at FROM roles r "
             "LEFT JOIN org_assignments o ON o.project_id = r.project_id "
             "AND o.agent_id = r.role_key WHERE r.project_id = :p AND r.role_key = :r"),
        {"p": project_id, "r": role_key},
    ).mappings().first()
    if row is None:
        row = connection.execute(
            text("SELECT NULL AS born_by, NULL AS created_at, reports_to, updated_at "
                 "FROM org_assignments WHERE project_id = :p AND agent_id = :r"),
            {"p": project_id, "r": role_key},
        ).mappings().first()
    nodes = [centre]
    edges = []
    if row and row["reports_to"]:
        boss = resolve_role(str(row["reports_to"])) or str(row["reports_to"])
        nodes.append({"id": f"agent:{boss}", "kind": "agent", "role_key": boss})
        edges.append({"from": f"agent:{boss}", "to": centre["id"], "acts": 0, "how_top": "named",
                      "label": "Reports to",
                      "last_at": _iso(row["updated_at"] or row["created_at"]),
                      "reconstructed": False})
    born_by = str((row or {}).get("born_by") or "")
    if born_by and born_by != "spec-pack":
        if born_by.startswith("agent:"):
            node = {"id": born_by, "kind": "agent", "role_key": born_by.split(":", 1)[1]}
        else:
            node = {"id": f"person:{born_by}", "kind": "person"}
        nodes.append(node)
        edges.append({"from": node["id"], "to": centre["id"], "acts": 0, "how_top": "named",
                      "label": "Born by", "last_at": _iso((row or {}).get("created_at")),
                      "reconstructed": False})
    return {"role_key": role_key, "mode": "declared", "nodes": nodes, "edges": edges}


def _credit_line(kinds: list[str], labels: list[str]) -> str:
    """One line per agent in Credits, from what it did and nothing else."""
    if not kinds:
        return ""
    counts: dict[str, int] = {}
    for kind in kinds:
        counts[kind] = counts.get(kind, 0) + 1
    titles = list(dict.fromkeys(label.split(" · ")[0] for label in labels))
    head = " · ".join(titles[:2])
    if counts.get("approval") and len(counts) == 1:
        return "Approved" if counts["approval"] == 1 else f"Approved ×{counts['approval']}"
    if counts.get("run") and len(counts) == 1:
        return "Started the run"
    repeats = max(counts.values())
    return f"{head} · {repeats} rounds" if repeats > 1 and len(titles) == 1 else head


def chain(connection: Connection, project_id: str, workflow_run_id: str, *,
          viewer: Optional[str] = None) -> Optional[dict[str, Any]]:
    """A run's acts as one ordered tree, with who is accountable, totals and Credits."""
    root = connection.execute(
        text("SELECT * FROM agent_acts WHERE kind = 'run' AND workflow_run_id = :r "
             "AND project_id = :p ORDER BY started_at LIMIT 1"),
        {"r": workflow_run_id, "p": project_id},
    ).mappings().first()
    if not root:
        return None
    rows = [dict(r) for r in connection.execute(
        text("SELECT * FROM agent_acts WHERE root_act_id = :root ORDER BY started_at, act_id"),
        {"root": root["root_act_id"]},
    ).mappings().all()]
    children: dict[Optional[str], list[dict[str, Any]]] = {}
    for row in rows:
        children.setdefault(row.get("parent_act_id"), []).append(row)
    ordered: list[tuple[dict[str, Any], int]] = []

    def walk(act: dict[str, Any], depth: int) -> None:
        ordered.append((act, depth))
        for child in children.get(act["act_id"], []):
            walk(child, depth + 1)

    # This run's subtree: a child run's chain is its own, and its parent's chain contains it.
    walk(dict(root), 0)
    return _chain_of(connection, ordered, root, viewer=viewer,
                     ids={"workflow_run_id": workflow_run_id})


def chain_for_code_task(connection: Connection, project_id: str, code_task_run_id: str, *,
                        viewer: Optional[str] = None) -> Optional[dict[str, Any]]:
    """ONE BUILD'S chain, in the run chain's shape, so its Credits credit THIS build's acts
    (factory test D4: the cockpit credited the build's workflow run — "1 agent $0.00" while
    Outputs said 5 agents). Its root (or the first act that signed it), then every stage and
    seat, oldest first."""
    rows = [dict(r) for r in connection.execute(
        text("SELECT * FROM agent_acts WHERE project_id = :p AND code_task_run_id = :c "
             "AND kind IN ('run', 'stage') ORDER BY (kind = 'run') DESC, started_at, act_id"),
        {"p": project_id, "c": code_task_run_id},
    ).mappings().all()]
    if not rows:
        return None
    root = rows[0]
    ordered = [(root, 0)] + [(r, 1) for r in rows[1:]]
    return _chain_of(connection, ordered, root, viewer=viewer,
                     ids={"workflow_run_id": None, "code_task_run_id": code_task_run_id})


def _chain_of(connection: Connection, ordered: list[tuple[dict[str, Any], int]],
              root: Mapping[str, Any], *, viewer: Optional[str],
              ids: dict[str, Any]) -> dict[str, Any]:
    wire = _serialized(connection, [a for a, _ in ordered], viewer=viewer)
    for item, (_, depth) in zip(wire, ordered):
        item["depth"] = depth
    started = min((a["started_at"] for a, _ in ordered), default=None)
    ended = max(((a.get("ended_at") or _now()) for a, _ in ordered), default=None)
    minutes = round((ended - started).total_seconds() / 60, 1) if started and ended else 0
    credits_order: list[Optional[str]] = []
    doing: dict[Optional[str], tuple[list[str], list[str]]] = {}
    for act, _ in ordered:
        key = act.get("role_key")
        if key is None:
            if act["kind"] not in ("run", "approval"):
                continue  # a gate waiting is not something a person did
            key = None
        if key not in doing:
            credits_order.append(key)
            doing[key] = ([], [])
        doing[key][0].append(act["kind"])
        doing[key][1].append(act["label"])
    credits = []
    for key in credits_order:
        kinds, labels = doing[key]
        line = _credit_line(kinds, labels)
        if key is None:
            line = " · ".join(dict.fromkeys(
                "Started the run" if k == "run" else "Approved" for k in kinds))
        credits.append({"role_key": key, "line": line})
    return {
        **ids,
        "root_act_id": root["act_id"],
        "acts": wire,
        "accountable": root["accountable"],
        "totals": {"steps": sum(1 for a, _ in ordered if a["kind"] == "step"),
                   "minutes": minutes,
                   "cost": round(sum(w["cost_usd"] for w in wire), 6)},
        "credits": credits,
    }


# ── presence (§4.5): derived, never stored ───────────────────────────────────────────────────


def _age(since: Any) -> str:
    if since is None:
        return ""
    seconds = max(0, int((_now() - since).total_seconds()))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def presence_map(connection: Connection, project_id: str, *, include_idle: bool = True,
                 role_key: Optional[str] = None) -> dict[str, dict[str, Any]]:
    """Every agent's state, in the §4.5 priority: needs you > in a panel > working > idle.

    "Needs you" is this agent's OWN act waiting on a person. A gate waiting on a person is the
    operator's, not the agent whose finished work it holds (agents-home D4): that agent shows
    its last real state, and the gate reads as yours on Live, the pill and Activity. The line
    is built from the act's label — data, never a model's words.

    WHAT IT READS IS BOUNDED BY WHAT IS OPEN, NOT BY HISTORY. The open acts come off the
    partial `agent_acts_live` index. The idle line ("Last: …") is one index probe per agent
    (`agent_acts_role_ended`): the agents are found by skipping through that index, never by
    sorting every act the project has ever ended. `include_idle=False` (the 4s live poll) skips
    it altogether; `role_key` reads one agent (its Passport)."""
    params: dict[str, Any] = {"p": project_id, "h": LIVE_HORIZON_HOURS}
    only = ""
    if role_key:
        only = " AND (a.role_key = :r OR p.role_key = :r)"
        params["r"] = role_key
    open_rows = [dict(r) for r in connection.execute(
        text(
            f"""
            SELECT a.act_id, a.role_key, a.kind, a.status, a.label, a.started_at,
                   a.workflow_run_id, a.step_id, a.decision_id, p.role_key AS parent_role
            FROM agent_acts a LEFT JOIN agent_acts p ON p.act_id = a.parent_act_id
            WHERE a.project_id = :p AND a.ended_at IS NULL
              AND (a.status = 'awaiting_input'
                   OR a.started_at > now() - make_interval(hours => :h)){only}
            ORDER BY a.started_at DESC
            """
        ),
        params,
    ).mappings().all()]
    rank = {"needs_you": 0, "in_panel": 1, "working": 2}
    out: dict[str, dict[str, Any]] = {}
    verbs = verbs_for(connection, open_rows)

    def offer(role: Optional[str], state: str, row: dict[str, Any], line: str) -> None:
        if not role or (role_key and role != role_key):
            return
        held = out.get(role)
        if held is None or rank[state] < rank[held["_rank"]]:
            out[role] = {"state": state, "act_id": row["act_id"], "line": line,
                         "since": _iso(row["started_at"]), "_rank": state}

    for row in open_rows:
        if row["status"] == "awaiting_input":
            # Only the agent's own ask. A gate's wait — a step held on a decision, role-less
            # since D4 and role-bearing in acts written before it — belongs to nobody here.
            if row["kind"] == "step" and row.get("decision_id"):
                continue
            offer(row["role_key"], "needs_you", row, f"Waiting on you: {row['label']}")
        elif row["kind"] == "seat":
            offer(row["role_key"], "in_panel", row, f"In a panel: {row['label']}")
        else:
            offer(row["role_key"], "working", row,
                  f"Now: {verbs.get(str(row['act_id']), 'Working')} · {row['label']}")

    if include_idle:
        ended = _last_ended(connection, project_id, role_key)
        last_verbs = verbs_for(connection, ended)
        for row in ended:
            if row["role_key"] in out:
                continue
            verb = last_verbs.get(str(row["act_id"]), "Working")
            out[row["role_key"]] = {"state": "idle", "act_id": row["act_id"],
                                    "line": f"Last: {verb} · {row['label']} · "
                                            f"{_age(row['ended_at'])}",
                                    "since": _iso(row["ended_at"]), "_rank": "idle"}
    for value in out.values():
        value.pop("_rank", None)
    return out


def _last_ended(connection: Connection, project_id: str,
                role_key: Optional[str]) -> list[dict[str, Any]]:
    """Each agent's most recent ended act: a skip-scan for the agents, one probe each."""
    roles_sql = (
        "SELECT CAST(:r AS text) AS role_key" if role_key else
        """
        WITH RECURSIVE agents AS (
            (SELECT role_key FROM agent_acts
             WHERE project_id = :p AND role_key IS NOT NULL AND ended_at IS NOT NULL
             ORDER BY role_key LIMIT 1)
            UNION ALL
            SELECT (SELECT n.role_key FROM agent_acts n
                    WHERE n.project_id = :p AND n.role_key > agents.role_key
                      AND n.ended_at IS NOT NULL
                    ORDER BY n.role_key LIMIT 1)
            FROM agents WHERE agents.role_key IS NOT NULL
        )
        SELECT role_key FROM agents WHERE role_key IS NOT NULL
        """
    )
    return [dict(r) for r in connection.execute(
        text(
            f"""
            SELECT a.role_key, l.act_id, l.label, l.ended_at, l.status, l.kind, l.step_id,
                   l.workflow_run_id
            FROM ({roles_sql}) a
            CROSS JOIN LATERAL (
                SELECT act_id, label, ended_at, status, kind, step_id, workflow_run_id
                FROM agent_acts
                WHERE project_id = :p AND role_key = a.role_key AND ended_at IS NOT NULL
                ORDER BY ended_at DESC LIMIT 1
            ) l
            """
        ),
        {"p": project_id, **({"r": role_key} if role_key else {})},
    ).mappings().all()]


def live(connection: Connection, project_id: str) -> dict[str, Any]:
    """The agents that are not idle, and an ETag over exactly that, for a cheap 4s poll. Reads
    only what is open: the cost of the hottest poll does not grow with the ledger."""
    present = sorted(presence_map(connection, project_id, include_idle=False).items())
    items = [{"role_key": role, **p} for role, p in present if p["state"] != "idle"]
    etag = hashlib.sha256(json.dumps(
        [(i["role_key"], i["state"], i["act_id"]) for i in items]).encode()).hexdigest()[:20]
    return {"items": items, "etag": f'"{etag}"'}


# ── who a decision belongs to (agents-home spec §6.2) ────────────────────────────────────────


def owners_of_decisions(connection: Connection, project_id: str,
                        decision_ids: Iterable[str]) -> dict[str, Optional[str]]:
    """{decision_id: role_key or None} — the agent a waiting decision is ABOUT.

    In order: the role of the act the decision is linked to (agent_acts.decision_id); else
    that act's parent's role (a gate's step act is nobody's — it holds the work of the step
    before it); else the gated step's `captain` when it names an agent. Else None: a card with
    no attributed agent shows BOSS or You, never a guessed face."""
    ids = [str(d) for d in dict.fromkeys(decision_ids) if d]
    if not ids:
        return {}
    out: dict[str, Optional[str]] = {d: None for d in ids}
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (a.decision_id) a.decision_id, a.role_key,
                   p.role_key AS parent_role
            FROM agent_acts a LEFT JOIN agent_acts p ON p.act_id = a.parent_act_id
            WHERE a.project_id = :p AND a.decision_id = ANY(:ids) AND a.kind <> 'approval'
            ORDER BY a.decision_id, a.started_at DESC
            """
        ),
        {"p": project_id, "ids": ids},
    ).mappings().all()
    for r in rows:
        out[str(r["decision_id"])] = r["role_key"] or r["parent_role"] or None
    missing = [d for d, role in out.items() if role is None]
    if missing:
        from .step_agents import named_agent

        for r in connection.execute(
            text(
                """
                SELECT s.decision_id, s.step_id, v.spec
                FROM workflow_step_runs s
                JOIN workflow_runs r ON r.id = s.workflow_run_id
                JOIN workflow_versions v ON v.id = r.workflow_version_id
                WHERE s.decision_id = ANY(:ids) AND r.project_id = :p
                """
            ),
            {"ids": missing, "p": project_id},
        ).mappings().all():
            spec = r["spec"] if isinstance(r["spec"], dict) else {}
            step = next((st for st in spec.get("steps") or []
                         if isinstance(st, dict) and str(st.get("id")) == str(r["step_id"])), {})
            named = str(step.get("captain") or "").strip().lower()
            if not named:
                try:
                    named = str(named_agent(step) or "").strip().lower()
                except Exception:  # noqa: BLE001
                    named = ""
            if named and named != "human":
                out[str(r["decision_id"])] = resolve_role(named) or named
    return out


def owner_of_decision(connection: Connection, project_id: str,
                      decision_id: str) -> Optional[str]:
    """The agent one waiting decision is about, or None. See owners_of_decisions."""
    return owners_of_decisions(connection, project_id, [decision_id]).get(str(decision_id))


def step_owners(connection: Connection, project_id: str,
                pairs: Iterable[tuple[str, str]]) -> dict[tuple[str, str], Optional[str]]:
    """{(run_id, step_id): role_key} — who ran a step, from its newest step act."""
    pairs = list(dict.fromkeys((str(r), str(s)) for r, s in pairs if r and s))
    if not pairs:
        return {}
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (workflow_run_id, step_id) workflow_run_id, step_id, role_key
            FROM agent_acts
            WHERE project_id = :p AND kind = 'step' AND workflow_run_id = ANY(:runs)
            ORDER BY workflow_run_id, step_id, started_at DESC
            """
        ),
        {"p": project_id, "runs": sorted({r for r, _ in pairs})},
    ).mappings().all()
    found = {(str(r["workflow_run_id"]), str(r["step_id"])): r["role_key"] for r in rows}
    return {pair: found.get(pair) for pair in pairs}


# ── the project's river of work (agents-home spec §4.9, §6.4) ───────────────────────────────

#: Activity filters → what they select. A handoff is an edge an agent, a captain or BOSS made.
ACTIVITY_KINDS = ("needs_you", "handoffs", "builds", "panels", "talks")
_HANDOFF_HOWS = ("captain", "asked_by_agent", "model_pick")
#: Kinds that are a step's OWN inner work when they hang off one (chain-model.ts NESTED).
_NESTED_KINDS = ("stage", "seat", "run", "chat")
#: The SQL form of chain-model.ts nestedUnderWork: an act folded under the work it belongs to.
_NESTED_SQL = ("(p.act_id IS NOT NULL AND p.kind <> 'run' AND "
               "(p.kind IN ('stage', 'seat') OR a.kind IN ('stage', 'seat', 'run', 'chat')))")


def activity(connection: Connection, project_id: str, *, cursor: Optional[str] = None,
             limit: int = 50, kinds: Optional[Iterable[str]] = None,
             role_key: Optional[str] = None, run_id: Optional[str] = None,
             parent_act_id: Optional[str] = None, days: int = 1,
             viewer: Optional[str] = None) -> dict[str, Any]:
    """Every agent's work on the project, newest first: acts_for_agent without the role.

    Each item is the act's wire shape plus `verb`, `from`, `children{count}` (its own inner
    work, folded) and `turns{tool_calls}` (how many tool calls fold under it; 0 = no fold
    control, never a promise). Unfiltered, it reads the work that stands on its own — a step,
    a standalone build stage, a chat — and folds the rest under it; `parent` reads one act's
    folded work, `run` a whole run, and `role` / `kinds` everything that matches. A run's root
    (the person starting it) is not agent work and is left out unless `run` asks."""
    limit = max(1, min(int(limit), 200))
    days = 7 if int(days or 1) >= 7 else 1
    wanted = [k for k in (kinds or []) if k]
    unknown = sorted(set(wanted) - set(ACTIVITY_KINDS))
    if unknown:
        raise ValueError(f"unknown activity filter {unknown[0]!r}; use one of "
                         f"{', '.join(ACTIVITY_KINDS)}")
    params: dict[str, Any] = {"p": project_id, "d": days, "n": limit + 1}
    where = ["a.project_id = :p", "a.started_at > now() - make_interval(days => :d)"]
    if parent_act_id:
        where.append("a.parent_act_id = :parent")
        where.append(_NESTED_SQL)
        params["parent"] = parent_act_id
    elif run_id:
        where.append("a.workflow_run_id = :run")
        params["run"] = run_id
    else:
        where.append("a.kind <> 'run'")
        if not wanted and not role_key:
            where.append(f"NOT {_NESTED_SQL}")
    if role_key:
        where.append("a.role_key = :r")
        params["r"] = role_key
    ors = []
    if "needs_you" in wanted:
        ors.append("a.status = 'awaiting_input'")
    if "handoffs" in wanted:
        ors.append("(a.how = ANY(:hows) OR a.kind = 'handoff')")
        params["hows"] = list(_HANDOFF_HOWS)
    if "builds" in wanted:
        ors.append("a.kind = 'stage'")
    if "panels" in wanted:
        ors.append("a.kind = 'seat'")
    if "talks" in wanted:
        ors.append("a.kind = 'chat'")
    if ors:
        where.append("(" + " OR ".join(ors) + ")")
    if cursor:
        at, _, after = cursor.partition("|")
        where.append("(a.started_at, a.act_id) < (CAST(:ca AS timestamptz), :cid)")
        params.update(ca=at, cid=after)
    rows = [dict(r) for r in connection.execute(
        text(f"SELECT a.* FROM agent_acts a LEFT JOIN agent_acts p "
             f"ON p.act_id = a.parent_act_id WHERE {' AND '.join(where)} "
             "ORDER BY a.started_at DESC, a.act_id DESC LIMIT :n"),
        params,
    ).mappings().all()]
    more = len(rows) > limit
    rows = rows[:limit]
    ids = [r["act_id"] for r in rows]
    parents = {r["act_id"]: dict(r) for r in connection.execute(
        text("SELECT * FROM agent_acts WHERE act_id = ANY(:a)"),
        {"a": [r["parent_act_id"] for r in rows if r.get("parent_act_id")]},
    ).mappings().all()} if rows else {}
    nested: dict[str, int] = {}
    tools: dict[str, int] = {}
    if ids:
        for r in connection.execute(
            text(f"SELECT a.parent_act_id AS id, count(*) AS n FROM agent_acts a "
                 f"JOIN agent_acts p ON p.act_id = a.parent_act_id "
                 f"WHERE a.parent_act_id = ANY(:ids) AND {_NESTED_SQL} GROUP BY 1"),
            {"ids": ids},
        ).mappings().all():
            nested[str(r["id"])] = int(r["n"])
        for r in connection.execute(
            text("SELECT act_id AS id, count(*) AS n FROM agent_turns "
                 "WHERE act_id = ANY(:ids) AND direction = 'tool_call' GROUP BY 1"),
            {"ids": ids},
        ).mappings().all():
            tools[str(r["id"])] = int(r["n"])
    items = []
    for act, wire in zip(rows, _serialized(connection, rows, viewer=viewer)):
        wire["from"] = _from_node(act, parents.get(act.get("parent_act_id")))
        wire["children"] = {"count": nested.get(str(act["act_id"]), 0)}
        wire["turns"] = {"tool_calls": tools.get(str(act["act_id"]), 0)}
        items.append(wire)
    nxt = f"{_iso(rows[-1]['started_at'])}|{rows[-1]['act_id']}" if more and rows else None
    return {"project_id": project_id, "items": items, "next": nxt}


#: The most tool calls one act shows; past it the Activity log has them all.
TOOL_CALLS_CAP = 50


def tool_calls(connection: Connection, project_id: str, act_id: str, *,
               limit: int = TOOL_CALLS_CAP) -> Optional[dict[str, Any]]:
    """The tool calls folded under one act: name, whether it worked, how long it took.

    NEVER CONTENT. A tool call's arguments and its result are the customer's code and data;
    this reads only the columns that say what was called and how it went. A call is paired
    with the next result of the same tool (the CLI writes them in order); a call with no
    result yet has `ok: None`. None when the act is not on this project."""
    owner = connection.execute(
        text("SELECT project_id FROM agent_acts WHERE act_id = :a"), {"a": act_id}).scalar()
    if owner != project_id:
        return None
    limit = max(1, min(int(limit), TOOL_CALLS_CAP))
    rows = connection.execute(
        text("SELECT direction, tool_name, ok, latency_ms, created_at FROM agent_turns "
             "WHERE act_id = :a AND direction IN ('tool_call', 'tool_result') "
             "ORDER BY created_at, turn_index, id"),
        {"a": act_id},
    ).mappings().all()
    calls: list[dict[str, Any]] = []
    waiting: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        name = str(r["tool_name"] or "")
        if r["direction"] == "tool_call":
            item = {"tool_name": name or "tool", "ok": None,
                    "latency_ms": int(r["latency_ms"]) if r["latency_ms"] else None,
                    "at": _iso(r["created_at"])}
            calls.append(item)
            waiting.setdefault(name, []).append(item)
        else:
            queue = waiting.get(name) or (waiting.get("") if not name else None) or []
            if not queue and not name:
                queue = next((q for q in waiting.values() if q), [])
            if queue:
                item = queue.pop(0)
                item["ok"] = bool(r["ok"])
                if r["latency_ms"]:
                    item["latency_ms"] = int(r["latency_ms"])
    return {"act_id": act_id, "items": calls[:limit], "more": len(calls) > limit,
            "total": len(calls)}


def copy_declared_lines(connection: Connection, project_id: Optional[str] = None) -> int:
    """One-time, idempotent: roles.reports_to takes the plan's line (org_assignments) for
    history. Nothing reads roles.reports_to for a declared line any more; returns rows changed."""
    return int(connection.execute(
        text("UPDATE roles r SET reports_to = o.reports_to FROM org_assignments o "
             "WHERE o.project_id = r.project_id AND o.agent_id = r.role_key "
             "AND r.reports_to IS DISTINCT FROM o.reports_to"
             + (" AND r.project_id = :p" if project_id else "")),
        {"p": project_id} if project_id else {},
    ).rowcount or 0)


def observed_edges(connection: Connection, project_id: str, *,
                   hours: float = 168, with_outcomes: bool = False,
                   include_people: bool = False, sequence: bool = False,
                   with_newest: bool = False) -> list[dict[str, Any]]:
    """Agent→agent parent→child pairs in the window: the Team chart's observed handoffs.

    The defaults are the Team chart's reader, unchanged. Organization (agents_org_board) asks
    for more (org-view spec §6.1): `with_outcomes` adds, per pair, how many of the handed acts
    ended in `error`, are open and `awaiting_input`, or went `stale`, plus the raw `how`;
    `include_people` adds person→agent rows (`source: "person"`): an agent's act whose parent is
    no agent and that a person handed — directly (a chat they opened), or as the first work of a
    run they started (the step is handed by BOSS; the RUN act it hangs from is the person's).

    `sequence` adds the hand-offs a workflow makes WITHOUT naming them: the worker records BOSS
    as every step's hander and hangs each step from the run act, so no step has an agent parent
    and every edge looked like it came from BOSS. Inside one run (root act), the step after
    another is handed by the previous step's agent — the order the work actually went — so
    consecutive BOSS-handed steps/stages/seats by different agents become previous→next pairs.
    Their counts merge into the parent→child pairs. The Command Center's Floor asks for it.
    `with_newest` adds, per pair, the newest handed act and its run (for a comet to fly once).

    `hours` may be fractional (the Floor reads the last hour)."""
    outcomes = (
        ", count(*) FILTER (WHERE c.status = 'error') AS failed,"
        " count(*) FILTER (WHERE c.status = 'awaiting_input' AND c.ended_at IS NULL) AS waiting,"
        " count(*) FILTER (WHERE c.status = 'stale') AS stale"
    ) if with_outcomes else ""
    newest = (
        ", (array_agg(c.act_id ORDER BY c.started_at DESC))[1] AS newest_act_id,"
        " (array_agg(c.workflow_run_id ORDER BY c.started_at DESC))[1] AS run_id"
    ) if with_newest else ""
    window = "c.started_at > now() - make_interval(secs => :s)"
    # With `sequence`, a person hands a run's FIRST agent; the later steps are the agents' own
    # hand-offs (below), not the person's again.
    first_only = (
        "AND NOT EXISTS (SELECT 1 FROM agent_acts e WHERE e.root_act_id = c.root_act_id"
        " AND e.role_key IS NOT NULL AND e.kind IN ('step', 'stage', 'seat')"
        " AND (e.started_at, e.act_id) < (c.started_at, c.act_id))"
    ) if sequence else ""
    rows = connection.execute(
        text(
            f"""
            SELECT p.role_key AS source, c.role_key AS target, count(*) AS times,
                   max(c.started_at) AS last_at, mode() WITHIN GROUP (ORDER BY c.how) AS how
                   {outcomes}{newest}
            FROM agent_acts c JOIN agent_acts p ON p.act_id = c.parent_act_id
            WHERE c.project_id = :p AND c.role_key IS NOT NULL AND p.role_key IS NOT NULL
              AND c.role_key <> p.role_key
              AND {window}
            GROUP BY 1, 2
            """
        ),
        {"p": project_id, "s": float(hours) * 3600},
    ).mappings().all()
    people = connection.execute(
        text(
            f"""
            SELECT 'person' AS source, c.role_key AS target, count(*) AS times,
                   max(c.started_at) AS last_at, mode() WITHIN GROUP (ORDER BY c.how) AS how
                   {outcomes}{newest}
            FROM agent_acts c LEFT JOIN agent_acts p ON p.act_id = c.parent_act_id
            WHERE c.project_id = :p AND c.role_key IS NOT NULL AND p.role_key IS NULL
              AND (c.handed_by_kind = 'person' OR p.handed_by_kind = 'person')
              AND {window}
              {first_only}
            GROUP BY 2
            """
        ),
        {"p": project_id, "s": float(hours) * 3600},
    ).mappings().all() if include_people else []
    # PREVIOUS STEP'S AGENT → NEXT STEP'S AGENT, inside one run (see the docstring).
    steps = connection.execute(
        text(
            f"""
            WITH seq AS (
              SELECT a.act_id, a.role_key, a.status, a.ended_at, a.how, a.started_at,
                     a.workflow_run_id, a.handed_by_kind, a.parent_act_id,
                     LAG(a.role_key) OVER (PARTITION BY a.root_act_id
                                           ORDER BY a.started_at, a.act_id) AS prev_role
              FROM agent_acts a
              WHERE a.project_id = :p AND a.role_key IS NOT NULL
                AND a.kind IN ('step', 'stage', 'seat')
                AND a.root_act_id IN (SELECT root_act_id FROM agent_acts
                                      WHERE project_id = :p
                                        AND started_at > now() - make_interval(secs => :s))
            )
            SELECT c.prev_role AS source, c.role_key AS target, count(*) AS times,
                   max(c.started_at) AS last_at, mode() WITHIN GROUP (ORDER BY c.how) AS how
                   {outcomes}{newest}
            FROM seq c LEFT JOIN agent_acts p ON p.act_id = c.parent_act_id
            WHERE c.prev_role IS NOT NULL AND c.prev_role <> c.role_key
              AND c.handed_by_kind = 'boss' AND p.role_key IS NULL
              AND {window}
            GROUP BY 1, 2
            """
        ),
        {"p": project_id, "s": float(hours) * 3600},
    ).mappings().all() if sequence else []
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for r in [*rows, *steps, *people]:
        edge = {"source": r["source"], "target": r["target"], "times": int(r["times"]),
                "last_action": edge_label(r["how"]) if r["how"] in HOWS else None,
                "last_at": r["last_at"]}
        if with_outcomes:
            edge.update(failed=int(r["failed"] or 0), waiting=int(r["waiting"] or 0),
                        stale=int(r["stale"] or 0), how=r["how"])
        if with_newest:
            edge.update(newest_act_id=r["newest_act_id"], run_id=r["run_id"])
        key = (str(edge["source"]), str(edge["target"]))
        have = merged.get(key)
        if have is None:
            merged[key] = edge
            continue
        # One pair, two readings (named parent and run order): one edge, counts summed.
        have["times"] += edge["times"]
        for k in ("failed", "waiting", "stale"):
            if k in have:
                have[k] += edge[k]
        if edge["last_at"] and (not have["last_at"] or edge["last_at"] > have["last_at"]):
            have["last_at"] = edge["last_at"]
            for k in ("last_action", "how", "newest_act_id", "run_id"):
                if k in edge:
                    have[k] = edge[k]
    return list(merged.values())


def my_recent_work(connection: Connection, project_id: str, role_key: str, *, limit: int = 10,
                   kind: Optional[str] = None) -> list[dict[str, Any]]:
    """The calling agent's own acts, for it to cite as [act:…]."""
    page = acts_for_agent(connection, project_id, role_key, kind=kind, days=90,
                          limit=max(1, min(int(limit), 25)))
    return [{"act_id": a["act_id"], "kind": a["kind"], "label": a["label"],
             "status": a["status"], "how": a["edge_label"], "started_at": a["started_at"],
             "ended_at": a["ended_at"]} for a in page["items"]]


# ── backfill (§5.1): reconstructed acts from legacy rows ─────────────────────────────────────


def backfill(engine: Any, project_id: str, *, pause_seconds: float = 0.0) -> int:
    """Acts for work done before the ledger existed. Returns how many acts it wrote.

    Idempotent per source row; every act it writes is `reconstructed`, and the parent order is
    inferred, which the UI says (dashed, `Reconstructed`). Sources: workflow step rows with
    ran_as, panel seats, and build stages from the llm_calls stage order of each code task.

    ONE SHORT TRANSACTION PER RUN, PANEL OR BUILD, never the project's whole history in one:
    each takes its subject's lock with a try, so a live tick or build always wins and the unit
    is picked up next time. Historical step rows are read, never rewritten. `pause_seconds`
    is the breath between units the boot job takes, so history is a trickle beside live work."""
    from ..db import begin

    written = 0

    def breathe() -> None:
        if pause_seconds:
            time.sleep(pause_seconds)

    def candidates(sql: str) -> list[Any]:
        with begin(engine) as c:
            return list(c.execute(text(sql), {"p": project_id}).all())

    for (run_id,) in candidates(
        """
        SELECT r.id FROM workflow_runs r
        JOIN workflow_definitions d ON d.id = r.workflow_definition_id
        WHERE r.project_id = :p AND COALESCE(d.namespace, 'system') = 'product'
          AND NOT EXISTS (SELECT 1 FROM agent_acts a WHERE a.workflow_run_id = r.id)
        ORDER BY r.created_at
        """
    ):
        with begin(engine) as c:
            if not _lock(c, f"run:{run_id}", wait=False) or _count(
                    c, "workflow_run_id = :s", run_id):
                continue
            sync_run(c, str(run_id), wait=False, reconstructed=True)
            written += _count(c, "workflow_run_id = :s", run_id)
        breathe()

    for (meeting_id,) in candidates(
        """
        SELECT m.id FROM meeting_runs m WHERE m.project_id = :p AND NOT EXISTS
            (SELECT 1 FROM agent_acts a WHERE a.meeting_run_id = m.id AND a.kind = 'seat')
        ORDER BY m.created_at
        """
    ):
        with begin(engine) as c:
            before = _count(c, "meeting_run_id = :s", meeting_id)
            sync_meeting(c, str(meeting_id), wait=False, reconstructed=True)
            written += _count(c, "meeting_run_id = :s", meeting_id) - before
        breathe()

    for (task_id,) in candidates(
        """
        SELECT DISTINCT c.code_task_run_id FROM llm_calls c
        WHERE c.project_id = :p AND c.code_task_run_id IS NOT NULL AND c.act_id IS NULL
          AND NOT EXISTS (SELECT 1 FROM agent_acts a
                          WHERE a.code_task_run_id = c.code_task_run_id AND a.kind = 'stage')
        """
    ):
        with begin(engine) as c:
            if not _lock(c, f"task:{task_id}", wait=False):
                continue
            before = _count(c, "code_task_run_id = :s", task_id)
            _backfill_build(c, project_id, str(task_id))
            written += _count(c, "code_task_run_id = :s", task_id) - before
        breathe()
    return written


def _count(connection: Connection, where: str, subject: Any) -> int:
    return int(connection.execute(
        text(f"SELECT count(*) FROM agent_acts WHERE {where}"), {"s": str(subject)}
    ).scalar() or 0)


def _backfill_build(connection: Connection, project_id: str, code_task_run_id: str) -> None:
    calls = connection.execute(
        text("SELECT id, stage FROM llm_calls WHERE code_task_run_id = :c AND project_id = :p "
             "AND act_id IS NULL ORDER BY created_at, id"),
        {"c": code_task_run_id, "p": project_id},
    ).all()
    for call_id, stage in calls:
        got = stage_act(connection, project_id=project_id, code_task_run_id=code_task_run_id,
                        stage=stage, reconstructed=True)
        if got:
            connection.execute(text("UPDATE llm_calls SET act_id = :a WHERE id = :i"),
                               {"a": got["act_id"], "i": call_id})
    close_code_task(connection, code_task_run_id)
    # Its stages' times are its calls' times, not the moment they were rebuilt.
    connection.execute(
        text(
            """
            UPDATE agent_acts a SET started_at = t.first_at,
                   ended_at = CASE WHEN a.ended_at IS NULL THEN NULL ELSE t.last_at END
            FROM (SELECT act_id, min(created_at) AS first_at, max(created_at) AS last_at
                  FROM llm_calls WHERE code_task_run_id = :c AND act_id IS NOT NULL
                  GROUP BY act_id) t
            WHERE a.act_id = t.act_id AND a.reconstructed AND a.kind = 'stage'
            """
        ),
        {"c": code_task_run_id},
    )


def backfill_all(engine: Any, *, pause_seconds: float = 0.05) -> dict[str, int]:
    """The boot job: every project's history, one short transaction per unit. Never raises."""
    written: dict[str, int] = {}
    try:
        from ..db import begin

        with begin(engine) as c:
            projects = [str(p) for p in c.execute(text("SELECT id FROM projects")).scalars()]
    except Exception as error:  # noqa: BLE001 — a boot job never stops the boot
        log.warning("agent acts: backfill could not list projects: %s", error)
        return written
    for project_id in projects:
        try:
            written[project_id] = backfill(engine, project_id, pause_seconds=pause_seconds)
        except Exception as error:  # noqa: BLE001
            log.warning("agent acts: backfill of %s stopped: %s: %s", project_id,
                        type(error).__name__, error)
    return written
