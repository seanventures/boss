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

from typing import Any, Literal, Optional, TypedDict

MissionState = Literal["running", "waiting_you", "waiting_person", "waiting", "stopped",
                       "blocked", "done"]
AttentionKind = Literal["approval", "failed_step", "readiness"]


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


class Hop(TypedDict, total=False):
    act_id: str
    role_key: Optional[str]
    person: Optional[str]
    state: str                   # an AvatarState
    label: str
    edge_label: str
    how: str
    inside: int                  # acts folded inside this hop
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


class Counts(TypedDict):
    agents: int
    working: int
    needs_you: int
    running: int
    stopped: int


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


def build(connection: Any, settings: Any, project_id: str) -> LiveBoard:
    """The whole Live tab in one read. Implemented by stream S1."""
    raise NotImplementedError("agents_live_board.build is S1's")
