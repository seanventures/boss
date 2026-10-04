/**
 * Agent World: the frozen contract every stream builds against (spec §12.2, §0.2).
 *
 * Phase 0 writes the shapes only. Five streams (kit, identity, acts, agents, runs) and
 * S-converse build at the same time, so a shape changed here is a shape changed under all of
 * them: change it with the lead, never inside a stream. The entry points at the bottom throw
 * until the owning stream replaces them with the real module named beside each one.
 *
 * Wire format note: the API speaks snake_case. These are the web shapes, after the owning
 * client module has mapped them; the converse wire shapes are spelled out in ConverseWire.
 */

// ── Faces and state ─────────────────────────────────────────────────────────────────────────

export type AvatarState =
  | "idle" | "thinking" | "speaking" | "working" | "needs_you" | "done" | "stuck" | "paused" | "retired";
export type AvatarVariant = "agent" | "auto" | "you" | "person" | "boss";
export type AvatarSize = 16 | 20 | 24 | 32 | 48 | 96 | 320;
export type AvatarSpec = {
  kit: "boss-v1"; seed: string; hat?: string; emblem?: string; palette?: string; visor?: string;
};

// ── Identity and persona ────────────────────────────────────────────────────────────────────

export type Voice = { blunt: number; thorough: number; bold: number; casual: number };
export type Persona = {
  version: 1; archetype: string | null; callsign?: string; tagline?: string; about_me?: string;
  voice?: Voice; phrases?: string[]; never?: string[]; avatar?: AvatarSpec;
};
export type Lifecycle = "active" | "paused" | "retired";
export type AgentIdentity = {
  roleKey: string; agentId: string | null; name: string; callsign?: string; tagline?: string;
  archetype?: string | null; avatar: AvatarSpec; lifecycle: Lifecycle;
};
export type Presence = { state: AvatarState; actId?: string; line: string; since?: string };

// ── Acts (signed work) ──────────────────────────────────────────────────────────────────────

export type ActHow = "named" | "only_holder" | "only_candidate" | "model_pick" | "captain" | "run_actor"
  | "next_stage" | "seated" | "asked_by_person" | "asked_by_agent" | "approved" | "started";
export type ActKind = "run" | "step" | "stage" | "seat" | "chat" | "approval" | "handoff";
export type ActStatus = "pending" | "active" | "awaiting_input" | "complete" | "error" | "stale" | "skipped";
export type Act = {
  actId: string; roleKey: string | null; kind: ActKind;
  status: ActStatus; parentActId?: string; rootActId: string; handedBy: { kind: "person" | "agent" | "boss"; id?: string };
  how: ActHow; edgeLabel: string; why?: string; considered: string[]; scope: { skills: string[]; mcp: string[] };
  scopeOk?: boolean; accountable: string; label: string; links: Record<string, string>;
  artifacts: { kind: string; ref: string; url?: string }[];
  tokensIn: number; tokensOut: number; costUsd: number; reconstructed: boolean; startedAt: string; endedAt?: string;
  version?: number;
};
export type Passport = {
  agentId: string; roleKey: string; displayName: string; persona: Persona;
  born: { at: string; by: string; from: Record<string, unknown> };
  sponsor?: string; lifecycle: Lifecycle; version: number; fingerprint: string; reportsTo?: string;
  vitals: { trust: number; runs: number; cleanRate: number; cost7d: number; panels: number; apps: number };
  presence: Presence; identity: { mode: "local"; tokens: false };
};
export type Chain = {
  acts: Act[]; accountable: string; totals: { steps: number; minutes: number; cost: number };
  /** A person's credit (roleKey null) may name them: `person_id` on the wire. */
  credits: { roleKey: string | null; line: string; personId?: string }[];
};
export type LineageGraph = {
  nodes: { id: string; kind: "agent" | "person" | "boss"; roleKey?: string }[];
  edges: { from: string; to: string; acts: number; howTop: ActHow; label: string; lastAt: string; reconstructed: boolean }[];
};

// ── Conversations: talk, plan, room (S-converse, spec §0.2 A) ───────────────────────────────

/** talk = 1:1 · plan = "Plan it with <agent>", ends in a StartProposal · room = 2-5 SMEs. */
export type ConversationKind = "talk" | "plan" | "room";
export type ConversationStatus = "open" | "summarized" | "started" | "closed";
/** Where it was opened from, so the first turn can be about something. */
export type ConversationContext = {
  from: "passport" | "chip" | "run_step" | "palette" | "presence" | "roster" | "room";
  actId?: string; workflowRunId?: string; stepId?: string;
};
export type TurnKind = "message" | "question" | "proposal" | "summary" | "error";
export type ConversationTurn = {
  turnId: string; seq: number; speakerKind: "person" | "agent" | "boss";
  roleKey: string | null; actor?: string;
  /** @mentioned role keys; empty = the whole room. */
  addressedTo: string[]; replyToTurnId?: string;
  kind: TurnKind; content: string; status: "streaming" | "complete" | "error";
  proposal?: StartProposal; summary?: RoomSummary; error?: ModelFailure;
  actId?: string; citedActs: string[];
  tokensIn: number; tokensOut: number; costUsd: number; receiptId?: string; createdAt: string;
};
/** One step's agent in a proposal. null = Auto (BOSS picks). */
export type ProposedStepAgent = { stepId: string; stepLabel: string; roleKey: string | null };
/** The reviewable card. Nothing starts until the operator presses Start on it. */
export type StartProposal = {
  proposalId: string;
  /** Exactly one of workflowKey / workClass, as the composer's start sends it. */
  workflowKey?: string; workClass?: string;
  title: string; directive: string;
  /** The workflow's declared inputs, keyed by field name, filled values only. */
  inputs: Record<string, string>;
  acceptanceCriteria: string[]; repository?: string;
  agents: ProposedStepAgent[];
  /** One line: why this workflow. */
  why: string;
  status: "proposed" | "started" | "dismissed" | "superseded";
  runId?: string;
  /** Filled by the server's readiness check when the card is made; Start re-checks. */
  readiness?: { ok: boolean; problems: string[] };
};
export type RoomNextAction = {
  text: string; ownerRoleKey: string | null;
  kind: "start" | "decision" | "note";
  /** kind=start carries the card it would start. */
  proposal?: StartProposal;
};
export type RoomSummary = {
  text: string; byRoleKey: string;
  decisions: { text: string; for: string[]; against: string[] }[];
  nextActions: RoomNextAction[];
};
export type RoomPreset = { key: string; label: string; seats: string[] };
export type ConversationSummary = {
  conversationId: string; kind: ConversationKind; title: string; seats: string[];
  leadRoleKey: string; preset?: string; status: ConversationStatus;
  context: ConversationContext; startedRunId?: string; turnCount: number; updatedAt: string;
};
export type Conversation = ConversationSummary & { turns: ConversationTurn[]; summary?: RoomSummary };
/** model_failures.explain(), as the API returns it. */
export type ModelFailure = { kind: string; headline: string; action: string; href?: string; retry: boolean };

/**
 * The SSE stream every converse POST returns (turns, summary, propose). In order:
 * turn_start → token* → (proposal | summary)? → turn_end, repeated per speaking seat, then done.
 * A failed seat sends error for that turn and the room moves on to the next seat.
 */
export type ConverseEvent =
  | { type: "turn_start"; turnId: string; seq: number; speakerKind: "agent" | "boss"; roleKey: string | null;
      actId?: string; replyToTurnId?: string }
  | { type: "token"; turnId: string; text: string }
  | { type: "proposal"; turnId: string; proposal: StartProposal }
  | { type: "summary"; turnId: string; summary: RoomSummary }
  | { type: "turn_end"; turnId: string; status: "complete" | "error"; tokensIn: number; tokensOut: number;
      costUsd: number; receiptId?: string; citedActs: string[] }
  | { type: "error"; turnId?: string; error: ModelFailure }
  | { type: "done"; conversation: ConversationSummary };

/**
 * The HTTP contract (owner: S-converse, block `# --- agent world: converse ---` in main.py;
 * web proxy app/api/conversations/[[...path]]/route.ts). All bodies snake_case on the wire.
 *
 *   GET  /v1/conversations/presets                         → { items: RoomPreset[] }
 *   GET  /v1/conversations?project_id=&kind=&limit=         → { items: ConversationSummary[] }
 *   POST /v1/conversations                                  { project_id, actor, kind, seats[], lead_role_key?,
 *                                                             preset?, title?, context? } → 201 Conversation
 *                                                             (no model call; 422 on seat count / unknown or retired agent)
 *   GET  /v1/conversations/{id}?project_id=                 → Conversation (turns in seq order)
 *   POST /v1/conversations/{id}/turns          (SSE)        { project_id, actor, message, to?: role_key[],
 *                                                             rounds?: 1|2 } → ConverseEvent stream.
 *                                                             message "" on an empty talk/plan = the greeting.
 *                                                             room: every addressed seat speaks once in seat
 *                                                             order, seeing the round so far; rounds=2 lets each
 *                                                             answer the others (reply_to_turn_id set).
 *   POST /v1/conversations/{id}/summary        (SSE)        { project_id, actor } → … summary event; status=summarized
 *   POST /v1/conversations/{id}/propose        (SSE)        { project_id, actor } → … proposal event (plan, or room)
 *   POST /v1/conversations/{id}/start                       { project_id, actor, proposal_id, start_anyway?: string[] }
 *                                                             → { run_id, conversation } via the composer's start path
 *                                                             (POST /v1/delivery-workflows/start, readiness 409 passed
 *                                                             through unchanged); proposal.agents go as run input
 *                                                             `step_agents: {step_id: role_key}`.
 *   POST /v1/conversations/{id}/proposals/{proposal_id}/dismiss { project_id, actor } → Conversation
 *   POST /v1/conversations/{id}/close                       { project_id, actor } → Conversation
 *
 * Refusals: 409 { code: "needs_model", message: "Needs a model. Connect one in Repo and models." } ·
 * 409 { code: "budget", … } from the budget gate · 403 not a project actor · 404 other project's id.
 * Acts: each agent turn opens one act — talk/plan: kind chat, how asked_by_person; room: kind seat,
 * how seated (asked_by_person when @mentioned); parent = context.actId or the turn it replies to;
 * thread_id = conversation id; subject_kind "conversation". Never asked_by_agent here.
 */
export type ConverseApi = {
  roomPresets(): Promise<RoomPreset[]>;
  listConversations(projectId: string, o?: { kind?: ConversationKind; limit?: number }): Promise<ConversationSummary[]>;
  createConversation(projectId: string, body: { kind: ConversationKind; seats: string[]; leadRoleKey?: string;
    preset?: string; title?: string; context?: ConversationContext }): Promise<Conversation>;
  getConversation(projectId: string, conversationId: string): Promise<Conversation>;
  sendTurn(projectId: string, conversationId: string, body: { message: string; to?: string[]; rounds?: 1 | 2 },
    signal?: AbortSignal): AsyncIterable<ConverseEvent>;
  summarizeRoom(projectId: string, conversationId: string, signal?: AbortSignal): AsyncIterable<ConverseEvent>;
  proposeStart(projectId: string, conversationId: string, signal?: AbortSignal): AsyncIterable<ConverseEvent>;
  startFromProposal(projectId: string, conversationId: string, proposalId: string,
    o?: { startAnyway?: string[] }): Promise<{ runId: string; conversation: Conversation }>;
  dismissProposal(projectId: string, conversationId: string, proposalId: string): Promise<Conversation>;
  closeConversation(projectId: string, conversationId: string): Promise<Conversation>;
};

/** The room presets every install ships (S-converse serves these from the API). */
export const ROOM_PRESETS: RoomPreset[] = [
  { key: "product_eng_security", label: "Product + Eng + Security", seats: ["pm", "engineering_captain", "security_reviewer"] },
  { key: "architecture_review", label: "Architecture review", seats: ["chief_architect", "backend_engineer", "frontend_engineer", "adversarial_reviewer"] },
  { key: "incident", label: "Incident", seats: ["sre", "operations_engineer", "backend_engineer", "security_reviewer"] },
];

// ── Web entry points (owner in brackets): the real modules, re-exported here. ──────────────

// [kit] Type-only imports there, so these re-exports make no runtime cycle.
export { avatarSvg } from "./agent-avatar";
export { agentIdentity } from "./agent-identity";
export { useAgentIdentities } from "./use-agent-identities";
// [agents] presence, one shared poll.
export { useLivePresence } from "./agents-live";
// [converse] the 1:1 talk from any chip, Passport, run step or palette, and the hook every
// converse surface draws from.
export { openTalk } from "./converse-doors";
export { useConversation } from "./agent-converse";
