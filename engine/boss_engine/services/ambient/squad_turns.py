"""Agent turns on the board: how a squad actually works an item together (BOSSAPP-908; design
§6, §11.4, §13).

ONE AGENT PATH (BOSSAPP-759; the operator's standing rule)
----------------------------------------------------------
A squad member's turn is NOT a second agent engine. It is `AgentService.decide` — the one
pipeline every domain agent's turn goes through (the same model routing, Jev when ready,
confidence bar, governed spend, the turn kept on the record's own thread, learning written back)
— with the board's moves as the candidates:

    finding · hypothesis · proposal (lead) · critique · vote · hand_lead (lead) · escalate ·
    resolve (lead) · pass

The member reads, before it moves: the item, its seat and squad, the squad's MEMORY first (what
was tried and failed), the board as the member may see it (no herding: votes it has not cast
are hidden), the evidence it may cite, and — for the lead — what the autonomy gate allows for
each action. Its answer's arguments are checked by the caller's `check` (evidence exists, the
enum is right, the content is not a repeat) with one re-ask, then posted through `collab.post`.
The agent's persona is its own role brief (agent_steps.agent_brief, the same compiler as its
work prompt); what it may DO beyond posting is the gate's and the ledger's call, never its own.

CONSENSUS IN PRACTICE (design §11.4)
------------------------------------
    proposal ──► critique (the critic of record, plus anyone) ──► votes when the risk asks ──►
    settle:  approved  → a ledger decision WITH the consensus record → the action executes
             rejected  → a 'revise' decision; the proposal goes to tried-and-failed memory
             needs a person / tie / no quorum / gate says person → Needs you, the squad's
                         recommendation and every dissent on the card
A proposal settles at the END of a round, so every member has had its turn on it — except a
low-risk `single` proposal, which the lead settles at once. Dissent is never dropped: it rides
on the decision's consensus record, on the Needs-you recommendation, into the handoff packet
and onto the diagram as a split arrow.

TIE-BREAK RULES
    1. Any `unsafe` critique sends it to a person (collab.tally).
    2. Maker-checker: one agreeing non-proposer approves; a single objection is kept as dissent;
       two objections go to a person (collab.tally). The proposer never checks itself.
    3. A squad vote that ties (approvals == rejections, quorum met) has no majority: it goes to
       a person with HOLD as the recommendation and both sides on the card.
    4. A maker-checker proposal nobody checked within two turns each goes to a person.
    5. A proposal the squad already rejected (same action, same words) cannot be made again.

LOOP GUARDS (design §6, §9 — in code, never in a prompt)
    a turn cap per item, a spend cap per item and the project's daily envelope, a proposal cap
    per item, duplicate posts refused, a round where every member passes ends the run, the
    project stop and a person's take-over end it at the next turn (the lead's lease is checked
    before every turn and every effect).
"""
from __future__ import annotations

import functools
import re
from typing import Any, Callable, Mapping, Optional, Union

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from . import asks, board, collab, ledger, policy, squad_memory, squad_plugins, squads
from . import task_classes as tc
from ._common import (EvidenceError, clean_text, emit, party, party_id, person,
                      verify_evidence)

#: Turns any squad may take on one item, ever. A squad that has not converged by then needs a
#: person, not more turns.
MAX_TURNS_PER_ITEM = 40
MAX_ROUNDS_PER_RUN = 4
MAX_PROPOSALS_PER_ITEM = 4
#: What a squad may spend on one item's turns (the project envelope still applies on top).
SQUAD_BUDGET_USD_MICROS = 1_500_000
BOARD_WINDOW = 30
#: What a lead may propose (the board's own moves — findings, questions — are message kinds).
PROPOSABLE = ("convene_meeting", "start_workflow", "open_draft_pr", "comment", "label", "nudge",
              "staging_rollback", "staging_restart", "silence_rule", "prod_rollback",
              "prod_restart", "quarantine_test", "merge", "deploy_prod")
DEFAULT_UNDO = {
    "convene_meeting": "close the panel", "start_workflow": "cancel the run",
    "open_draft_pr": "close the draft PR", "comment": "delete the comment",
    "label": "remove the label", "nudge": "withdraw the nudge",
    "staging_rollback": "roll staging forward", "staging_restart": "none needed",
    "silence_rule": "unsilence the rule", "prod_rollback": "roll production forward",
    "prod_restart": "none needed", "quarantine_test": "un-quarantine the test",
}
RISK_TIER = {"low": 1, "medium": 2, "high": 3}

Bind = Union[Engine, Connection]
#: A model for a member: one adapter for every member, or a function party -> adapter.
ModelFor = Union[None, Any, Callable[[str], Any]]


class TurnRefused(RuntimeError):
    """The squad may not take another turn on this item now (a guard, the stop, a take-over)."""

    def __init__(self, guard: str, reason: str):
        super().__init__(reason)
        self.guard, self.reason = guard, reason


# ── reading the board ────────────────────────────────────────────────────────────────────────

def _settled_ids(connection: Connection, item: Mapping[str, Any]) -> set[str]:
    """Proposals that a decision settled, or that went to a person."""
    decided = set(connection.execute(text(
        "SELECT proposal_id FROM agent_act_decisions WHERE work_item_id = :w "
        "AND proposal_id IS NOT NULL"), {"w": item["id"]}).scalars())
    asked = set(connection.execute(text(
        "SELECT recommendation->>'proposal_id' FROM decisions WHERE project_id = :p "
        "AND recommendation->>'work_item_id' = :w "
        "AND recommendation->>'proposal_id' IS NOT NULL"),
        {"p": item["project_id"], "w": item["id"]}).scalars())
    return decided | asked


def open_proposal(connection: Connection, item_id: str) -> Optional[dict[str, Any]]:
    """The one proposal the squad is working on (a lead keeps one open at a time)."""
    item = board.get(connection, item_id)
    settled = _settled_ids(connection, item)
    open_ = [m for m in collab.messages(connection, item_id, kinds=("proposal",))
             if m["id"] not in settled]
    return open_[-1] if open_ else None


def _replies(connection: Connection, proposal_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(text(
        "SELECT id, sender, kind, body FROM work_item_messages WHERE reply_to = :m "
        "AND kind IN ('critique', 'vote') ORDER BY created_at, id"),
        {"m": proposal_id}).mappings()]


def _turns(connection: Connection, item: Mapping[str, Any], *, since: Any = None,
           exclude: Optional[str] = None) -> int:
    return int(connection.execute(text(
        "SELECT count(*) FROM events WHERE project_id = :p AND event_type = 'ambient.squad_turn' "
        "AND entity_id = :w AND (CAST(:s AS TIMESTAMPTZ) IS NULL OR created_at > :s) "
        "AND (CAST(:x AS TEXT) IS NULL OR data->>'member' <> :x)"),
        {"p": item["project_id"], "w": item["id"], "s": since, "x": exclude}).scalar() or 0)


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _board_view(connection: Connection, item_id: str, viewer: str) -> list[dict[str, Any]]:
    out = []
    for m in collab.messages(connection, item_id, viewer=viewer)[-BOARD_WINDOW:]:
        body = {k: v for k, v in (m.get("body") or {}).items() if k != "packet"}
        out.append({"id": m["id"], "kind": m["kind"], "by": m["sender"],
                    **({"reply_to": m["reply_to"]} if m.get("reply_to") else {}), "body": body})
    return out


def _citable(connection: Connection, item: Mapping[str, Any]) -> list[str]:
    refs = [f"work_item:{item['id']}"]
    for link in board.links(connection, item["id"]):
        if link["kind"] in ("signal", "workflow_run", "meeting", "incident", "alert",
                            "code_task", "act"):
            refs.append(f"{link['kind'] if link['kind'] != 'act' else 'act'}:{link['ref']}")
        elif link["kind"] == "pull_request":
            refs.append(f"pull_request:{link['ref']}")
    refs += [f"message:{m['id']}" for m in collab.messages(
        connection, item["id"], kinds=("finding", "hypothesis", "proposal"))][-12:]
    return refs


# ── what a member may read before it moves ───────────────────────────────────────────────────
#
# A MEMBER LOOKS WITH THE TOOLS IT HOLDS. Every agent holds BOSS's fundamentals reads (logs,
# health, run history, diagnoses: agent_baseline, granted in role_skills) and whatever a person
# gave it on top. A squad turn used to offer none of them — the decision was asked with no read
# phase — so on boss-dogfood the Infra Steward's squad, holding `boss.fundamentals` (read_logs,
# system_health, inspect_self), told a person it had "no tool access to host:install logs" and
# asked them to grant it. Now each turn offers the READ tools of the bundles the member holds
# (the grant is role_skills, nothing wider), bounded in calls and cost; writes stay the board's
# moves and the gate's. A model that cannot call tools (a scripted test model) gets none.

#: A member's looks per turn, and what they may cost.
READ_TURNS = 3
READ_USD_MICROS = 150_000


@functools.lru_cache(maxsize=4)
def _bundle_reads(edition: Optional[str]) -> dict[str, tuple[str, ...]]:
    """{bundle skill id: the READ tools it offers on this build} (agent_baseline.bundles)."""
    from ..agent_baseline import bundles

    registry = _read_registry(edition)
    names = set(registry.names())
    return {skill_id: tuple(t for t in tools
                            if t in names and getattr(registry.get(t), "kind", None) == "read")
            for skill_id, tools in bundles().items()}


@functools.lru_cache(maxsize=4)
def _read_registry(edition: Optional[str]) -> Any:
    from ...tools import build_discuss_read_registry

    return build_discuss_read_registry(edition=edition)


def held_skill_ids(connection: Connection, project_id: str, member: str) -> set[str]:
    """The skills granted to the member's role here (role_skills: the grant)."""
    return {str(s) for s in connection.execute(text(
        "SELECT skill_id FROM role_skills WHERE project_id = :p AND role_key = :r"),
        {"p": project_id, "r": party_id(member)}).scalars()}


def held_reads(connection: Connection, settings: Any, project_id: str,
               member: str) -> list[str]:
    """The read tools the member holds here: those of every bundle skill its role was granted
    (and that the project can run: agent_steps.holdings, the one reading of a grant)."""
    from ..agent_steps import holdings

    role = party_id(member)
    try:
        with connection.begin_nested():
            held = (holdings(connection, settings, project_id, role).get(role) or {}).get(
                "runnable") or []
    except Exception:  # noqa: BLE001 — a member whose grant cannot be read looks with nothing
        return []
    reads = _bundle_reads(getattr(settings, "boss_edition", None) if settings else None)
    return sorted({t for s in held for t in reads.get(str(s), ())})


def read_phase(connection: Connection, settings: Any, project_id: str, member: str) -> Any:
    """The member's read phase (agent_decision.ReadPhase over the reads it holds), or None."""
    from ...context import EngineContext
    from ..agent_service import ReadPhase

    names = held_reads(connection, settings, project_id, member)
    if not names:
        return None
    registry = _read_registry(getattr(settings, "boss_edition", None) if settings else None)
    allowed = [n for n in names if n in registry.names()]
    if not allowed:
        return None
    ctx = EngineContext(settings=settings, engine=getattr(connection, "engine", None))

    def run(name: str, args: dict) -> dict:
        if name not in allowed:
            return {"ok": False, "error": f"'{name}' is not a read tool {member} holds"}
        return registry.invoke(name, ctx, project_id=project_id, actor=member, args=args or {})

    return ReadPhase(tools=[registry.get(n).openai() for n in allowed], run=run,
                     max_turns=READ_TURNS, max_usd_micros=READ_USD_MICROS)


def _can_read(adapter: Any, decider: Any) -> bool:
    """A read phase needs a model that can call tools: the routed one (adapter None), or a
    given adapter with a chat-completion call. A typed decider (Jev) answers without tools."""
    return decider is None and (adapter is None or hasattr(adapter, "create_chat_completion"))


# ── the candidates a member has on its turn ──────────────────────────────────────────────────

def _schema(required: tuple[str, ...], **props: tuple[str, str]) -> dict[str, Any]:
    return {"properties": {k: {"type": t, "description": d} for k, (t, d) in props.items()},
            "required": list(required)}


_EVIDENCE = ("array", "refs you cite, each 'kind:ref' from the citable list (or commit:<sha>, "
                      "url:<https…>, pull_request:<owner/repo#n>, log:<what>)")
_SCHEMAS = {
    "finding": _schema(("summary",), summary=("string", "what you established, one sentence"),
                       evidence=_EVIDENCE),
    "hypothesis": _schema(("summary", "confidence"),
                          summary=("string", "what you think is true, one sentence"),
                          confidence=("number", "0..1"), evidence=_EVIDENCE),
    "proposal": _schema(("action", "summary"),
                        action=("string", "one of: " + ", ".join(PROPOSABLE)),
                        summary=("string", "what exactly to do, one sentence"),
                        verify=("string", "how the squad will know it worked"),
                        workflow=("string", "for start_workflow/open_draft_pr: the workflow key")),
    "critique": _schema(("position", "summary"),
                        position=("string", "agree | object | concern | unsafe"),
                        summary=("string", "why, one sentence")),
    "vote": _schema(("vote",), vote=("string", "approve | reject | abstain"),
                    reason=("string", "why, one sentence")),
    "hand_lead": _schema(("to", "summary"), to=("string", "agent:<role> on this squad"),
                         summary=("string", "why they should lead now")),
    # An ask a person reads in one line: what the squad cannot do and what it needs from them
    # (the card says "<squad> can't <blocked> — <need>, or take it?"; `reason` rides behind it).
    "escalate": _schema(("reason", "blocked", "need", "need_kind"),
                        reason=("string", "what is stuck and what you need, in full"),
                        need_kind=("string", "access (a tool or skill you lack: name it in "
                                             "`grant`) | information (facts only a person "
                                             "has) | decision (a person's call)"),
                        blocked=("string", "what the squad cannot do, a few words starting with "
                                           "a verb, e.g. \"read the install's logs to explain "
                                           "the restarts\""),
                        need=("string", "what a person must give, 2-5 words starting with a "
                                        "verb, e.g. \"grant log access\", \"confirm the "
                                        "rollback\", \"send the restart times\""),
                        grant=("string", "only when you lack access: the tool or skill id you "
                                         "need (one you already hold is offered to you: call "
                                         "it instead)")),
    "resolve": _schema(("outcome", "summary"), outcome=("string", "resolved | failed"),
                       summary=("string", "how it ended, one sentence")),
}


def _choices(*, me: str, seat: str, lead: str, prop: Optional[Mapping[str, Any]],
             mode: Optional[str], replies: list[dict[str, Any]], proposals_left: int,
             decided: bool) -> list[Any]:
    from ..agent_service import Choice

    is_lead = me == lead
    out = [Choice("finding", "post a finding you can back with evidence", acts=False,
                  label="Post a finding", arguments=_SCHEMAS["finding"]),
           Choice("hypothesis", "post a hypothesis with your confidence", acts=False,
                  label="Post a hypothesis", arguments=_SCHEMAS["hypothesis"])]
    if is_lead and prop is None and proposals_left > 0:
        out.append(Choice("proposal", "propose ONE action for the squad to check (the gate and "
                                      "the consensus rule decide whether it may happen)",
                          label="Propose", arguments=_SCHEMAS["proposal"]))
    if prop is not None and prop["sender"] != me:
        mine = {r["kind"] for r in replies if r["sender"] == me}
        if "critique" not in mine:
            out.append(Choice("critique", "agree with, object to, raise a concern about, or "
                                          "flag as unsafe the open proposal", acts=False,
                              label="Critique", arguments=_SCHEMAS["critique"]))
        if mode in ("squad_vote_and_person", "recommend_only") and "vote" not in mine:
            out.append(Choice("vote", "cast your vote on the open proposal (you cannot see the "
                                      "others' votes until you have voted)", acts=False,
                              label="Vote", arguments=_SCHEMAS["vote"]))
    if is_lead:
        out.append(Choice("hand_lead", "hand the lead (and the item's lease) to another squad "
                                       "member better placed to own it now", label="Hand lead",
                          arguments=_SCHEMAS["hand_lead"]))
        out.append(Choice("escalate", "put the item in front of a person: the squad is stuck",
                          label="Ask a person", arguments=_SCHEMAS["escalate"]))
        if decided and prop is None:
            out.append(Choice("resolve", "end the item: the decided work is done (or failed)",
                              label="Resolve", arguments=_SCHEMAS["resolve"]))
    else:
        out.append(Choice("escalate", "raise a concern to the lead that the board cannot carry",
                          acts=False, label="Raise to the lead",
                          arguments=_SCHEMAS["escalate"]))
    out.append(Choice("pass", "nothing new to add this round", acts=False, label="Pass"))
    return out


NEED_KINDS = ("access", "information", "decision")


def _check_escalation(connection: Connection, project_id: str, me: str,
                      args: Mapping[str, Any]) -> str:
    """A LEAD'S ASK IS CHECKED AGAINST THE TOOL CATALOGUE BEFORE IT REACHES A PERSON. An ask for
    access names what it lacks (`grant`); one it already holds is refused (call it), and one that
    exists nowhere in the catalogue is refused with the reads it does hold — so "no squad tool
    reaches host:install logs" (boss-dogfood 2026-11-07, while install_restarts and read_logs
    were offered) never becomes a card. "" when it may go to a person."""
    kind = str(args.get("need_kind") or "")
    if kind not in NEED_KINDS:
        return f"need_kind is one of {', '.join(NEED_KINDS)}"
    if kind != "access":
        return ""
    wanted = str(args.get("grant") or "").strip()
    held = held_reads(connection, None, project_id, me)
    if not wanted:
        return ("an ask for access names the tool or skill you need in `grant`; the reads you "
                f"hold are: {', '.join(held) or 'none'}")
    if wanted in held or wanted in held_skill_ids(connection, project_id, me):
        return (f"you already hold {wanted}: call it (it is offered to you) instead of asking a "
                "person for access")
    if asks.grantable(connection, project_id, me, wanted) is None:
        return (f"no tool or skill named {wanted} exists in BOSS's catalogue, so a person cannot "
                f"grant it. The reads you hold: {', '.join(held) or 'none'}. Call the one that "
                "answers it; if none can, ask for information or a decision instead")
    return ""


def _check_answer(connection: Connection, item: Mapping[str, Any], *, me: str,
                  squad: Mapping[str, Any], prop: Optional[Mapping[str, Any]]):
    """The caller's check (agent_decision `check`): refuse an answer the board would refuse, so
    the model is asked once more with the reason instead of the turn failing."""
    posted = collab.messages(connection, item["id"])
    tried = {_norm(r) for r in squad_memory.read(connection, item["id"])["tried_and_failed"]}

    def check(answer: Mapping[str, Any], _corpus: str) -> str:
        move, args = answer.get("action"), dict(answer.get("arguments") or {})
        if args.get("evidence") is not None:
            if not isinstance(args["evidence"], list):
                return "evidence is a list of 'kind:ref' strings"
            try:
                verify_evidence(connection, item["project_id"], args["evidence"])
            except EvidenceError as exc:
                return f"{exc}; cite only refs from the citable list"
        if move == "proposal":
            if args.get("action") not in PROPOSABLE:
                return f"action is one of {', '.join(PROPOSABLE)}"
            if any(_norm(f"{args.get('action')}: {args.get('summary')}") in t for t in tried):
                return "the squad already tried that and it was rejected; propose something else"
        if move in ("finding", "hypothesis", "proposal", "critique"):
            said = _norm(args.get("summary"))
            if any(m["sender"] == me and m["kind"] == move
                   and _norm((m.get("body") or {}).get("summary")) == said for m in posted):
                return "you already posted exactly that; add something new or pass"
        if move == "critique" and args.get("position") not in collab.POSITIONS:
            return f"position is one of {', '.join(collab.POSITIONS)}"
        if move == "vote" and args.get("vote") not in collab.VOTES:
            return f"vote is one of {', '.join(collab.VOTES)}"
        if move == "hand_lead" and (squads.seat_of(squad, str(args.get("to"))) is None
                                    or args.get("to") == me):
            return "hand the lead to another member of this squad (agent:<role>)"
        if move == "escalate" and me == squad["lead"]:
            refused = _check_escalation(connection, item["project_id"], me, args)
            if refused:
                return refused
        if move == "resolve" and args.get("outcome") not in ("resolved", "failed"):
            return "outcome is resolved or failed"
        if move == "hypothesis":
            try:
                if not 0 <= float(args.get("confidence")) <= 1:
                    return "confidence is between 0 and 1"
            except (TypeError, ValueError):
                return "confidence is a number between 0 and 1"
        return ""

    return check


# ── guards ───────────────────────────────────────────────────────────────────────────────────

def _guard(connection: Connection, item_id: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    """(item, squad, the lead's token) — or TurnRefused naming the guard."""
    item = board.get(connection, item_id)
    if item is None:
        raise LookupError(f"no ambient work item {item_id}")
    squad = squads.roster(connection, item_id)
    if squad is None:
        raise TurnRefused("no_squad", "no squad is seated on this item (squads.assemble)")
    if item["lifecycle"] in board.TERMINAL:
        raise TurnRefused("ended", f"the item is {item['lifecycle']}")
    if item["lifecycle"] == "needs_you":
        raise TurnRefused("needs_you", "the item is waiting on a person")
    if policy.is_stopped(connection, item["project_id"]):
        raise TurnRefused("stopped", "ambient work is stopped here")
    if item.get("lease_kind") == "person":
        raise TurnRefused("taken_over", f"{item.get('lease_owner')} has taken this over")
    token = squads.lead_token(connection, item_id, squad["lead"])
    try:
        if token is None:
            raise board.LeaseLost("the lead does not hold the item")
        board.assert_lease(connection, item_id, owner=squad["lead"], token=token)
    except board.LeaseLost as exc:
        raise TurnRefused("lease_lost", str(exc)) from None
    if _turns(connection, item) >= MAX_TURNS_PER_ITEM:
        raise TurnRefused("turn_cap", f"{MAX_TURNS_PER_ITEM} squad turns spent on this item")
    if int(item.get("cost_usd_micros") or 0) >= SQUAD_BUDGET_USD_MICROS:
        raise TurnRefused("budget", "the squad's budget for this item is spent")
    if policy.envelope(connection, item["project_id"])["available_usd_micros"] <= 0:
        raise TurnRefused("envelope", "today's ambient envelope is spent")
    return item, squad, token


def _resume_lead(connection: Connection, item_id: str) -> None:
    """At a round's start: the lead takes the item back if its lease lapsed (never from a
    person, never while stopped — claim refuses both)."""
    item = board.get(connection, item_id)
    squad = squads.roster(connection, item_id)
    if item is None or squad is None or item["lifecycle"] not in ("proposed", "claimed"):
        return
    if item.get("lease_kind") == "person":
        return
    token = squads.lead_token(connection, item_id, squad["lead"])
    try:
        if token is not None:
            board.assert_lease(connection, item_id, owner=squad["lead"], token=token)
            return
    except board.LeaseLost:
        pass
    try:
        board.claim(connection, item_id, owner=squad["lead"], minutes=60)
    except board.Conflict:
        return


# ── one turn ─────────────────────────────────────────────────────────────────────────────────

def _model(model: ModelFor, member: str) -> Any:
    if model is None:
        return None
    if callable(model) and not hasattr(model, "create_text_response") and not hasattr(
            model, "create_chat_completion"):
        return model(member)
    return model


def take_turn(connection: Connection, settings: Any, item_id: str, member: str, *,
              round_no: int = 1, model: ModelFor = None, decider: Any = None,
              read: Any = None, launchers: Optional[squad_plugins.Launchers] = None
              ) -> dict[str, Any]:
    """One squad member's turn on the item, through the one agent pipeline. Returns
    {member, seat, move, status, message_id?, reason, cost_usd_micros}. Raises TurnRefused."""
    from ..agent_service import AgentService

    item, squad, token = _guard(connection, item_id)
    member = party(member, allow_boss=False)
    seat = squads.seat_of(squad, member)
    if seat is None:
        raise TurnRefused("not_seated", f"{member} is not on this item's squad")
    prop = open_proposal(connection, item_id)
    mode = None
    replies: list[dict[str, Any]] = []
    if prop is not None:
        mode = policy.gate(connection, item["project_id"], item["task_class"],
                           prop["body"]["action"])["consensus_mode"]
        replies = _replies(connection, prop["id"])
    proposals = len(collab.messages(connection, item_id, kinds=("proposal",)))
    decided = connection.execute(text(
        "SELECT 1 FROM agent_act_decisions WHERE work_item_id = :w AND action IS NOT NULL "
        "LIMIT 1"), {"w": item_id}).first() is not None
    choices = _choices(me=member, seat=seat, lead=squad["lead"], prop=prop, mode=mode,
                       replies=replies, proposals_left=MAX_PROPOSALS_PER_ITEM - proposals,
                       decided=decided)
    klass = tc.task_class(item["task_class"])
    name = squads.label(member)
    context: dict[str, Any] = {
        "the item": {k: item.get(k) for k in ("id", "title", "task_class", "severity",
                                              "subject_ref", "app", "lifecycle",
                                              "autonomy_level")},
        "your seat": {"you": member, "seat": seat, "lead": squad["lead"],
                      "squad": [f"{m['party']} ({m['seat']})" for m in squad["members"]]},
        "squad memory (read first)": squad_memory.pack(connection, item_id),
        "the board (oldest first)": _board_view(connection, item_id, member),
        "refs you may cite": _citable(connection, item),
    }
    if prop is not None:
        context["the open proposal"] = {"id": prop["id"], "by": prop["sender"],
                                        "action": prop["body"]["action"],
                                        "summary": prop["body"].get("summary"),
                                        "consensus rule": mode}
    if member == squad["lead"]:
        context["what the autonomy gate allows here"] = {
            a: (lambda v: "allowed" if v["allowed"] and not v["needs_person"] else
                ("a person decides" if v["needs_person"] else "not at this level"))(
                policy.gate(connection, item["project_id"], klass.key, a)) for a in PROPOSABLE}
    try:
        from ..agent_steps import agent_brief

        with connection.begin_nested():
            brief = agent_brief(connection, item["project_id"], party_id(member))
    except Exception:  # noqa: BLE001 — a role with no brief still takes its turn by name
        brief = ""
    situation = (f"You are {name} ({member}), the {seat} of the {squad['title']} working one "
                 f"BOSS work item together on a shared board. SEAT: {member}. Round "
                 f"{round_no}. Agents coordinate ONLY by typed board moves; the lead proposes, "
                 "the critic checks, the autonomy gate and the consensus rule decide whether "
                 "anything happens. Read the squad memory first and never repeat what was "
                 "tried and failed. Cite only refs you were given.")
    adapter = _model(model, member)
    if read is None and _can_read(adapter, decider):
        # The tools it holds, to look before it moves (and before it says it cannot see).
        read = read_phase(connection, settings, item["project_id"], member)
    decision = AgentService(settings).decide(
        connection, project_id=item["project_id"], actor=member, role=party_id(member),
        agent=name, external_ref=f"work_item:{item_id}", situation=situation,
        message="What is your one move on this item now?", choices=choices, context=context,
        hints=[f"{klass.title} · {item.get('autonomy_level')} · seat {seat}"],
        adapter=adapter, decider=decider, brief=brief,
        check=_check_answer(connection, item, me=member, squad=squad, prop=prop), read=read)
    cost = int(decision.cost_usd_micros or 0)
    if cost:
        board.add_cost(connection, item_id, cost)
    result = {"member": member, "seat": seat, "round": round_no, "move": decision.action,
              "status": decision.status, "reason": decision.reason or decision.question
              or decision.refused or decision.error, "cost_usd_micros": cost}
    if decision.decided:
        result.update(_apply(connection, settings, item, squad, token, member, decision,
                             prop=prop, launchers=launchers))
    else:
        result["move"] = "pass"
        result["status"] = "no_move" if decision.status == "no_model" else "unsure"
    emit(connection, item["project_id"], "ambient.squad_turn", member, "work_item", item_id,
         {"member": member, "seat": seat, "round": round_no, "move": result["move"],
          "status": result["status"], "message_id": result.get("message_id"),
          "cost_usd_micros": cost})
    return result


def _apply(connection: Connection, settings: Any, item: Mapping[str, Any],
           squad: Mapping[str, Any], token: str, member: str, decision: Any, *,
           prop: Optional[Mapping[str, Any]],
           launchers: Optional[squad_plugins.Launchers]) -> dict[str, Any]:
    move, args = decision.action, dict(decision.arguments or {})
    item_id = item["id"]
    if move == "pass":
        return {"status": "passed"}
    if move in ("finding", "hypothesis"):
        body = {"summary": args.get("summary"), "evidence": args.get("evidence") or []}
        if move == "hypothesis":
            body["confidence"] = args.get("confidence")
        m = collab.post(connection, item_id, kind=move, sender=member, body=body)
        return {"status": "posted", "message_id": m["id"]}
    if move == "proposal":
        action = tc.action(args["action"])
        m = collab.post(connection, item_id, kind="proposal", sender=member, body={
            "action": action.key, "summary": args.get("summary"), "verify": args.get("verify"),
            "workflow": args.get("workflow"), "confidence": decision.confidence,
            "undo": {"action": DEFAULT_UNDO.get(action.key, f"reverse: {action.title}")}})
        squad_memory.note(connection, item_id, actor=member,
                          phase=f"Proposal open: {action.title}",
                          next_step=clean_text(args.get("summary"), 300))
        out = {"status": "posted", "message_id": m["id"]}
        mode = policy.gate(connection, item["project_id"], item["task_class"],
                           action.key)["consensus_mode"]
        if mode == "single":
            out["settled"] = settle(connection, settings, item_id, end_of_round=True,
                                    launchers=launchers)
        return out
    if move in ("critique", "vote"):
        if prop is None:
            return {"status": "refused", "reason": "no open proposal"}
        body = ({"position": args.get("position"), "summary": args.get("summary")}
                if move == "critique" else {"vote": args.get("vote"), "reason": args.get("reason")})
        m = collab.post(connection, item_id, kind=move, sender=member, body=body,
                        reply_to=prop["id"])
        return {"status": "posted", "message_id": m["id"]}
    if move == "hand_lead":
        got = squads.hand_lead(connection, item_id, to=args["to"], token=token,
                               summary=args.get("summary") or "")
        return {"status": "handed", "lead": got["lead"]}
    if move == "escalate":
        if member == squad["lead"]:
            if asks.refused_launches(connection, item_id) >= asks.MAX_REFUSED_LAUNCHES:
                # BOSS refused the squad's decided action twice: that is the policy's answer,
                # recorded on the ledger and the board — never a question to a person.
                return stop_by_policy(connection, item, squad, token)
            entry = asks.question_entry(
                connection, item, actor=member, reason=args.get("reason"),
                squad_title=squad["title"], blocked=args.get("blocked"), need=args.get("need"),
                grant=(asks.grantable(connection, item["project_id"], member, args.get("grant"))
                       if args.get("need_kind") == "access" else None),
                reads=len(held_reads(connection, settings, item["project_id"], member)))
            got = asks.ask(connection, settings, item_id, actor=member, token=token,
                           entry=entry)
            squad_memory.note(connection, item_id, actor=member, phase="Asked a person")
            return {"status": "needs_you", "decision_id": got["needs_you_decision_id"]}
        m = collab.post(connection, item_id, kind="escalate", sender=member,
                        body={"to": squad["lead"], "reason": args.get("reason")})
        return {"status": "posted", "message_id": m["id"]}
    if move == "resolve":
        outcome = args.get("outcome") or "resolved"
        board.finish(connection, item_id, actor=member, lifecycle=outcome, token=token,
                     outcome=clean_text(args.get("summary"), 280))
        learned = squad_memory.close(connection, item_id, actor=member)
        return {"status": outcome, "learning": (learned.get("learning") or {}).get("id"),
                "learning_skipped": learned.get("skipped")}
    return {"status": "refused", "reason": f"unknown move {move}"}


# ── consensus: settling a proposal ───────────────────────────────────────────────────────────

def tie_break(record: Mapping[str, Any]) -> dict[str, Any]:
    """Rule 3: a squad vote with as many approvals as rejections (quorum met) has no majority."""
    out = dict(record)
    counted = record.get("tally") or {}
    if record.get("mode") in ("squad_vote_and_person", "recommend_only") and counted.get(
            "quorum_met") and counted.get("approve", 0) == counted.get("reject", 0) and \
            counted.get("approve", 0) > 0:
        out["result"], out["needs_person"] = "tied", True
        out["tie_break"] = "no majority: a person decides; the squad recommends holding"
    return out


def _rationale(record: Mapping[str, Any], *, what: str) -> str:
    agreed = sorted({c["critic"] for c in record.get("critiques") or ()
                     if c.get("position") == "agree"} |
                    {v["voter"] for v in record.get("votes") or () if v.get("vote") == "approve"})
    parts = [f"{record.get('proposer')} proposed {what} under {record.get('mode')}"]
    if agreed:
        parts.append(f"{', '.join(agreed)} agreed")
    if record.get("dissent"):
        parts.append(f"{len(record['dissent'])} dissent kept on record")
    return ("; ".join(parts) + ".").replace("..", ".")


def _evidence_for(connection: Connection, item_id: str, prop: Mapping[str, Any],
                  replies: list[dict[str, Any]]) -> list[dict[str, str]]:
    refs = [{"kind": "message", "ref": prop["id"]}]
    refs += [{"kind": "message", "ref": r["id"]} for r in replies][:4]
    refs += [{"kind": "message", "ref": m["id"]} for m in collab.messages(
        connection, item_id, kinds=("finding",))][-3:]
    return refs


def settle(connection: Connection, settings: Any, item_id: str, *, end_of_round: bool,
           launchers: Optional[squad_plugins.Launchers] = None) -> Optional[dict[str, Any]]:
    """Settle the open proposal if it is ready. Returns {state, ...} or None (nothing open, or
    still waiting on the squad). state: decided | revise | needs_you | waiting."""
    item = board.get(connection, item_id)
    squad = squads.roster(connection, item_id)
    if item is None or squad is None or item["lifecycle"] not in ("claimed", "in_progress"):
        return None
    prop = open_proposal(connection, item_id)
    if prop is None:
        return None
    lead = squad["lead"]
    token = squads.lead_token(connection, item_id, lead)
    if token is None:
        return None
    body = prop["body"]
    action = tc.action(body["action"])
    verdict = policy.gate(connection, item["project_id"], item["task_class"], action.key)
    record = tie_break(collab.tally(connection, item_id, prop["id"],
                                    mode=verdict["consensus_mode"]))
    mode = record["mode"]
    replies = _replies(connection, prop["id"])
    others = [m["party"] for m in squad["members"] if m["party"] != prop["sender"]]
    since = _turns(connection, item, since=prop["created_at"], exclude=prop["sender"])
    if mode == "maker_checker":
        if not end_of_round:
            return None
        if record["result"] == "pending":
            if since < 2 * max(1, len(others)):
                return None
            record = {**record, "result": "needs_person", "needs_person": True,
                      "tie_break": "nobody checked it within two turns each"}
    elif mode != "single":
        voted = {v["voter"] for v in record["votes"]}
        if not end_of_round or (not set(others) <= voted and since < len(others)):
            return None

    if record["result"] == "approved" and not record["needs_person"] and \
            verdict["allowed"] and not verdict["needs_person"]:
        return _approve(connection, item, squad, token, prop, record, replies, launchers)
    if record["result"] == "rejected" and not record["needs_person"]:
        return _revise(connection, item, squad, prop, record, replies)
    if not verdict["allowed"] and not verdict["needs_person"]:
        return _revise(connection, item, squad, prop,
                       {**record, "dissent": record["dissent"] + [
                           {"party": "boss", "position": "gate", "reason": verdict["reason"]}]},
                       replies)
    return _to_person(connection, item, squad, token, prop, record, verdict, settings)


def _approve(connection, item, squad, token, prop, record, replies, launchers):
    lead = squad["lead"]
    action = tc.action(prop["body"]["action"])
    item_id = item["id"]
    if item["lifecycle"] == "claimed":
        try:
            board.start(connection, item_id, owner=lead, token=token)
        except board.Conflict as exc:
            return {"state": "waiting", "reason": str(exc)}
    board.assert_lease(connection, item_id, owner=lead, token=token, lock=True)
    decided_by = f"squad:{item_id}" if record["mode"] != "single" else lead
    decision = ledger.record_decision(
        connection, project_id=item["project_id"], decided_by=decided_by,
        question=clean_text(f"{action.title}: {prop['body'].get('summary')}?", 290),
        chosen=action.key, options=[{"key": action.key, "label": action.title},
                                    {"key": "hold", "label": "Hold"}],
        rejected=[{"option": "hold", "why": "the squad agreed to act"}],
        rationale=_rationale(record, what=action.title), reversibility=action.reversibility,
        undo=prop["body"].get("undo") or {"action": DEFAULT_UNDO.get(action.key, "reverse")},
        evidence=_evidence_for(connection, item_id, prop, replies),
        confidence=prop["body"].get("confidence"), work_item_id=item_id,
        task_class=item["task_class"], action=action.key, consensus=record,
        proposal_id=prop["id"])
    squad_memory.note(connection, item_id, actor=lead, phase=f"Decided: {action.title}")
    out: dict[str, Any] = {"state": "decided", "act_id": decision["act_id"],
                           "action": action.key, "dissent": record["dissent"]}
    out["executed"] = _execute(connection, item_id, squad, prop, decision, launchers)
    if (out["executed"] or {}).get("error") and \
            asks.refused_launches(connection, item_id) >= asks.MAX_REFUSED_LAUNCHES:
        out["stopped"] = stop_by_policy(connection, board.get(connection, item_id), squad,
                                        token, action=action.key)
    return out


def stop_by_policy(connection: Connection, item: Mapping[str, Any], squad: Mapping[str, Any],
                   token: str, *, action: Optional[str] = None) -> dict[str, Any]:
    """BOSS refused what the squad decided, twice: a POLICY OUTCOME. It is recorded on the
    ledger (decided by the policy, in plain words) and the item ends — nobody is asked
    "which workflow is permitted?" (2026-11-07: "A2 gate has twice refused …" cards)."""
    from .asks import action_words

    what = action_words(action or connection.execute(text(
        "SELECT action FROM agent_act_decisions WHERE work_item_id = :w AND action IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 1"), {"w": item["id"]}).scalar())
    ledger.record_decision(
        connection, project_id=item["project_id"], decided_by="policy:squad_launch",
        question=f"BOSS refused to {what} twice: ask a person?", chosen="stop",
        options=["stop", "ask"], work_item_id=item["id"], task_class=item["task_class"],
        evidence=asks.item_evidence(connection, item["id"]),
        rationale=(f"BOSS refused to {what} here twice. That is the policy's answer, so it is "
                   "recorded as the outcome and nobody is asked."),
        reversibility="reversible", undo={"action": "reopen", "work_item_id": item["id"]})
    squad_memory.note(connection, item["id"], actor=squad["lead"],
                      phase=f"Stopped: BOSS refused to {what} twice")
    board.finish(connection, item["id"], actor=squad["lead"], lifecycle="failed", token=token,
                 outcome=f"BOSS refused to {what} twice (policy)")
    return {"status": "failed", "stopped": "policy"}


def _execute(connection, item_id, squad, prop, decision, launchers) -> Optional[dict[str, Any]]:
    action = prop["body"]["action"]
    if not squad_plugins.executes(action):
        squad_memory.note(connection, item_id, actor=squad["lead"],
                          next_step=f"{action} is carried out by its owner, not the squad")
        return None
    if launchers is None:
        return {"error": "no launchers given; the decision is recorded and waits"}
    return squad_plugins.EXECUTORS[action](
        connection, board.get(connection, item_id), squad=squad, proposal=prop,
        decision=decision, launchers=launchers)


def _revise(connection, item, squad, prop, record, replies):
    body = prop["body"]
    reasons = "; ".join(f"{d['party']}: {d['reason']}" for d in record["dissent"]) or "rejected"
    squad_memory.note(connection, item["id"], actor=squad["lead"],
                      tried_and_failed=f"{body['action']}: {body.get('summary')} — {reasons}",
                      phase="Proposal rejected; revise")
    decision = ledger.record_decision(
        connection, project_id=item["project_id"], decided_by=f"squad:{item['id']}",
        question=clean_text(f"Go ahead with: {body.get('summary')}?", 290), chosen="revise",
        options=[{"key": "proceed", "label": "Proceed"}, {"key": "revise", "label": "Revise"}],
        rejected=[{"option": "proceed", "why": clean_text(reasons, 280)}],
        rationale="The check did not pass, so the proposal goes back to the lead. "
                  "The objection is kept as dissent.",
        reversibility="reversible", undo={"action": "propose again with the objection met"},
        evidence=_evidence_for(connection, item["id"], prop, replies), work_item_id=item["id"],
        task_class=item["task_class"], consensus=record, proposal_id=prop["id"])
    return {"state": "revise", "act_id": decision["act_id"], "dissent": record["dissent"]}


def _to_person(connection, item, squad, token, prop, record, verdict, settings=None):
    """A person must decide the squad's proposal: through the ONE ambient path (asks.ask) —
    it joins the project's one card, in plain words built from the item's rows (the package,
    the app, the action); the squad's own summary and every dissent ride behind it."""
    body = prop["body"]
    action = tc.action(body["action"])
    recommended = "approve" if record["result"] == "approved" else "hold"
    why = record.get("tie_break") or verdict.get("reason") or record["quorum_rule"]
    entry = asks.squad_entry(
        connection, item, actor=squad["lead"], action=action.key, proposal_id=prop["id"],
        recommended=recommended, why=why, squad_title=squad["title"],
        consensus={k: record.get(k) for k in ("mode", "result", "votes", "critiques", "dissent",
                                              "quorum_rule", "tie_break")},
        dissent=record["dissent"])
    entry["detail"] = asks.scrub(body.get("summary"), 300)
    got = asks.ask(connection, settings, item["id"], actor=squad["lead"], token=token,
                   entry=entry)
    squad_memory.note(connection, item["id"], actor=squad["lead"],
                      phase=f"Needs you: {action.title}")
    return {"state": "needs_you", "decision_id": got["needs_you_decision_id"],
            "asked": got["outcome"], "recommended": recommended, "dissent": record["dissent"],
            "result": record["result"]}


# ── a person answers ─────────────────────────────────────────────────────────────────────────

def person_answered(connection: Connection, item_id: str, *, actor: str, approve: bool,
                    rationale: str, entry: Optional[Mapping[str, Any]] = None
                    ) -> dict[str, Any]:
    """A person answered the squad's Needs-you card: record THEIR decision on the ledger (it
    settles the proposal), and move the item back to the squad. The approved action executes
    at the squad's next round (`execute_person_approvals`). The card itself is resolved by the
    Decisions path (907) that calls this."""
    who = person(actor)
    item = board.get(connection, item_id)
    if item is None:
        raise LookupError(f"no ambient work item {item_id}")
    row = connection.execute(text(
        "SELECT recommendation FROM decisions WHERE id = :d"),
        {"d": item.get("needs_you_decision_id")}).first()
    rec = dict(row[0]) if row and isinstance(row[0], Mapping) else {}
    if entry is not None:
        rec = dict(entry)   # one entry on a card that asks several things
    prop_id = rec.get("proposal_id")
    board.answered(connection, item_id, actor=actor)
    if not prop_id:
        return {"item": board.get(connection, item_id), "decision": None}
    action = tc.action(rec["action"])
    decision = ledger.record_decision(
        connection, project_id=item["project_id"], decided_by=who,
        question=clean_text(f"{action.title}?", 290),
        chosen=action.key if approve else "hold",
        options=[{"key": action.key, "label": action.title}, {"key": "hold", "label": "Hold"}],
        rationale=clean_text(rationale, 600) or ("Approved." if approve else "Held."),
        reversibility=action.reversibility if approve else "reversible",
        undo=({"action": DEFAULT_UNDO.get(action.key, "reverse")}
              if (action.reversibility == "reversible" or not approve) else None),
        evidence=[{"kind": "message", "ref": prop_id}], work_item_id=item_id,
        task_class=item["task_class"], action=action.key if approve else None,
        consensus=rec.get("consensus"), proposal_id=prop_id,
        decision_id=item.get("needs_you_decision_id"))
    if not approve:
        squad_memory.note(connection, item_id, actor=who,
                          tried_and_failed=f"{action.key}: held by {who}",
                          phase="A person held it")
    return {"item": board.get(connection, item_id), "decision": decision}


def execute_person_approvals(connection: Connection, item_id: str, *,
                             launchers: Optional[squad_plugins.Launchers]) -> list[dict]:
    """Carry out what a person approved on the squad's card, once."""
    item = board.get(connection, item_id)
    squad = squads.roster(connection, item_id)
    if item is None or squad is None or item["lifecycle"] not in ("claimed", "in_progress"):
        return []
    token = squads.lead_token(connection, item_id, squad["lead"])
    if token is None:
        return []
    carried = set()
    for m in collab.messages(connection, item_id, kinds=("handoff",)):
        decided = ((m.get("body") or {}).get("packet") or {}).get("decided") or {}
        if decided.get("act_id"):
            carried.add(decided["act_id"])
    done = []
    for d in ledger.list_decisions(connection, item["project_id"], work_item_id=item_id):
        if d["decider_kind"] != "person" or not d.get("action") or d["act_id"] in carried \
                or not squad_plugins.executes(d["action"]) or not d.get("proposal_id"):
            continue
        prop = next((m for m in collab.messages(connection, item_id, kinds=("proposal",))
                     if m["id"] == d["proposal_id"]), None)
        if prop is None:
            continue
        if item["lifecycle"] == "claimed":
            board.start(connection, item_id, owner=squad["lead"], token=token)
            item = board.get(connection, item_id)
        board.assert_lease(connection, item_id, owner=squad["lead"], token=token, lock=True)
        done.append(_execute(connection, item_id, squad, prop, d, launchers) or {})
    return done


# ── rounds ───────────────────────────────────────────────────────────────────────────────────

def _tx(bind: Bind):
    if isinstance(bind, Engine):
        return bind.begin()

    class _Same:
        def __enter__(self_inner):
            return bind

        def __exit__(self_inner, *exc):
            return False

    return _Same()


def collaborate(bind: Bind, settings: Any, item_id: str, *, rounds: int = MAX_ROUNDS_PER_RUN,
                model: ModelFor = None, decider: Any = None, read: Any = None,
                launchers: Optional[squad_plugins.Launchers] = None) -> dict[str, Any]:
    """Run the squad on the item for up to `rounds` rounds: each round brings finished runs and
    panels back, carries out what a person approved, gives every member one turn in seat order
    (lead, specialists, critic), then settles the open proposal.

    `bind` is an Engine (each turn commits on its own, so the console watches it live and a
    stop or a take-over between turns is seen at once) or a Connection (one transaction).
    Ends early when the item ends or needs a person, a guard refuses, or a whole round passes
    with nothing new (no progress). Returns {rounds, turns, settled, results, stopped}."""
    rounds = max(1, min(int(rounds), 12))
    if launchers is None and settings is not None:
        launchers = squad_plugins.ToolLaunchers(settings)
    out: dict[str, Any] = {"rounds": 0, "turns": [], "settled": [], "results": [],
                           "stopped": None}
    for round_no in range(1, rounds + 1):
        with _tx(bind) as c:
            _resume_lead(c, item_id)
            back = squad_plugins.collect_results(c, item_id)
            executed = execute_person_approvals(c, item_id, launchers=launchers)
            squad = squads.roster(c, item_id)
        out["results"] += back
        if squad is None:
            out["stopped"] = {"guard": "no_squad", "reason": "no squad is seated on this item"}
            break
        out["rounds"] = round_no
        moved = len(executed) + len(back)
        for m in squad["members"]:
            try:
                with _tx(bind) as c:
                    turn = take_turn(c, settings, item_id, m["party"], round_no=round_no,
                                     model=model, decider=decider, read=read,
                                     launchers=launchers)
            except TurnRefused as stop:
                out["stopped"] = {"guard": stop.guard, "reason": stop.reason}
                break
            out["turns"].append(turn)
            if turn.get("settled"):
                out["settled"].append(turn["settled"])
            if turn["status"] not in ("passed", "unsure", "no_move", "refused"):
                moved += 1
            if turn["status"] in ("resolved", "failed", "needs_you"):
                out["stopped"] = {"guard": "ended" if turn["status"] != "needs_you"
                                  else "needs_you", "reason": turn["status"]}
                break
        if out["stopped"]:
            break
        with _tx(bind) as c:
            settled = settle(c, settings, item_id, end_of_round=True, launchers=launchers)
        if settled:
            out["settled"].append(settled)
            moved += 1
            if settled["state"] == "needs_you":
                out["stopped"] = {"guard": "needs_you", "reason": "waiting on a person"}
                break
        if moved == 0:
            out["stopped"] = {"guard": "no_progress",
                              "reason": "a whole round passed with nothing new"}
            break
    return out


__all__ = ["TurnRefused", "take_turn", "settle", "tie_break", "open_proposal", "collaborate",
           "person_answered", "execute_person_approvals", "PROPOSABLE", "MAX_TURNS_PER_ITEM",
           "SQUAD_BUDGET_USD_MICROS", "MAX_PROPOSALS_PER_ITEM"]
