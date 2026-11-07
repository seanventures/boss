"""Agents › Live in one read: what needs you, what is moving, what got done, who is idle.

THE CONTRACT (agents-home spec §6.2), frozen in Phase 0. apps/web/lib/agents-live-board.ts
parses exactly these shapes; change them with the lead, never inside a stream.

PURE COMPOSITION. Nothing here restates a rule another module owns:
  * `attention` and `counts.needs_you` ARE command_center.assemble().needs_you — same ranking,
    same expiry sweep — so the Live tab, the pill, the Command Center and Approvals count one
    list (engine/tests/test_every_surface_agrees_on_what_needs_the_operator.py).
  * `missions` come from the run rows /v1/delivery-workflows/runs reads, plus agent_acts.chain.
  * `teams` / `agents` come from agent_acts.presence_map and agent_identity.team_of.
  * every verb comes from agent_acts.work_verb. No model writes any line here.

NOTHING IS ESTIMATED. There is no percent and no ETA: no field of that name may appear in the
payload (apps/api/tests/test_the_live_board_tells_the_truth.py scans for them).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping, Optional, TypedDict

from sqlalchemy import text

MissionState = Literal["running", "waiting_you", "waiting_person", "waiting", "stopped",
                       "blocked", "stalled", "done"]
AttentionKind = Literal["approval", "failed_step", "readiness", "gate_override", "budget_refused",
                        "system_health"]


class AttentionAction(TypedDict):
    action: str
    label: str
    tone: str
    effect: str
    needs: list[str]


class AttentionRun(TypedDict, total=False):
    id: str
    subject: str
    step_id: Optional[str]
    step_label: Optional[str]
    step_index: Optional[int]
    step_count: Optional[int]


class AttentionItem(TypedDict, total=False):
    id: str                      # decision id | "run:<run>:<step>" | "ready:<key>"
    kind: AttentionKind
    decision_kind: Optional[str]
    decision_id: Optional[str]
    title: str
    detail: str
    what_to_do: Optional[str]
    role_key: Optional[str]      # attributed agent (agent_acts.owner_of_decision), never guessed
    run: Optional[AttentionRun]
    actions: list[AttentionAction]
    answers: list[dict[str, Any]]
    decide_inline: bool          # §4.2 policy, decided here, never by the web
    content_hash: Optional[str]
    preview: Optional[str]
    risk_tier: Optional[int]
    fix: Optional[dict[str, str]]
    at: Optional[str]
    # Where the question comes from, in a word or two ("Spend guard", the workflow's name,
    # "Factory"): decision_words.headline, never a raw code.
    source: Optional[str]
    # The build this is about when its subject is a code task (factory spec §6.5):
    # {run_id, app, where, failing: [{name, label, waivable, reason?}], retryable, retry_reason,
    #  pr_url?, preview_url?, plan_url?}
    build: Optional[dict[str, Any]]


class Hop(TypedDict, total=False):
    act_id: str
    role_key: Optional[str]
    person: Optional[str]
    state: str                   # an AvatarState
    label: str
    edge_label: str
    how: str
    inside: int                  # acts folded inside this hop
    gate: bool                   # a gate's own act, waiting on a person (no agent)
    loop: dict[str, Any]         # {a, b, rounds} instead of a hop
    fold: dict[str, int]         # {hidden: n} — hops folded out of a long lane


class NextHop(TypedDict):
    role_key: Optional[str]      # spec captains only; Auto → None
    step_label: str
    named: bool


class Mission(TypedDict, total=False):
    run_id: str
    subject: str
    workflow_key: str
    workflow_name: str
    version: Optional[int]
    actor: Optional[str]
    state: MissionState
    standing: str
    step_index: int
    step_count: int
    step_label: Optional[str]
    verb: str
    waiting_on: Optional[str]
    waiting_on_role: Optional[str]
    blocked_reason: Optional[str]
    error: Optional[str]
    attention_id: Optional[str]
    started_at: Optional[str]
    step_since: Optional[str]
    elapsed_seconds: Optional[int]
    step_elapsed_seconds: Optional[int]
    last_progress_at: Optional[str]
    cost_usd: float
    hops: list[Hop]
    folded: bool
    next: list[NextHop]
    finished_at: Optional[str]


class Outcome(TypedDict, total=False):
    act_id: Optional[str]
    role_key: Optional[str]
    kind: Literal["pr", "pr_merged", "report", "minutes", "run"]
    text: str
    url: Optional[str]
    run_id: Optional[str]
    label: Optional[str]
    at: str


class Team(TypedDict, total=False):
    key: str
    label: str
    tip: Optional[str]
    members: list[str]
    working: int
    needs_you: int
    in_panel: int


class BoardAgent(TypedDict, total=False):
    role_key: str
    name: str
    callsign: Optional[str]
    team: str
    state: str                   # AvatarState | "in_panel"
    line: str
    since: Optional[str]


class Counts(TypedDict, total=False):
    agents: int
    working: int
    needs_you: int               # decisions only (agents-home D2)
    running: int
    stalled: int                 # live runs with no progress for STALLED_AFTER, apart from running
    stopped: int
    setup: int                   # readiness gaps, counted apart
    building: int            # the Factory's BUILDING rows (factory spec §10, decision 2)


class LiveBoard(TypedDict):
    project_id: str
    etag: str
    generated_at: str
    counts: Counts
    attention: list[AttentionItem]
    missions: list[Mission]
    outcomes: list[Outcome]
    teams: list[Team]
    agents: list[BoardAgent]
    # factory_view.BuildRow, the Floor's BUILDING rows — the same rows, the same words.
    builds: list[dict[str, Any]]


# ── policy ───────────────────────────────────────────────────────────────────────────────────

#: Decision kinds a card may approve or reject in place (spec §4.2). A pull request, a refusal
#: (a notice with guidance) and anything else go to Approvals with `Review ↗`.
INLINE_KINDS = frozenset({"workflow_gate", "deploy_gate", "general"})
#: The highest risk tier a card may decide inline. Above it, the decision needs a closer look.
INLINE_MAX_RISK = 2
#: How long a finished run stays on Live, so a finish is seen.
JUST_FINISHED = timedelta(minutes=10)
#: Outcomes reach back this far.
OUTCOME_WINDOW = timedelta(hours=24)
#: The most runs whose chain one read draws. Past it a row keeps its words, not its faces.
CHAIN_CAP = 25
#: A lane longer than this folds: the first 2 hops, `+n`, the last 4 (spec §4.3).
LANE_MAX = 7
LANE_HEAD, LANE_TAIL = 2, 4
OUTCOMES_MAX = 8
NEXT_MAX = 4


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _aware(value: Any) -> Optional[datetime]:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _rec(decision: Mapping[str, Any]) -> dict[str, Any]:
    rec = decision.get("recommendation")
    if isinstance(rec, str):
        try:
            rec = json.loads(rec)
        except ValueError:
            rec = {}
    return rec if isinstance(rec, dict) else {}


def _needs(action: Mapping[str, Any]) -> list[str]:
    needs = action.get("needs")
    if isinstance(needs, (list, tuple)):
        return [str(n) for n in needs if n]
    return [str(needs)] if needs else []


def _preview_line(preview: Mapping[str, Any]) -> Optional[str]:
    """One short line naming what approving will do, from the preview's own fields."""
    if not preview or preview.get("kind") == "unresolved":
        return None
    for key in ("title", "subject", "summary", "name", "filename"):
        value = preview.get(key)
        if isinstance(value, str) and value.strip():
            line = value.strip().splitlines()[0]
            return line if len(line) <= 120 else line[:119].rstrip() + "…"
    return None


def decide_inline(kind: Optional[str], risk_tier: Any, actions: list[dict[str, Any]],
                  has_preview: bool, content_hash: Optional[str]) -> bool:
    """THE INLINE-DECIDE RULE (spec §4.2), decided here and never by the web: a workflow gate,
    deploy gate or general decision, at risk tier 2 or below, whose approve and reject need
    nothing more (no note, no name), and — when approving is approving content — with the
    hash of the content the card shows."""
    if kind not in INLINE_KINDS:
        return False
    try:
        if risk_tier is None or int(risk_tier) > INLINE_MAX_RISK:
            return False
    except (TypeError, ValueError):
        return False
    drawn = [a for a in actions if a.get("action") in ("approve", "reject")]
    if not any(a.get("action") == "approve" for a in drawn):
        return False
    if any(_needs(a) for a in drawn):
        return False
    if has_preview and not content_hash:
        return False
    return True


# ── attention: the Command Center's needs_you, attributed ────────────────────────────────────


def attention_items(connection: Any, project_id: str, needs_you: list[dict[str, Any]],
                    decisions: list[Mapping[str, Any]]) -> list[AttentionItem]:
    """`command_center.assemble().needs_you`, in its order, with who each is about and whether
    the card may decide it. Nothing is re-derived: the fields pass through."""
    from . import agent_acts

    by_id = {str(d.get("id")): d for d in decisions}
    owners = agent_acts.owners_of_decisions(
        connection, project_id,
        [str(n.get("decision_id")) for n in needs_you if n.get("kind") == "approval"])
    step_owner = agent_acts.step_owners(
        connection, project_id,
        [(str(n.get("run_id")), str(n.get("step_id"))) for n in needs_you
         if n.get("kind") == "failed_step"])
    build_owner: dict[str, Optional[str]] = {}
    wanted = {str(n.get("decision_id")): (str(n["build"].get("run_id")), str(n["build"].get("where")))
              for n in needs_you
              if n.get("kind") == "approval" and isinstance(n.get("build"), dict)
              and not owners.get(str(n.get("decision_id")))}
    if wanted:
        from . import factory_view

        build_owner = factory_view.build_decision_owners(connection, project_id, wanted)
    out: list[AttentionItem] = []
    for n in needs_you:
        kind = str(n.get("kind"))
        item: AttentionItem = {
            "id": str(n.get("id")), "kind": kind,  # type: ignore[typeddict-item]
            "decision_kind": n.get("decision_kind"),
            "decision_id": n.get("decision_id"),
            "title": str(n.get("title") or ""),
            "detail": str(n.get("detail") or ""),
            "what_to_do": n.get("what_to_do"),
            "role_key": None,
            "run": n.get("run"),
            "actions": [],
            "answers": [],
            "decide_inline": False,
            "content_hash": None,
            "preview": None,
            "risk_tier": n.get("risk_tier"),
            # Lands on the Repo and models row that needs it (`?area=setup&focus=<row>`).
            "fix": ({"area": str(n["fix_area"]), "label": "Fix in Repo and models",
                     **({"focus": str(n["fix_focus"])} if n.get("fix_focus") else {})}
                    if n.get("fix_area") else None),
            "at": n.get("at"),
            "build": n.get("build"),
            "source": n.get("source"),
        }
        if kind == "approval":
            decision = by_id.get(str(n.get("decision_id"))) or {}
            actions = [{"action": str(a.get("action")), "label": str(a.get("label") or ""),
                        "tone": str(a.get("tone") or ""), "effect": str(a.get("effect") or ""),
                        "needs": _needs(a)} for a in n.get("allowed_actions") or []]
            preview = _rec(decision).get("action_preview")
            preview = preview if isinstance(preview, dict) else {}
            content_hash = preview.get("content_hash") or None
            item["actions"] = actions  # type: ignore[typeddict-item]
            item["content_hash"] = content_hash
            item["preview"] = _preview_line(preview)
            item["role_key"] = (owners.get(str(n.get("decision_id")))
                                or build_owner.get(str(n.get("decision_id"))))
            item["decide_inline"] = decide_inline(
                n.get("decision_kind"), n.get("risk_tier"), actions, bool(preview),
                content_hash)
        elif kind in ("gate_override", "budget_refused"):
            # A build stopped for the operator (factory spec §6.5): the reviewer whose gate
            # failed, from the build's acts — or nobody (a refused budget is BOSS's).
            item["role_key"] = n.get("role_key")
            item["answers"] = [
                {"key": a.get("key"), "label": a.get("label"),
                 "enabled": a.get("state", "enabled") == "enabled",
                 **({"reason": a["reason"]} if a.get("reason") else {})}
                for a in n.get("answers") or []]
        elif kind == "system_health":
            # The machine (stability spec §4d): Shepherd's card, BOSS's face. Each option is
            # the server's — its label, its tooltip, whether it asks to confirm.
            item["role_key"] = None
            item["finding_id"] = n.get("finding_id")  # type: ignore[typeddict-unknown-key]
            item["severity"] = n.get("severity")  # type: ignore[typeddict-unknown-key]
            item["answers"] = [
                {"key": a.get("key"), "label": a.get("label"), "enabled": True,
                 **({"reason": a["reason"]} if a.get("reason") else {}),
                 **({"confirm": True} if a.get("confirm") else {})}
                for a in n.get("answers") or []]
        elif kind == "failed_step":
            item["role_key"] = step_owner.get((str(n.get("run_id")), str(n.get("step_id"))))
            item["answers"] = [
                {"key": a.get("key"), "label": a.get("label"),
                 "enabled": a.get("state", "enabled") == "enabled",
                 **({"reason": a["reason"]} if a.get("reason") else {}),
                 "step_id": a.get("step_id") or n.get("step_id")}
                for a in n.get("answers") or []]
        out.append(item)
    return out


def enrich(connection: Any, project_id: str, cc: dict[str, Any],
           read: Mapping[str, Any]) -> dict[str, Any]:
    """The Command Center's cards and running rows, attributed the way Live's are.

    Each `needs_you` item gains `role_key`, `decide_inline`, `content_hash`, `preview` and the
    full `allowed_actions` ({action, label, tone, effect, needs: [...]}) — `actions` stays the
    list of names older readers use — and each `running` row gains `verb` (work_verb of its
    newest open work) and `role_key`. In place; returns `cc`. Never fails the page."""
    from . import agent_acts

    try:
        with connection.begin_nested():
            items = attention_items(connection, project_id, cc.get("needs_you") or [],
                                    read.get("decisions") or [])
            for item, attributed in zip(cc.get("needs_you") or [], items):
                item["role_key"] = attributed.get("role_key")
                item["decide_inline"] = bool(attributed.get("decide_inline"))
                item["content_hash"] = attributed.get("content_hash")
                item["preview"] = attributed.get("preview")
                if item.get("kind") == "approval":
                    item["allowed_actions"] = attributed.get("actions") or []
            running = cc.get("running") or []
            ids = [str(r["run_id"]) for r in running]
            newest: dict[str, dict[str, Any]] = {}
            if ids:
                for row in connection.execute(
                    text(
                        """
                        SELECT DISTINCT ON (workflow_run_id) *
                        FROM agent_acts
                        WHERE project_id = :p AND workflow_run_id = ANY(:ids)
                          AND ended_at IS NULL AND kind NOT IN ('run', 'approval')
                        ORDER BY workflow_run_id, started_at DESC
                        """
                    ),
                    {"p": project_id, "ids": ids},
                ).mappings().all():
                    newest[str(row["workflow_run_id"])] = dict(row)
            verbs = agent_acts.verbs_for(connection, list(newest.values()))
            waiting_you = {str(a.get("id")) for a in items}
            for r in running:
                act = newest.get(str(r["run_id"]))
                status = str(r.get("status") or "")
                step = r.get("step_status")
                if status == "blocked":
                    r["verb"] = "Blocked"
                elif act is not None:
                    r["verb"] = verbs.get(str(act["act_id"])) or agent_acts.work_verb(act)
                elif step == "waiting":
                    r["verb"] = "Waiting"
                else:
                    r["verb"] = "Working"
                if r["verb"] == "Needs you" and not any(
                        i.get("run", {}) and (i.get("run") or {}).get("id") == r["run_id"]
                        and i["id"] in waiting_you for i in items):
                    r["verb"] = "Waiting"
                r["role_key"] = (act or {}).get("role_key") or (r.get("agent") or {}).get(
                    "role_key")
    except Exception:  # noqa: BLE001 — attribution is never worth a blank landing page
        import logging

        logging.getLogger("boss.agents_live_board").warning(
            "could not attribute the Command Center", exc_info=True)
    return cc


# ── missions: one row per run on Live ────────────────────────────────────────────────────────


def _person_of(act: Mapping[str, Any]) -> Optional[str]:
    if act.get("kind") in ("run", "approval") and act.get("handed_by_kind") == "person":
        return act.get("handed_by_id") or act.get("accountable")
    return act.get("accountable") or act.get("handed_by_id")


def act_state(act: Mapping[str, Any]) -> str:
    """chain-model.ts actState: what an act's status looks like on a face."""
    status = str(act.get("status") or "")
    if act.get("kind") == "run":
        return "done" if status == "complete" else "idle"  # the starter is never stuck
    if act.get("kind") == "approval":
        return "done"
    return {"active": "speaking" if act.get("kind") == "seat" else "working",
            "awaiting_input": "needs_you", "complete": "done", "error": "failed",
            "stale": "stopped"}.get(status, "idle")


def _short(label: Any) -> str:
    text_value = str(label or "")
    return (text_value.split(" · ")[0] or text_value).strip()


def lane(acts: list[dict[str, Any]], *, viewer: Optional[str] = None) -> list[Hop]:
    """A run's chain as Live's lane: depth-0 hops, folded the way chain-model.ts folds.

    The root (whoever started it) first; then the work that stands on its own, in start order,
    each with how much of its own inner work (stages, seats, a sub-run) folds under it; a
    back-and-forth between two agents on the same work said once as a loop; and a lane longer
    than LANE_MAX folded to its first LANE_HEAD hops, `+n`, and its last LANE_TAIL."""
    from .agent_acts import _label_for

    if not acts:
        return []
    by_id = {a["act_id"]: a for a in acts}
    root = next((a for a in acts if a.get("kind") == "run"
                 and (not a.get("parent_act_id") or a["parent_act_id"] not in by_id)), None) \
        or next((a for a in acts if not a.get("parent_act_id")
                 or a["parent_act_id"] not in by_id), acts[0])
    children: dict[str, list[dict[str, Any]]] = {}
    for a in acts:
        if a is root or not a.get("parent_act_id"):
            continue
        children.setdefault(a["parent_act_id"], []).append(a)

    def nested(a: Mapping[str, Any]) -> bool:
        parent_id = a.get("parent_act_id")
        if not parent_id or parent_id == root["act_id"]:
            return False
        parent = by_id.get(parent_id)
        if parent is None:
            return False
        if parent.get("kind") in ("stage", "seat"):
            return True
        return a.get("kind") in ("stage", "seat", "run", "chat") and parent.get("kind") != "run"

    def inside(act_id: str) -> int:
        count, todo, seen = 0, [act_id], {act_id}
        while todo:
            for child in children.get(todo.pop(), []):
                if nested(child) and child["act_id"] not in seen:
                    seen.add(child["act_id"])
                    count += 1
                    todo.append(child["act_id"])
        return count

    def hop(a: Mapping[str, Any]) -> Hop:
        out: Hop = {"act_id": a["act_id"], "role_key": a.get("role_key"),
                    "state": act_state(a), "label": _short(a.get("label")),
                    "edge_label": _label_for(dict(a), viewer), "how": str(a.get("how") or "")}
        if not a.get("role_key"):
            out["person"] = _person_of(a)
            if a.get("kind") == "step" and (a.get("status") == "awaiting_input"
                                            or a.get("decision_id")
                                            or (a.get("links") or {}).get("decision_id")):
                # A gate waiting on a person: a real act, flagged so the lane draws it as the
                # amber `You` hop rather than adding one of its own.
                out["gate"] = True
        n = inside(a["act_id"])
        if n:
            out["inside"] = n
        return out

    # CAUSAL ORDER, as agent_acts.chain walks it: each act after the act that handed it the
    # work. A gate is never claimed, so its start is when its row was made — often before the
    # step that released it — and a clock order put the gate ahead of the work it holds.
    def started(a: Mapping[str, Any]) -> tuple:
        return (_aware(a.get("started_at")) or datetime.max.replace(tzinfo=timezone.utc),
                a["act_id"])

    walk: dict[str, int] = {}
    stack = [root]
    while stack:  # depth first, iteratively: a long loop of rounds is a deep chain
        a = stack.pop()
        if a["act_id"] in walk:
            continue
        walk[a["act_id"]] = len(walk)
        stack.extend(sorted(children.get(a["act_id"], []), key=started, reverse=True))
    main = sorted((a for a in acts if a is not root and not nested(a)),
                  key=lambda a: (walk.get(a["act_id"], len(walk)), started(a)))
    first = hop(root)
    if root.get("kind") == "run":
        first["label"] = "Started"
    items: list[Hop] = [hop(a) for a in main]

    def key(h: Hop) -> str:
        return str(h.get("role_key") or f"person:{h.get('person') or ''}")

    def work(h: Hop, a: Mapping[str, Any]) -> str:
        return str(a.get("step_id") or h.get("label") or "")

    works = [work(h, a) for h, a in zip(items, main)]
    collapsed: list[Hop] = []
    i = 0
    while i < len(items):
        x = items[i]
        y = items[i + 1] if i + 1 < len(items) else None
        if y is not None and key(x) != key(y):
            j = i + 2
            while j < len(items) and key(items[j]) == key(items[j - 2]) \
                    and works[j] == works[j - 2]:
                j += 1
            if j - i >= 4:
                collapsed.append({"loop": {"a": x.get("role_key") or key(x),
                                           "b": y.get("role_key") or key(y),
                                           "rounds": (j - i + 1) // 2}})
                i = j
                continue
        collapsed.append(x)
        i += 1
    whole = [first, *collapsed]
    if len(whole) > LANE_MAX:
        hidden = len(whole) - LANE_HEAD - LANE_TAIL
        whole = [*whole[:LANE_HEAD], {"fold": {"hidden": hidden}}, *whole[-LANE_TAIL:]]
    return whole


def _chains(connection: Any, project_id: str,
            run_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Every act of these runs' chains, in one read: {run_id: [act rows, by start]}."""
    if not run_ids:
        return {}
    rows = [dict(r) for r in connection.execute(
        text(
            """
            WITH roots AS (
                SELECT DISTINCT ON (workflow_run_id) workflow_run_id, root_act_id
                FROM agent_acts
                WHERE project_id = :p AND kind = 'run' AND workflow_run_id = ANY(:ids)
                ORDER BY workflow_run_id, started_at
            )
            SELECT roots.workflow_run_id AS chain_of, a.*
            FROM roots JOIN agent_acts a ON a.root_act_id = roots.root_act_id
            ORDER BY a.started_at, a.act_id
            """
        ),
        {"p": project_id, "ids": run_ids},
    ).mappings().all()]
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(str(row.pop("chain_of")), []).append(row)
    return out


def _next_steps(spec_steps: Mapping[str, Mapping[str, Any]], started: set[str],
                names_role: Any) -> list[NextHop]:
    """Who the workflow names for the steps not started yet — the spec's captains, nothing
    guessed. An agent step BOSS will pick for is `Auto` (role_key None)."""
    from . import command_center

    out: list[NextHop] = []
    for sid, step in spec_steps.items():
        if sid in started or len(out) >= NEXT_MAX:
            continue
        kind = str(step.get("kind") or "capability")
        if kind not in ("capability", "subworkflow"):
            continue
        binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
        named = str(step.get("captain") or binding.get("agent") or "").strip().lower()
        label = command_center.step_label(step, sid)
        if named and named != "human":
            out.append({"role_key": names_role(named), "step_label": label, "named": True})
        elif binding.get("agent_work"):
            out.append({"role_key": None, "step_label": label, "named": False})
    return out


_SORT = {"stopped": 0, "waiting_you": 1, "blocked": 2, "running": 3, "waiting_person": 3,
         "waiting": 3, "stalled": 4, "done": 5}

#: A live run with no progress for this long is STALLED, not running: on an existing install eight runs
#: "running" for a month made the page say "8 running" while nothing moved. The rule is here,
#: once; every count (running, stalled) and the state sentence read the state it sets.
STALLED_AFTER = timedelta(days=7)
#: The states a stall can turn into "stalled". A gate waiting on YOU is your wait, never a
#: stall; a stopped run already says it stopped.
STALLABLE = frozenset({"running", "blocked", "waiting", "waiting_person"})


def last_progress(run: Mapping[str, Any], steps: list[Mapping[str, Any]]) -> Optional[datetime]:
    """When the run last moved: the newest of its steps' completed/started/updated times, else
    when it was created. Never the run row's own updated_at, which bookkeeping touches."""
    times = [t for s in steps for t in (_aware(s.get("completed_at")), _aware(s.get("started_at")),
                                         _aware(s.get("updated_at"))) if t]
    return max(times) if times else _aware(run.get("created_at"))


def missions(connection: Any, project_id: str, read: Mapping[str, Any],
             attention: list[AttentionItem], *, now: datetime,
             viewer: Optional[str] = None) -> list[Mission]:
    """One row per run on Live: active, stopped and compensating runs, and runs completed in
    the last JUST_FINISHED. From the rows command_center.read already holds; chains in one read."""
    from . import agent_acts, command_center, workflows
    from .workflow_run_actions import standing

    by_run: dict[str, list[Mapping[str, Any]]] = {}
    for step in read["steps"]:
        by_run.setdefault(str(step.get("workflow_run_id")), []).append(step)
    attention_ids = {str(a.get("id")) for a in attention}
    rows: list[Mission] = []
    for run in read["runs"]:
        status = str(run.get("status") or "")
        place = standing(status)
        finished = _aware(run.get("completed_at")) or _aware(run.get("updated_at"))
        just_done = status == "completed" and finished is not None \
            and now - finished <= JUST_FINISHED
        if place == "closed" and not just_done:
            continue
        spec = command_center._spec_steps(run)
        order = {sid: i for i, sid in enumerate(spec)}
        run_steps = sorted(command_center._latest_per_step(by_run.get(str(run["id"]), [])),
                           key=lambda s: order.get(str(s.get("step_id")), len(order)))
        done = sum(1 for s in run_steps if s.get("status") in ("succeeded", "skipped"))
        count = max(len(spec), len(run_steps))
        current = next((s for s in run_steps if s.get("status") in command_center.CURRENT_STEP),
                       None)
        failed = next((s for s in reversed(run_steps)
                       if s.get("status") in command_center.FAILED_STEP), None)
        here = current or (failed if place == "stopped" else None)
        spec_step = spec.get(str(here.get("step_id")), {}) if here else {}
        output = here.get("output") if here and isinstance(here.get("output"), dict) else {}
        approval = here.get("approval") if here and isinstance(here.get("approval"), dict) else {}
        state = "running"
        waiting_on = waiting_role = None
        attention_id = None
        if just_done:
            state = "done"
        elif place == "stopped":
            state = "stopped"
            if failed is not None:
                fid = f"run:{run['id']}:{failed.get('step_id')}"
                attention_id = fid if fid in attention_ids else None
        elif status == "blocked":
            state = "blocked"
        elif current is not None and current.get("status") == "waiting":
            decision_id = str(current.get("decision_id") or "")
            if decision_id and decision_id in attention_ids:
                state, attention_id = "waiting_you", decision_id
            elif decision_id and str(current.get("decision_status") or "") in (
                    "approved", "executed"):
                state = "running"  # answered; a worker picks the answer up next tick
            elif decision_id and str(current.get("decision_status") or "") in (
                    "rejected", "expired", "cancelled"):
                # Answered NO: never "waiting on a person" (agents-home D3). The step fails as
                # the answer lands; until this read sees it, the run is stopped there.
                state = "stopped"
            elif decision_id:
                state = "waiting_person"
                waiting_role = str(approval.get("approver_role") or "") or None
                waiting_on = waiting_role or str(current.get("decision_status") or "") or None
            else:
                state = "waiting"
                waiting_on = workflows.waiting_on_label({
                    "approval": approval, "decision_id": None, "output": output})
        moved = last_progress(run, by_run.get(str(run["id"]), []))
        if state in STALLABLE and moved is not None and now - moved > STALLED_AFTER:
            state = "stalled"
        step_since = (_aware(here.get("started_at")) or _aware(here.get("updated_at"))
                      if here else None)
        created = _aware(run.get("created_at"))
        rows.append({
            "run_id": str(run["id"]),
            "subject": command_center.run_name(run),
            "workflow_key": str(run.get("workflow_key") or ""),
            "workflow_name": str(run.get("workflow_name") or ""),
            "version": run.get("version"),
            "actor": run.get("actor"),
            "state": state,  # type: ignore[typeddict-item]
            "standing": place,
            "step_index": command_center.step_index(done, count, status),
            "step_count": count,
            "step_label": (command_center.step_label(spec_step, str(here.get("step_id")))
                           if here else None),
            "verb": "Working",
            "waiting_on": waiting_on,
            "waiting_on_role": waiting_role,
            "blocked_reason": command_center._first_line(run.get("blocked_reason")) or None,
            "error": command_center._first_line(run.get("error")) or None,
            "attention_id": attention_id,
            "started_at": _iso(created),
            "step_since": _iso(step_since),
            "elapsed_seconds": int(((finished if just_done else now) - created).total_seconds())
            if created else None,
            "step_elapsed_seconds": int((now - step_since).total_seconds())
            if step_since and not just_done else None,
            "last_progress_at": _iso(moved),
            "cost_usd": 0.0,
            "hops": [],
            "next": _next_steps(spec, {str(s.get("step_id")) for s in run_steps},
                                lambda k: agent_acts.resolve_role(k) or k),
            "finished_at": _iso(finished) if just_done else None,
            "_capability": str(((spec_step.get("binding") or {}) if isinstance(
                spec_step.get("binding"), dict) else {}).get("capability")
                or spec_step.get("capability") or ""),
            "_current_kind": str((here or {}).get("step_kind") or ""),
        })

    def last_hop_at(m: Mapping[str, Any]) -> str:
        return str(m.get("step_since") or m.get("started_at") or "")

    # Waiting on you, stopped, blocked, running (most recent hop first), just finished.
    rows.sort(key=last_hop_at, reverse=True)
    rows.sort(key=lambda m: _SORT.get(str(m["state"]), 3))
    drawn = [m["run_id"] for m in rows[:CHAIN_CAP]]
    chains = _chains(connection, project_id, drawn)
    open_ids = [a["act_id"] for acts in chains.values() for a in acts if a.get("ended_at") is None]
    live_cost = agent_acts._live_cost(connection, open_ids)
    all_acts = [a for acts in chains.values() for a in acts]
    verbs = agent_acts.verbs_for(connection, [a for a in all_acts if a.get("ended_at") is None])
    for m in rows:
        cap = m.pop("_capability", "")  # type: ignore[misc]
        m.pop("_current_kind", None)  # type: ignore[misc]
        acts = chains.get(m["run_id"])
        if m["run_id"] not in drawn:
            m["folded"] = True  # past CHAIN_CAP: its words, not its faces
        elif acts:
            m["hops"] = lane(acts, viewer=viewer)
            m["cost_usd"] = round(sum(
                float(a.get("cost_usd") or 0) + (live_cost.get(a["act_id"], (0, 0, 0.0))[2]
                                                 if a.get("ended_at") is None else 0)
                for a in acts), 6)
        m["verb"] = _mission_verb(m, acts or [], verbs, cap)
    return rows


def _mission_verb(m: Mapping[str, Any], acts: list[dict[str, Any]], verbs: Mapping[str, str],
                  capability: str) -> str:
    """The run's one word now, from agent_acts.work_verb: the newest open work in its chain
    (a build's stage says Coding, not its step's Working), else its step's capability."""
    from .agent_acts import work_verb

    state = m["state"]
    if state == "done":
        return "Finished"
    if state == "stopped":
        # A run that stopped at a failed step FAILED (the row says `Failed · step N`).
        return "Failed"
    if state == "blocked":
        return "Blocked"
    if state == "stalled":
        return "Stalled"
    if state == "waiting_you":
        return "Needs you"
    if state in ("waiting", "waiting_person"):
        return "Waiting"
    open_work = [a for a in acts if a.get("ended_at") is None
                 and a.get("kind") not in ("run", "approval")]
    if open_work:
        newest = max(open_work, key=lambda a: (_aware(a.get("started_at"))
                                               or datetime.min.replace(tzinfo=timezone.utc)))
        return verbs.get(newest["act_id"]) or work_verb(newest)
    return work_verb({"kind": "step", "status": "active"}, capability=capability or None)


# ── outcomes ─────────────────────────────────────────────────────────────────────────────────

_PR_NUMBER = re.compile(r"(?:#|/pull/)(\d+)")


def outcomes(connection: Any, project_id: str, read: Mapping[str, Any], *,
             now: datetime) -> list[Outcome]:
    """What got done in the last day: pull requests, reports and minutes the agents' work
    carries as artifacts, and runs that completed. Newest first, at most OUTCOMES_MAX.

    `PR #n merged` only when the merge queue recorded the merge (github_merge_authorizations,
    status merged) — never inferred from the PR being old or the run finishing."""
    from .command_center import run_name

    since = now - OUTCOME_WINDOW
    rows = connection.execute(
        text(
            """
            SELECT act_id, role_key, artifacts, workflow_run_id, label, ended_at
            FROM agent_acts
            WHERE project_id = :p AND ended_at > :since
              AND jsonb_typeof(artifacts) = 'array' AND jsonb_array_length(artifacts) > 0
            ORDER BY ended_at DESC
            LIMIT 60
            """
        ),
        {"p": project_id, "since": since},
    ).mappings().all()
    found: list[Outcome] = []
    seen: set[tuple[str, str]] = set()
    pr_urls: list[str] = []
    for r in rows:
        artifacts = r["artifacts"] if isinstance(r["artifacts"], list) else []
        for a in artifacts:
            if not isinstance(a, dict):
                continue
            kind = str(a.get("kind") or "")
            if kind not in ("pr", "report", "minutes"):
                continue
            ref = str(a.get("ref") or "")
            url = str(a.get("url") or "") or None
            dedupe = (kind, url or ref)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            if kind == "pr":
                number = _PR_NUMBER.search(ref) or _PR_NUMBER.search(url or "")
                words = f"PR #{number.group(1)} opened" if number else "PR opened"
                if url:
                    pr_urls.append(url)
            else:
                words = "Report ready" if kind == "report" else "Minutes ready"
            found.append({"act_id": r["act_id"], "role_key": r["role_key"], "kind": kind,
                          "text": words, "url": url if url and url.startswith("http") else None,
                          "run_id": r["workflow_run_id"], "label": r["label"],
                          "at": _iso(r["ended_at"]) or ""})
    if pr_urls:
        merged = {str(m) for m in connection.execute(
            text("SELECT pull_request_url FROM github_merge_authorizations "
                 "WHERE project_id = :p AND status = 'merged' AND pull_request_url = ANY(:u)"),
            {"p": project_id, "u": pr_urls},
        ).scalars().all()}
        for item in found:
            if item["kind"] == "pr" and item.get("url") in merged:
                item["kind"] = "pr_merged"
                item["text"] = item["text"].replace(" opened", " merged")
    for run in read["runs"]:
        finished = _aware(run.get("completed_at"))
        if str(run.get("status")) != "completed" or finished is None or finished < since:
            continue
        found.append({"act_id": None, "role_key": None, "kind": "run", "text": "Finished",
                      "url": None, "run_id": str(run["id"]),
                      "label": run_name(run),
                      "at": _iso(finished) or ""})
    found.sort(key=lambda o: str(o.get("at") or ""), reverse=True)
    return found[:OUTCOMES_MAX]


# ── teams and agents ─────────────────────────────────────────────────────────────────────────


def force(connection: Any, project_id: str, attention: list[AttentionItem]
          ) -> tuple[list[Team], list[BoardAgent]]:
    """Every non-retired agent in its team, with its state: presence (open work) first, then a
    failed step it is attributed (Failed), then its lifecycle (Paused), else Idle."""
    from . import agent_acts
    from .agent_identity import TEAMS, team_of
    from .agent_persona import persona_of

    roles = connection.execute(
        text("SELECT role_key, display_name, spec, lifecycle FROM roles "
             "WHERE project_id = :p AND role_key <> 'human' ORDER BY role_key"),
        {"p": project_id},
    ).mappings().all()
    presence = agent_acts.quietly(connection, "read presence", agent_acts.presence_map,
                                  project_id) or {}
    stuck = {str(a.get("role_key")): a for a in attention
             if a.get("kind") == "failed_step" and a.get("role_key")}
    agents: list[BoardAgent] = []
    known: set[str] = set()
    for r in roles:
        key = str(r["role_key"])
        known.add(key)
        if str(r["lifecycle"] or "active") == "retired":
            continue
        named = persona_of({"role_key": key, "spec": r["spec"]})
        agents.append(_agent(key, str(r["display_name"] or key), named.get("callsign"),
                             team_of(r["spec"])["key"], presence.get(key), stuck.get(key),
                             str(r["lifecycle"] or "active")))
    # UNREGISTERED NAMES DOING WORK are on the floor too — in Yours, where an operator can see
    # them and register them. Never hidden because nothing governs them.
    for key, here in sorted(presence.items()):
        if key in known or key == "human":
            continue
        agents.append(_agent(key, key.replace("_", " ").capitalize(), None, "yours", here,
                             stuck.get(key), "active"))
    asks = {str(r["role_key"]): int(r["n"]) for r in connection.execute(
        text("SELECT role_key, count(*) AS n FROM agent_acts WHERE project_id = :p "
             "AND ended_at IS NULL AND status = 'awaiting_input' AND role_key IS NOT NULL "
             "AND NOT (kind = 'step' AND decision_id IS NOT NULL) "
             "GROUP BY role_key"),
        {"p": project_id},
    ).mappings().all()}
    order = [k for k, _, _ in TEAMS]
    labels = {k: (label, tip) for k, label, tip in TEAMS}
    custom = sorted({a["team"] for a in agents} - set(order))
    teams: list[Team] = []
    for key in [*order[:-1], *custom, order[-1]]:
        members = [a for a in agents if a["team"] == key]
        if key in labels:
            label, tip = labels[key]
        else:
            resolved = team_of({"team": key})
            label, tip = resolved["label"], None
            # The label as an operator wrote it, from the first member whose spec says so.
            for r in roles:
                spec = r["spec"] if isinstance(r["spec"], dict) else {}
                if team_of(spec)["key"] == key:
                    label = team_of(spec)["label"]
                    break
        team: Team = {"key": key, "label": label, "members": [a["role_key"] for a in members],
                      "working": sum(1 for a in members if a["state"] in ("working", "in_panel")),
                      # ITEMS, not agents (D8): this team's agents' OWN open asks. A gate is
                      # the operator's wait, never a team's (D4 + the lead's follow-up).
                      "needs_you": sum(asks.get(m["role_key"], 0) for m in members),
                      "in_panel": sum(1 for a in members if a["state"] == "in_panel")}
        if tip:
            team["tip"] = tip
        teams.append(team)
    return teams, agents


def mission_workers(missions: list[Mapping[str, Any]]) -> set[str]:
    """Agents on the current hop of a running mission, when that hop is working. Pure."""
    out: set[str] = set()
    for m in missions:
        if m.get("state") != "running":
            continue
        for h in reversed(m.get("hops") or []):
            if h.get("role_key"):
                if h.get("state") in ("working", "thinking", "speaking"):
                    out.add(str(h["role_key"]))
                break
    return out


def _agent(key: str, name: str, callsign: Any, team: str, here: Optional[Mapping[str, Any]],
           stuck: Optional[Mapping[str, Any]], lifecycle: str) -> BoardAgent:
    state = str((here or {}).get("state") or "idle")
    line = str((here or {}).get("line") or "")
    since = (here or {}).get("since")
    if state == "idle" and stuck is not None:
        # A failed step attributed to this agent: it FAILED, it is not stuck.
        state = "failed"
        line = f"Failed: {stuck.get('title') or ''}".rstrip(": ")
        since = stuck.get("at")
    elif state == "idle" and lifecycle == "paused":
        state = "paused"
    out: BoardAgent = {"role_key": key, "name": name, "team": team, "state": state,
                       "line": line or ("No work yet" if state == "idle" else "")}
    if callsign:
        out["callsign"] = str(callsign)
    if since:
        out["since"] = since
    return out


# ── the board ────────────────────────────────────────────────────────────────────────────────


def etag_of(board: Mapping[str, Any]) -> str:
    """Over what the page shows changing — ids, states, steps, counts — and not over time: the
    web ticks elapsed time from `step_since` itself, so a clock never costs a re-render."""
    material = {
        "attention": [(a.get("id"), a.get("decide_inline"), a.get("title")) for a in
                      board["attention"]],
        "missions": [(m["run_id"], m["state"], m["step_index"], m.get("step_count"),
                      m.get("verb"), m.get("cost_usd"),
                      [(h.get("act_id"), h.get("state"), h.get("inside")) if "act_id" in h
                       else json.dumps(h, sort_keys=True) for h in m.get("hops") or []])
                     for m in board["missions"]],
        "outcomes": [(o.get("kind"), o.get("act_id"), o.get("run_id"), o.get("text"))
                     for o in board["outcomes"]],
        "teams": [(t["key"], len(t["members"]), t["working"], t["needs_you"], t["in_panel"])
                  for t in board["teams"]],
        "agents": [(a["role_key"], a["state"], a.get("team")) for a in board["agents"]],
        "builds": [(b["run_id"], b["state"], b["word"], b["k"], b["n"],
                    [(s["key"], s["status"], s.get("role_key")) for s in b["stations"]])
                   for b in board.get("builds") or []],
        "counts": board["counts"],
    }
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode())
    return f'"lb-{digest.hexdigest()[:24]}"'


def compose(connection: Any, settings: Any, project_id: str, *,
            viewer: Optional[str] = None, now: Optional[datetime] = None,
            with_builds: bool = True, setup_out: Optional[dict[str, Any]] = None,
            ) -> tuple[LiveBoard, dict[str, Any], dict[str, Any]]:
    """The board, and the Command Center's own `assemble` output and read it was built from.

    Extracted from `build` with no change of behaviour (command-center spec §6.1): the Command
    Center view composes this board rather than restating it, and needs `cc` (recent runs, the
    expired-answerable count) and `read` (the run rows) without assembling them twice. The
    Command Center draws no builds, so it passes `with_builds=False` and skips
    factory_view.building_rows; `counts.building` is then 0 and `builds` empty. `setup_out`
    receives the Repo-and-models basics view the setup read already built."""
    from . import command_center

    now = now or datetime.now(timezone.utc)
    read = command_center.read(connection, settings, project_id, now, setup_out=setup_out)
    cc = command_center.assemble(**read)
    attention = attention_items(connection, project_id, cc["needs_you"], read["decisions"])
    rows = missions(connection, project_id, read, attention, now=now, viewer=viewer)
    # THE BUILDS ON THE FLOOR ARE THE BUILDS HERE (factory spec §10, decision 2): the same rows
    # factory_view draws, so Agents › Live and the Factory say the same builds and counts.
    builds: list[dict[str, Any]] = []
    if with_builds:
        try:
            from . import factory_view

            with connection.begin_nested():
                builds = [dict(b) for b in factory_view.building_rows(
                    connection, project_id, attention, viewer=viewer, now=now)]
        except Exception:  # noqa: BLE001 — the builds are never worth a blank Live tab
            import logging

            logging.getLogger("boss.agents_live_board").warning(
                "could not read the building builds", exc_info=True)
    teams, agents = force(connection, project_id, attention)
    # ONE SOURCE FOR "WORKING" (independent re-check R6): an agent on a running mission's
    # current working hop is working, whatever its presence row says this tick — so the team
    # tiles, the pill and In flight count the same agents.
    on_step = mission_workers(rows)
    for a in agents:
        if a["role_key"] in on_step and a["state"] in ("idle", "done"):
            a["state"] = "working"
    for t in teams:
        members = set(t["members"])
        t["working"] = sum(1 for a in agents if a["role_key"] in members
                           and a["state"] in ("working", "in_panel"))
    board: LiveBoard = {
        "project_id": project_id,
        "etag": "",
        "generated_at": now.isoformat(),
        "counts": {
            "agents": len(agents),
            "working": sum(1 for a in agents if a["state"] in ("working", "in_panel")),
            # THE SAME NUMBER as the Command Center's counts.needs_you, the pill's attention
            # count, Approvals' badge and Needs action: decisions only (agents-home D2).
            "needs_you": int(cc["counts"]["needs_you"]),
            "setup": int(cc["counts"]["setup"]),
            # RUNNING IS WHAT MOVES: a run with no progress for a week is `stalled`, counted
            # apart, never "running" (an existing install said "8 running" for a month of nothing).
            "running": sum(1 for m in rows if m["state"] not in ("done", "stopped", "stalled")),
            "stalled": sum(1 for m in rows if m["state"] == "stalled"),
            # What NEEDS YOU lists as stopped work, counted by the one rule (attention_counts).
            "stopped": command_center.attention_counts(attention)["stopped"],
            "building": len(builds),
        },
        "attention": attention,
        "missions": rows,
        "builds": builds,
        "outcomes": outcomes(connection, project_id, read, now=now),
        "teams": teams,
        "agents": agents,
    }
    board["etag"] = etag_of(board)
    return board, cc, read


def build(connection: Any, settings: Any, project_id: str, *,
          viewer: Optional[str] = None, now: Optional[datetime] = None) -> LiveBoard:
    """The whole Live tab in one read (spec §6.2). Composition only; see the module doc."""
    board, _cc, _read = compose(connection, settings, project_id, viewer=viewer, now=now,
                                with_builds=True)
    return board


def pill(connection: Any, settings: Any, project_id: str, *,
         now: Optional[datetime] = None) -> dict[str, Any]:
    """The top-bar pill's slice (spec §6.3): how many things need the operator — the Command
    Center's counts.needs_you, from the same list — and the first five, attributed."""
    from . import command_center

    now = now or datetime.now(timezone.utc)
    read = command_center.read(connection, settings, project_id, now)
    cc = command_center.assemble(**read)
    # The pill counts DECISIONS, so it lists decisions (agents-home D2): a stopped run and a
    # setup gap have their own places (Live's cards, the Setup rail).
    decisions = [n for n in cc["needs_you"] if n.get("kind") == "approval"]
    items = attention_items(connection, project_id, decisions[:5], read["decisions"])
    # WHAT ONLY A PERSON CAN SET UP on this project (project_experience.needs.setup_cards:
    # "Pick a prod host", onboarding's only-you items) counts too: the Project page's Needs you
    # bar lists them, and the bell said 2 beside a bar that said 3.
    try:
        from .project_experience.needs import setup_cards

        with connection.begin_nested():
            setup = setup_cards(connection, project_id)
    except Exception:  # noqa: BLE001 — the pill never fails over its setup line
        setup = []
    out = [{"id": a["id"], "role_key": a.get("role_key"), "title": a.get("title"),
            "at": a.get("at"), "decide_inline": bool(a.get("decide_inline"))} for a in items]
    out += [{"id": c["id"], "role_key": None, "title": c["label"], "at": None,
             "decide_inline": False, "kind": "setup", "href": c["action"]["href"]}
            for c in setup][: max(0, 5 - len(out))]
    return {"count": int(cc["counts"]["needs_you"]) + len(setup), "decisions":
            int(cc["counts"]["needs_you"]), "setup": len(setup), "items": out}
