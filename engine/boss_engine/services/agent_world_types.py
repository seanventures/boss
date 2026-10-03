"""Agent World: the frozen engine contract (spec §5, §0.2 A), written in Phase 0.

Five streams build against this at once, so the shapes here are the ones they agree on:
TypedDicts for what crosses a module boundary, the stub signatures each owning module must
implement with the same name and parameters, and the two tables that are data rather than code
(EDGE_LABELS, the §9 vocabulary; ALIASES, the one namespace for seat titles, captain ids and
stage roles). A stub raises NotImplementedError until its owner's module replaces it:

    agent_acts      → services/agent_acts.py      (S-acts)
    agent_identity  → services/agent_identity.py  (S-identity)
    agent_persona   → services/agent_persona.py   (S-identity)
    agent_converse  → services/agent_converse.py  (S-converse)

Two stubs carry a suffix only because they share this module: backfill_acts and
backfill_identity are each module's `backfill`. Nothing imports this module at runtime yet;
Phase 0 changes no behaviour.
"""

from __future__ import annotations

from typing import Any, Iterable, Iterator, Literal, NoReturn, Optional, TypedDict

# ── Vocabularies ─────────────────────────────────────────────────────────────────────────────

ActKind = Literal["run", "step", "stage", "seat", "chat", "approval", "handoff"]
ActStatus = Literal["pending", "active", "awaiting_input", "complete", "error", "stale", "skipped"]
ActHow = Literal["named", "only_holder", "only_candidate", "model_pick", "captain", "run_actor",
                 "next_stage", "seated", "asked_by_person", "asked_by_agent", "approved", "started"]
HandedByKind = Literal["person", "agent", "boss"]
Lifecycle = Literal["active", "paused", "retired"]
ChangeKind = Literal["born", "persona", "remit", "grant", "revoke", "model", "lifecycle"]
PromptMode = Literal["chat", "work", "panel"]
PresenceState = Literal["idle", "thinking", "speaking", "working", "needs_you", "done", "stuck",
                        "paused", "retired"]
ConversationKind = Literal["talk", "plan", "room"]

#: The words an edge may say (spec §9). The single source: the web renders the API's
#: edge_label and never builds one. `{…}` placeholders are filled by agent_acts.edge_label().
EDGE_LABELS: dict[str, str] = {
    "named": "Named in workflow",
    "only_holder": "Only one holds {skill}",
    "only_candidate": "Only one could",
    "model_pick": "Picked by BOSS over {n}",
    "captain": "Assigned by {captain}",
    "run_actor": "Ran as you",
    "next_stage": "Next stage",
    "seated": "Seated on panel",
    "asked_by_person": "Asked by {actor}",
    "asked_by_agent": "Asked by {agent}",
    "approved": "Approved by you",
    "started": "Started by you",
}

#: Every role key the shipped cast answers to after the Agent World packs land (§0.1, §8).
#: The first block exists today as packages/specpack/roles/*.yaml; the rest S-identity ships.
CANONICAL_ROLE_KEYS: frozenset[str] = frozenset({
    # shipped today
    "adversarial_reviewer", "cao", "coo", "cto", "deployment_manager", "design_critic",
    "docs_writer", "finops_sentinel", "infra_steward", "integration_validator", "media_publisher",
    "metatron", "model_spend_analyst", "pm", "program_manager", "providerops_manager", "qa",
    "release_manager", "resource_janitor", "secrets_steward", "security_reviewer",
    "software_engineer", "video_editor", "vp_agents",
    # new in Agent World (§0.1 cast, §4.4 chief_architect)
    "chief_architect", "engineering_captain", "backend_engineer", "frontend_engineer",
    "operations_engineer", "sre",
    # archetypes, "thinks like" agents (§0.1.3, §8)
    "first_principles_researcher", "visionary_founder", "relentless_operator",
    "teacher_who_simplifies",
})

#: One namespace (spec §4.4). Keys are lower-cased; resolve() lower-cases and trims its input.
#: Sources: meeting_service.ROLE_CATALOG keys and titles, captain_coordination.ROSTER ids,
#: llm_telemetry.ROLE_ALIASES. STAGE_ROLES values are already canonical keys.
ALIASES: dict[str, str] = {
    # meeting_service.ROLE_CATALOG (key, then title)
    "product_manager": "pm", "product manager": "pm",
    "engineer": "software_engineer", "engineer / architect": "software_engineer",
    "ux_designer": "design_critic", "ux designer": "design_critic",
    "privacy_safety": "security_reviewer", "privacy & safety": "security_reviewer",
    "finance": "finops_sentinel", "finance / finops": "finops_sentinel",
    "growth": "pm", "growth / marketing": "pm",
    "qa / test": "qa",
    "customer_advocate": "design_critic", "customer advocate": "design_critic",
    "security": "security_reviewer",
    "operations": "operations_engineer", "operations / deployment manager": "operations_engineer",
    "data": "pm", "data / analytics": "pm",
    # captain_coordination.ROSTER ids (engineering_captain, chief_architect, cto, qa are keys)
    "vp_product": "pm",
    "ux_engineer": "frontend_engineer",
    "factory_driver": "engineering_captain",
    "reviewer": "adversarial_reviewer",
    "queue_helper": "program_manager",
    # llm_telemetry.ROLE_ALIASES
    "architect": "chief_architect",
    # display names of the §0.1 cast
    "engineering captain": "engineering_captain", "backend engineer": "backend_engineer",
    "frontend engineer": "frontend_engineer", "operations engineer": "operations_engineer",
    "site reliability engineer": "sre", "chief architect": "chief_architect",
    "software engineer": "software_engineer", "adversarial reviewer": "adversarial_reviewer",
    "security reviewer": "security_reviewer", "qa lead": "qa", "release manager": "release_manager",
    "design critic": "design_critic", "documentation writer": "docs_writer",
    "docs writer": "docs_writer", "finops sentinel": "finops_sentinel",
}

#: The room presets every install ships (§0.2 A.3). Mirrored in apps/web/lib/agent-world-types.ts.
ROOM_PRESETS: list[dict[str, Any]] = [
    {"key": "product_eng_security", "label": "Product + Eng + Security",
     "seats": ["pm", "engineering_captain", "security_reviewer"]},
    {"key": "architecture_review", "label": "Architecture review",
     "seats": ["chief_architect", "backend_engineer", "frontend_engineer", "adversarial_reviewer"]},
    {"key": "incident", "label": "Incident",
     "seats": ["sre", "operations_engineer", "backend_engineer", "security_reviewer"]},
]

#: The fixed disclosure (L1). Every agent says it is an AI; operator overrides never remove it.
AI_DISCLOSURE = ("You are not a real person and never claim to be, or to be endorsed by, one. "
                 "If asked, say you are an AI agent with a persona.")

# ── Shapes ───────────────────────────────────────────────────────────────────────────────────


class AvatarSpec(TypedDict, total=False):
    kit: Literal["boss-v1"]
    seed: str
    hat: str
    emblem: str
    palette: str
    visor: str


class Voice(TypedDict):
    blunt: int
    thorough: int
    bold: int
    casual: int


class Persona(TypedDict, total=False):
    version: Literal[1]
    archetype: Optional[str]
    callsign: str
    tagline: str
    about_me: str
    voice: Voice
    phrases: list[str]
    never: list[str]
    avatar: AvatarSpec


class PromptBlock(TypedDict):
    layer: Literal["L0", "L1", "L2", "L3", "L4", "L5"]
    text: str


class Act(TypedDict, total=False):
    """One agent_acts row, as the services pass it around (snake_case, the column names)."""
    act_id: str
    project_id: str
    role_key: Optional[str]
    agent_id: Optional[str]
    fingerprint: Optional[str]
    identity_version: Optional[int]
    kind: ActKind
    status: ActStatus
    parent_act_id: Optional[str]
    root_act_id: str
    handed_by_kind: HandedByKind
    handed_by_id: Optional[str]
    how: ActHow
    why: Optional[str]
    considered: list[str]
    scope: dict[str, list[str]]
    scope_ok: Optional[bool]
    accountable: str
    subject_kind: Optional[str]
    subject_id: Optional[str]
    workflow_run_id: Optional[str]
    step_id: Optional[str]
    code_task_run_id: Optional[str]
    meeting_run_id: Optional[str]
    thread_id: Optional[str]
    decision_id: Optional[str]
    label: str
    artifacts: list[dict[str, Any]]
    tokens_in: int
    tokens_out: int
    cost_usd: float
    reconstructed: bool
    started_at: str
    ended_at: Optional[str]
    receipt_id: Optional[str]
    edge_label: str  # computed on read, never stored


class Presence(TypedDict, total=False):
    state: PresenceState
    act_id: str
    line: str
    since: str


class Passport(TypedDict, total=False):
    """GET /v1/agents/{role_key}/passport (spec §5.4)."""
    agent_id: str
    role_key: str
    display_name: str
    persona: Persona
    avatar: AvatarSpec
    born: dict[str, Any]  # {at, by, from}
    sponsor: Optional[str]
    lifecycle: Lifecycle
    version: int
    fingerprint: str
    reports_to: Optional[str]
    vitals: dict[str, float]  # {trust, runs, clean_rate, cost_7d, panels, apps}
    presence: Presence
    identity: dict[str, Any]  # {mode: 'local', tokens: False}


# Conversations (S-converse). Wire shapes, snake_case; the web maps them in lib/agent-converse.ts.


class ProposedStepAgent(TypedDict):
    step_id: str
    step_label: str
    role_key: Optional[str]  # None = Auto


class StartProposal(TypedDict, total=False):
    proposal_id: str
    workflow_key: str          # exactly one of workflow_key / work_class
    work_class: str
    title: str
    directive: str
    inputs: dict[str, str]
    acceptance_criteria: list[str]
    repository: str
    agents: list[ProposedStepAgent]
    why: str
    status: Literal["proposed", "started", "dismissed", "superseded"]
    run_id: str
    readiness: dict[str, Any]  # {ok, problems[]}


class RoomSummary(TypedDict):
    text: str
    by_role_key: str
    decisions: list[dict[str, Any]]     # [{text, for[], against[]}]
    next_actions: list[dict[str, Any]]  # [{text, owner_role_key, kind: start|decision|note, proposal?}]


class ConversationTurn(TypedDict, total=False):
    turn_id: str
    conversation_id: str
    seq: int
    speaker_kind: Literal["person", "agent", "boss"]
    role_key: Optional[str]
    actor: Optional[str]
    addressed_to: list[str]
    reply_to_turn_id: Optional[str]
    kind: Literal["message", "question", "proposal", "summary", "error"]
    content: str
    payload: dict[str, Any]
    status: Literal["streaming", "complete", "error"]
    act_id: Optional[str]
    fingerprint: Optional[str]
    cited_acts: list[str]
    tokens_in: int
    tokens_out: int
    cost_usd: float
    receipt_id: Optional[str]
    created_at: str


class Conversation(TypedDict, total=False):
    conversation_id: str
    project_id: str
    kind: ConversationKind
    title: str
    actor: str
    seats: list[str]
    lead_role_key: str
    preset: Optional[str]
    status: Literal["open", "summarized", "started", "closed"]
    context: dict[str, Any]  # {from, act_id?, workflow_run_id?, step_id?}
    summary: Optional[RoomSummary]
    agent_thread_id: Optional[str]
    root_act_id: Optional[str]
    started_run_id: Optional[str]
    turn_count: int
    turns: list[ConversationTurn]
    created_at: str
    updated_at: str


class ConverseEvent(TypedDict, total=False):
    """One SSE event: `event: <type>` + `data: <json of the rest>`.

    Order per speaking seat: turn_start → token* → (proposal | summary)? → turn_end; then done.
    A seat that fails sends `error` (data.error = model_failures.explain()) and the room moves on.
    """
    type: Literal["turn_start", "token", "proposal", "summary", "turn_end", "error", "done"]
    turn_id: str
    seq: int
    speaker_kind: Literal["agent", "boss"]
    role_key: Optional[str]
    act_id: str
    reply_to_turn_id: str
    text: str
    proposal: StartProposal
    summary: RoomSummary
    status: Literal["complete", "error"]
    tokens_in: int
    tokens_out: int
    cost_usd: float
    receipt_id: str
    cited_acts: list[str]
    error: dict[str, Any]
    conversation: Conversation


def _stub(name: str) -> NoReturn:
    raise NotImplementedError(f"{name}: agent world phase 0 stub")


# ── agent_acts (S-acts) ──────────────────────────────────────────────────────────────────────

def open_act(conn, *, project_id: str, kind: str, role_key: Optional[str], how: str,
             handed_by_kind: str, handed_by_id: Optional[str] = None,
             parent_act_id: Optional[str] = None, root_act_id: Optional[str] = None,
             why: Optional[str] = None, considered: Iterable[str] = (),
             scope: Optional[dict] = None, accountable: str, subject_kind: Optional[str] = None,
             subject_id: Optional[str] = None, label: str, **links: Any) -> str:
    """Write one act; returns act_id."""
    _stub("agent_acts.open_act")


def close_act(conn, act_id: str, *, status: str, artifacts: Iterable[dict] = (),
              tokens_in: int = 0, tokens_out: int = 0, cost_usd: float = 0) -> None:
    _stub("agent_acts.close_act")


def root_for_run(conn, workflow_run_id: str) -> str:
    """The run's kind='run' root act, created on first use."""
    _stub("agent_acts.root_for_run")


def edge_label(how: str, considered_n: int) -> str:
    """The §9 label for an edge, from EDGE_LABELS."""
    _stub("agent_acts.edge_label")


def backfill_acts(conn, project_id: str) -> int:
    """agent_acts.backfill(project_id): reconstructed acts from legacy rows, idempotent."""
    _stub("agent_acts.backfill")


# ── agent_identity (S-identity) ──────────────────────────────────────────────────────────────

def new_agent_id() -> str:
    _stub("agent_identity.new_agent_id")


def canonical(role_row: dict, grants: list, model_policy: dict) -> dict:
    """RFC 8785 JCS canonical document the fingerprint is the sha256 of."""
    _stub("agent_identity.canonical")


def seal_identity(conn, project_id: str, role_key: str, change_kind: str,
                  actor: str) -> tuple[int, str]:
    """(version, fingerprint); a no-op when the hash is unchanged."""
    _stub("agent_identity.seal_identity")


def passport(conn, project_id: str, role_key: str) -> Passport:
    _stub("agent_identity.passport")


def resolve(key_or_title: str) -> Optional[str]:
    """ALIASES lookup: a role key, seat title, captain id or stage role → role_key."""
    _stub("agent_identity.resolve")


def identities(conn, project_id: str) -> list[dict]:
    _stub("agent_identity.identities")


def set_lifecycle(conn, project_id: str, role_key: str, state: str, actor: str,
                  reason: str) -> dict:
    _stub("agent_identity.set_lifecycle")


def agent_card(conn, project_id: str, role_key: str) -> dict:
    """A2A Agent Card v1.0 + x-boss."""
    _stub("agent_identity.agent_card")


def backfill_identity(conn) -> int:
    """agent_identity.backfill(conn)."""
    _stub("agent_identity.backfill")


# ── agent_persona (S-identity) ───────────────────────────────────────────────────────────────

def compose_agent_prompt(role: dict, *, mode: PromptMode, act: Optional[dict],
                         memory: list[dict]) -> list[PromptBlock]:
    """Ordered L0..L5 blocks; the one place an agent's voice is built (chat, work, panel)."""
    _stub("agent_persona.compose_agent_prompt")


def lint_persona(persona: dict) -> tuple[list[dict], list[str]]:
    """(errors[{field, message}], warnings[]): the permission lint (422) and real-name lint (warn)."""
    _stub("agent_persona.lint_persona")


# ── agent_converse (S-converse) ──────────────────────────────────────────────────────────────

def create_conversation(conn, *, project_id: str, actor: str, kind: ConversationKind,
                        seats: list[str], lead_role_key: Optional[str] = None,
                        preset: Optional[str] = None, title: str = "",
                        context: Optional[dict] = None) -> Conversation:
    """No model call. 422 (ValueError) on seat count, unknown or retired agents."""
    _stub("agent_converse.create_conversation")


def get_conversation(conn, project_id: str, conversation_id: str) -> Conversation:
    _stub("agent_converse.get_conversation")


def list_conversations(conn, project_id: str, *, actor: Optional[str] = None,
                       kind: Optional[str] = None, limit: int = 20) -> list[Conversation]:
    _stub("agent_converse.list_conversations")


def take_turn(engine, *, project_id: str, conversation_id: str, actor: str, message: str,
              to: Optional[list[str]] = None, rounds: int = 1) -> Iterator[ConverseEvent]:
    """Record the person's turn, then stream each addressed seat's reply in seat order."""
    _stub("agent_converse.take_turn")


def summarize_room(engine, *, project_id: str, conversation_id: str,
                   actor: str) -> Iterator[ConverseEvent]:
    _stub("agent_converse.summarize_room")


def propose_start(engine, *, project_id: str, conversation_id: str,
                  actor: str) -> Iterator[ConverseEvent]:
    _stub("agent_converse.propose_start")


def start_from_proposal(conn, *, project_id: str, conversation_id: str, actor: str,
                        proposal_id: str, start_anyway: Optional[list[str]] = None) -> dict:
    """{run_id, conversation}; starts through the composer's start path, readiness included."""
    _stub("agent_converse.start_from_proposal")
