"use client";

/**
 * Talking with agents, from the browser: the ConverseApi client, the SSE reader, the doors
 * (`openTalk`, `openPlan`, `openRoom`) and `useConversation`, the hook every converse surface
 * draws from (spec §0.2 A; contract in lib/agent-world-types.ts).
 *
 * Everything goes through our own route, app/api/conversations/[[...path]]/route.ts, which adds
 * the API's auth: the browser cannot reach the API, and EventSource cannot send headers, so the
 * streams are POSTs read as a body (`readEvents`), not EventSources.
 *
 * The wire is snake_case; these are the web shapes. Three fields ride beside the frozen shapes
 * and are typed here, not there: a conversation's `seatInfo` (who each seat is, so a room can
 * name a seat before the identity resolver loads), a proposal's `workflowName`, and a next
 * action's `decisionId` once it has been opened.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { apiWrite } from "./api-write";
import type {
  Conversation, ConversationContext, ConversationKind, ConversationSummary, ConversationTurn,
  ConverseApi, ConverseEvent, ModelFailure, RoomNextAction, RoomPreset, RoomSummary, StartProposal,
} from "./agent-world-types";
import { ROOM_PRESETS } from "./agent-world-types";

// ── Extended web shapes ────────────────────────────────────────────────────────────────────

export type SeatInfo = {
  roleKey: string; name: string; callsign?: string | null; tagline?: string | null;
  agentId?: string | null; lifecycle: string;
};
/** `inputSchema` is the workflow's own start form (its input_schema, either shape), so the card
 *  asks for what the composer's start form asks for (lib/workflow-inputs.ts). */
export type ProposalView = StartProposal & { workflowName?: string; inputSchema?: unknown };
export type NextActionView = RoomNextAction & { decisionId?: string; proposal?: ProposalView };
export type SummaryView = Omit<RoomSummary, "nextActions"> & { nextActions: NextActionView[] };
export type TurnView = Omit<ConversationTurn, "proposal" | "summary"> & {
  proposal?: ProposalView; summary?: SummaryView;
};
export type ConversationView = Omit<Conversation, "turns" | "summary"> & {
  seatInfo: SeatInfo[]; turns: TurnView[]; summary?: SummaryView;
};
export type PresetView = RoomPreset & { missing: string[] };

/** A refusal the console can act on: `code` is needs_model | budget | busy | closed | stale |
 *  invalid | not_found; `readiness` is Start's checklist when a start was refused. */
export class ConverseRefusal extends Error {
  readonly status: number;
  readonly code: string;
  readonly readiness?: { missing?: { key: string; label?: string; fix?: string }[] };
  constructor(status: number, message: string, code = "", readiness?: ConverseRefusal["readiness"]) {
    super(message);
    this.name = "ConverseRefusal";
    this.status = status;
    this.code = code;
    this.readiness = readiness;
  }
}

/** The one sentence a missing model is said with, everywhere (the Studio's NO_MODEL_TIP). */
export const NEEDS_MODEL = "Needs a model. Set one in Settings.";

// ── Mapping the wire ───────────────────────────────────────────────────────────────────────

type Wire = Record<string, unknown>;
const str = (v: unknown): string => (v == null ? "" : String(v));
const opt = (v: unknown): string | undefined => (v == null || v === "" ? undefined : String(v));
const arr = <T = unknown>(v: unknown): T[] => (Array.isArray(v) ? (v as T[]) : []);
const num = (v: unknown): number => (typeof v === "number" ? v : Number(v ?? 0) || 0);

export function mapProposal(w: Wire): ProposalView {
  const readiness = w.readiness as Wire | undefined;
  return {
    proposalId: str(w.proposal_id), workflowKey: opt(w.workflow_key), workClass: opt(w.work_class),
    title: str(w.title), directive: str(w.directive),
    inputs: Object.fromEntries(Object.entries((w.inputs ?? {}) as Wire).map(([k, v]) => [k, str(v)])),
    acceptanceCriteria: arr<string>(w.acceptance_criteria).map(String),
    repository: opt(w.repository),
    agents: arr<Wire>(w.agents).map((a) => ({
      stepId: str(a.step_id), stepLabel: str(a.step_label || a.step_id),
      roleKey: a.role_key ? String(a.role_key) : null })),
    why: str(w.why), status: (str(w.status) || "proposed") as StartProposal["status"],
    runId: opt(w.run_id),
    readiness: readiness ? { ok: Boolean(readiness.ok), problems: arr<string>(readiness.problems).map(String) }
      : undefined,
    workflowName: opt(w.workflow_name),
    ...(w.input_schema && typeof w.input_schema === "object" ? { inputSchema: w.input_schema } : {}),
  };
}

export function mapSummary(w: Wire): SummaryView {
  return {
    text: str(w.text), byRoleKey: str(w.by_role_key),
    decisions: arr<Wire>(w.decisions).map((d) => ({
      text: str(d.text), for: arr<string>(d.for).map(String), against: arr<string>(d.against).map(String) })),
    nextActions: arr<Wire>(w.next_actions).map((a) => ({
      text: str(a.text), ownerRoleKey: a.owner_role_key ? String(a.owner_role_key) : null,
      kind: (["start", "decision", "note"].includes(str(a.kind)) ? str(a.kind) : "note") as RoomNextAction["kind"],
      ...(a.proposal ? { proposal: mapProposal(a.proposal as Wire) } : {}),
      ...(a.decision_id ? { decisionId: String(a.decision_id) } : {}),
    })),
  };
}

export function mapFailure(w: unknown): ModelFailure {
  const f = (w ?? {}) as Wire;
  return { kind: str(f.kind) || "failed", headline: str(f.headline) || "The model call failed.",
           action: str(f.action), href: opt(f.href), retry: Boolean(f.retry) };
}

export function mapTurn(w: Wire): TurnView {
  const kind = (str(w.kind) || "message") as ConversationTurn["kind"];
  const payload = (w.payload ?? {}) as Wire;
  return {
    turnId: str(w.turn_id), seq: num(w.seq),
    speakerKind: (str(w.speaker_kind) || "agent") as ConversationTurn["speakerKind"],
    roleKey: w.role_key ? String(w.role_key) : null, actor: opt(w.actor),
    addressedTo: arr<string>(w.addressed_to).map(String), replyToTurnId: opt(w.reply_to_turn_id),
    kind, content: str(w.content),
    status: (str(w.status) || "complete") as ConversationTurn["status"],
    ...(kind === "proposal" && payload.proposal_id ? { proposal: mapProposal(payload) } : {}),
    ...(kind === "summary" && payload.text ? { summary: mapSummary(payload) } : {}),
    ...(kind === "error" ? { error: mapFailure(payload) } : {}),
    actId: opt(w.act_id), citedActs: arr<string>(w.cited_acts).map(String),
    tokensIn: num(w.tokens_in), tokensOut: num(w.tokens_out), costUsd: num(w.cost_usd),
    receiptId: opt(w.receipt_id), createdAt: str(w.created_at),
  };
}

function mapContext(w: unknown): ConversationContext {
  const c = (w ?? {}) as Wire;
  return { from: (str(c.from) || "chip") as ConversationContext["from"], actId: opt(c.act_id),
           workflowRunId: opt(c.workflow_run_id), stepId: opt(c.step_id) };
}

function mapSeat(w: Wire): SeatInfo {
  return { roleKey: str(w.role_key), name: str(w.name || w.role_key), callsign: opt(w.callsign) ?? null,
           tagline: opt(w.tagline) ?? null, agentId: opt(w.agent_id) ?? null,
           lifecycle: str(w.lifecycle) || "active" };
}

export function mapConversationSummary(w: Wire): ConversationSummary & { seatInfo: SeatInfo[] } {
  const seats = arr<string>(w.seats).map(String);
  return {
    conversationId: str(w.conversation_id), kind: str(w.kind) as ConversationKind, title: str(w.title),
    seats, leadRoleKey: str(w.lead_role_key || seats[0]), preset: opt(w.preset),
    status: (str(w.status) || "open") as ConversationSummary["status"], context: mapContext(w.context),
    startedRunId: opt(w.started_run_id), turnCount: num(w.turn_count), updatedAt: str(w.updated_at),
    seatInfo: arr<Wire>(w.seat_info).map(mapSeat),
  };
}

export function mapConversation(w: Wire): ConversationView {
  return {
    ...mapConversationSummary(w),
    turns: arr<Wire>(w.turns).map(mapTurn),
    ...(w.summary ? { summary: mapSummary(w.summary as Wire) } : {}),
  };
}

export function mapEvent(type: string, d: Wire): ConverseEvent | null {
  switch (type) {
    case "turn_start":
      return { type, turnId: str(d.turn_id), seq: num(d.seq),
               speakerKind: (str(d.speaker_kind) || "agent") as "agent" | "boss",
               roleKey: d.role_key ? String(d.role_key) : null, actId: opt(d.act_id),
               replyToTurnId: opt(d.reply_to_turn_id) };
    case "token":
      return { type, turnId: str(d.turn_id), text: str(d.text) };
    case "proposal":
      return { type, turnId: str(d.turn_id), proposal: mapProposal((d.proposal ?? {}) as Wire) };
    case "summary":
      return { type, turnId: str(d.turn_id), summary: mapSummary((d.summary ?? {}) as Wire) };
    case "turn_end":
      return { type, turnId: str(d.turn_id), status: str(d.status) === "error" ? "error" : "complete",
               tokensIn: num(d.tokens_in), tokensOut: num(d.tokens_out), costUsd: num(d.cost_usd),
               receiptId: opt(d.receipt_id), citedActs: arr<string>(d.cited_acts).map(String) };
    case "error":
      return { type, turnId: opt(d.turn_id), error: mapFailure(d.error) };
    case "done":
      return { type, conversation: mapConversationSummary((d.conversation ?? {}) as Wire) };
    default:
      return null;
  }
}

/** Server-Sent Events from a fetch body: `event:` + `data:` blocks, split on a blank line. */
export async function* readEvents(body: ReadableStream<Uint8Array>): AsyncGenerator<ConverseEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const flush = function* (block: string): Generator<ConverseEvent> {
    let type = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) type = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (!data.length) return;
    try {
      const mapped = mapEvent(type, JSON.parse(data.join("\n")) as Wire);
      if (mapped) yield mapped;
    } catch { /* a malformed block is skipped, never fatal to the room */ }
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let cut = buffer.indexOf("\n\n");
    while (cut !== -1) {
      yield* flush(buffer.slice(0, cut));
      buffer = buffer.slice(cut + 2);
      cut = buffer.indexOf("\n\n");
    }
  }
  if (buffer.trim()) yield* flush(buffer);
}

// ── Transport ──────────────────────────────────────────────────────────────────────────────

const BASE = "/api/conversations";

async function refusal(r: Response): Promise<ConverseRefusal> {
  const body = (await r.json().catch(() => ({}))) as Wire;
  return new ConverseRefusal(r.status, str(body.error) || `HTTP ${r.status}`, str(body.code),
                             body.readiness as ConverseRefusal["readiness"]);
}

async function getJson(path: string, params: Record<string, string>): Promise<Wire> {
  const r = await fetch(`${BASE}${path}?${new URLSearchParams(params)}`, { cache: "no-store" });
  if (!r.ok) throw await refusal(r);
  return (await r.json()) as Wire;
}

async function postJson(path: string, body: Wire, label: string): Promise<Wire> {
  const r = await apiWrite(`${BASE}${path}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  }, { press: { scope: `converse:${path}`, label } });
  if (!r.ok) throw await refusal(r);
  return (await r.json()) as Wire;
}

async function* postStream(path: string, body: Wire, signal?: AbortSignal): AsyncGenerator<ConverseEvent> {
  const r = await apiWrite(`${BASE}${path}`, {
    method: "POST", headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body), signal,
  });
  if (!r.ok || !r.body) throw await refusal(r);
  yield* readEvents(r.body);
}

/** The client every surface uses. `actor` is the signed-in operator — never a literal. */
export function converseApi(actor: string): ConverseApi & {
  presets(projectId: string): Promise<{ items: PresetView[]; agents: SeatInfo[] }>;
  getView(projectId: string, conversationId: string): Promise<ConversationView>;
  openDecision(projectId: string, conversationId: string, turnId: string, index: number): Promise<{ decisionId: string; conversation: ConversationView }>;
} {
  const who = { actor };
  return {
    async roomPresets() { return ROOM_PRESETS; },
    async presets(projectId) {
      const w = await getJson("/presets", { project_id: projectId, actor });
      return { items: arr<Wire>(w.items).map((p) => ({ key: str(p.key), label: str(p.label),
                 seats: arr<string>(p.seats).map(String), missing: arr<string>(p.missing).map(String) })),
               agents: arr<Wire>(w.agents).map(mapSeat) };
    },
    async listConversations(projectId, o = {}) {
      const w = await getJson("", { project_id: projectId, actor, ...(o.kind ? { kind: o.kind } : {}),
                                    limit: String(o.limit ?? 20) });
      return arr<Wire>(w.items).map(mapConversationSummary);
    },
    async createConversation(projectId, body) {
      const w = await postJson("", { project_id: projectId, ...who, kind: body.kind, seats: body.seats,
        lead_role_key: body.leadRoleKey, preset: body.preset, title: body.title,
        context: body.context ? { from: body.context.from, act_id: body.context.actId,
          workflow_run_id: body.context.workflowRunId, step_id: body.context.stepId } : undefined },
        "Open conversation");
      return mapConversation(w) as unknown as Conversation;
    },
    async getConversation(projectId, conversationId) {
      return (await this.getView(projectId, conversationId)) as unknown as Conversation;
    },
    async getView(projectId, conversationId) {
      return mapConversation(await getJson(`/${encodeURIComponent(conversationId)}`,
                                           { project_id: projectId, actor }));
    },
    sendTurn(projectId, conversationId, body, signal) {
      return postStream(`/${encodeURIComponent(conversationId)}/turns`, { project_id: projectId, ...who,
        message: body.message, to: body.to ?? [], rounds: body.rounds ?? 1 }, signal);
    },
    summarizeRoom(projectId, conversationId, signal) {
      return postStream(`/${encodeURIComponent(conversationId)}/summary`, { project_id: projectId, ...who }, signal);
    },
    proposeStart(projectId, conversationId, signal) {
      return postStream(`/${encodeURIComponent(conversationId)}/propose`, { project_id: projectId, ...who }, signal);
    },
    async startFromProposal(projectId, conversationId, proposalId, o = {}) {
      const w = await postJson(`/${encodeURIComponent(conversationId)}/start`, {
        project_id: projectId, ...who, proposal_id: proposalId, start_anyway: o.startAnyway ?? [],
        ...((o as { inputs?: Record<string, string> }).inputs ? { inputs: (o as { inputs?: Record<string, string> }).inputs } : {}),
      }, "Start");
      return { runId: str(w.run_id), conversation: mapConversation((w.conversation ?? {}) as Wire) as unknown as Conversation };
    },
    async dismissProposal(projectId, conversationId, proposalId) {
      return mapConversation(await postJson(
        `/${encodeURIComponent(conversationId)}/proposals/${encodeURIComponent(proposalId)}/dismiss`,
        { project_id: projectId, ...who }, "Dismiss")) as unknown as Conversation;
    },
    async closeConversation(projectId, conversationId) {
      return mapConversation(await postJson(`/${encodeURIComponent(conversationId)}/close`,
        { project_id: projectId, ...who }, "Close")) as unknown as Conversation;
    },
    async openDecision(projectId, conversationId, turnId, index) {
      const w = await postJson(`/${encodeURIComponent(conversationId)}/decisions`,
        { project_id: projectId, ...who, turn_id: turnId, index }, "Open decision");
      return { decisionId: str(w.decision_id), conversation: mapConversation((w.conversation ?? {}) as Wire) };
    },
  };
}

// ── Doors (lib/converse-doors.ts: no React, so server code may import them too) ──────────

export {
  CONVERSE_COMMANDS, CONVERSE_EVENT, openConversation, openPicker, openPlan, openRoom, openTalk,
  type ConverseOpening,
} from "./converse-doors";

/** What a cut-off answer says; the same words the server settles it with. */
export const STOPPED: ModelFailure = { kind: "stopped", headline: "Stopped.", action: "Ask again when ready.", retry: true };

function stoppedTurn(t: TurnView): TurnView {
  return { ...t, kind: "error", status: "error", error: STOPPED };
}

// ── useConversation ────────────────────────────────────────────────────────────────────────

export type LiveState = {
  /** The seat whose words are arriving. */
  speaking: string | null;
  /** Seats answering this turn that have not said a word yet, the first one thinking. */
  thinking: string[];
  /** Seats still to speak this turn, in order (the first may be thinking). */
  queue: string[];
};

/** Where an action's failure is shown: a refusal sentence, or a model failure explained. */
export type ConverseProblem = { message: string; code?: string; failure?: ModelFailure; readiness?: ConverseRefusal["readiness"] };

function problemOf(e: unknown): ConverseProblem {
  if (e instanceof ConverseRefusal) {
    return { message: e.code === "needs_model" ? NEEDS_MODEL : e.message, code: e.code, readiness: e.readiness };
  }
  return { message: String((e as Error)?.message ?? e) };
}

/**
 * One conversation, live. `send`, `summarize` and `propose` stream; while they do, `speaking`
 * names the seat whose words are arriving and `thinking` the seats that have not spoken yet.
 * The frozen contract's `{conversation, speaking, thinking, send}` plus what the surfaces need.
 */
export function useConversation(projectId: string, conversationId: string | null, actor = "") {
  const api = useRef(converseApi(actor));
  api.current = converseApi(actor);
  const [conversation, setConversation] = useState<ConversationView | null>(null);
  const [live, setLive] = useState<LiveState>({ speaking: null, thinking: [], queue: [] });
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<ConverseProblem | null>(null);
  const abort = useRef<AbortController | null>(null);
  const alive = useRef(true);

  /** The server's copy. `stopped`: answers this client cut off, which the server may not have
   *  settled yet — they read as Stopped rather than as still streaming. */
  const reload = useCallback(async (stopped?: Set<string>) => {
    if (!conversationId) { setConversation(null); return; }
    try {
      const fresh = await api.current.getView(projectId, conversationId);
      if (!alive.current) return;
      setConversation(stopped?.size ? { ...fresh, turns: fresh.turns.map((t) =>
        stopped.has(t.turnId) && t.status === "streaming" ? stoppedTurn(t) : t) } : fresh);
    } catch (e) {
      if (alive.current) setProblem(problemOf(e));
    }
  }, [projectId, conversationId]);

  useEffect(() => { void reload(); }, [reload]);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; abort.current?.abort(); };
  }, []);

  const apply = useCallback((event: ConverseEvent) => {
    setConversation((c) => {
      if (!c) return c;
      switch (event.type) {
        case "turn_start": {
          if (c.turns.some((t) => t.turnId === event.turnId)) return c;
          const turn: TurnView = {
            turnId: event.turnId, seq: event.seq, speakerKind: event.speakerKind, roleKey: event.roleKey,
            addressedTo: [], replyToTurnId: event.replyToTurnId, kind: "message", content: "",
            status: "streaming", actId: event.actId, citedActs: [], tokensIn: 0, tokensOut: 0,
            costUsd: 0, createdAt: new Date().toISOString() };
          return { ...c, turns: [...c.turns, turn] };
        }
        case "token":
          return { ...c, turns: c.turns.map((t) => t.turnId === event.turnId
            ? { ...t, content: t.content + event.text } : t) };
        case "proposal":
          return { ...c, turns: c.turns.map((t) => t.turnId === event.turnId
            ? { ...t, kind: "proposal", proposal: event.proposal as ProposalView }
            : t.proposal && t.proposal.status === "proposed"
              ? { ...t, proposal: { ...t.proposal, status: "superseded" } } : t) };
        case "summary":
          return { ...c, summary: event.summary as SummaryView, status: "summarized",
                   turns: c.turns.map((t) => t.turnId === event.turnId
                     ? { ...t, kind: "summary", summary: event.summary as SummaryView } : t) };
        case "turn_end":
          return { ...c, turns: c.turns.map((t) => t.turnId === event.turnId
            ? { ...t, status: event.status, tokensIn: event.tokensIn, tokensOut: event.tokensOut,
                costUsd: event.costUsd, receiptId: event.receiptId, citedActs: event.citedActs } : t) };
        case "error":
          return event.turnId ? { ...c, turns: c.turns.map((t) => t.turnId === event.turnId
            ? { ...t, kind: "error", status: "error", error: event.error } : t) } : c;
        case "done":
          return { ...c, title: event.conversation.title, status: event.conversation.status,
                   turnCount: event.conversation.turnCount };
        default:
          return c;
      }
    });
    setLive((l) => {
      switch (event.type) {
        case "turn_start":
          return { speaking: null, thinking: event.roleKey ? [event.roleKey] : [],
                   queue: l.queue[0] === event.roleKey ? l.queue : l.queue.filter((k) => k !== event.roleKey) };
        case "token": {
          const who = l.thinking[0] ?? l.speaking;
          return { ...l, speaking: who ?? null, thinking: [] };
        }
        case "turn_end":
          return { speaking: null, thinking: [], queue: l.queue.slice(1) };
        case "done":
          return { speaking: null, thinking: [], queue: [] };
        default:
          return l;
      }
    });
    if (event.type === "error" && !event.turnId) setProblem({ message: event.error.headline, failure: event.error });
  }, []);

  const stream = useCallback(async (open: (signal: AbortSignal) => AsyncIterable<ConverseEvent>,
                                    queue: string[], optimistic?: TurnView) => {
    if (busy) return;
    setBusy(true);
    setProblem(null);
    setLive({ speaking: null, thinking: [], queue });
    if (optimistic) setConversation((c) => c ? { ...c, turns: [...c.turns, optimistic] } : c);
    const controller = new AbortController();
    abort.current = controller;
    // Answers this stream began and has not finished: what Stop cuts off.
    const unfinished = new Set<string>();
    try {
      for await (const event of open(controller.signal)) {
        if (event.type === "turn_start") unfinished.add(event.turnId);
        if (event.type === "turn_end") unfinished.delete(event.turnId);
        apply(event);
      }
      await reload();
    } catch (e) {
      if (controller.signal.aborted || (e as Error)?.name === "AbortError") {
        // Stopped. What was said is recorded (the server took the turn before it streamed),
        // so the transcript comes from the server, the cut-off answer reading as Stopped.
        if (!alive.current) return;
        setConversation((c) => c ? { ...c, turns: c.turns.map((t) =>
          unfinished.has(t.turnId) && t.status === "streaming" ? stoppedTurn(t) : t) } : c);
        await reload(unfinished);
      } else {
        if (optimistic) setConversation((c) => c ? { ...c, turns: c.turns.filter((t) => t !== optimistic) } : c);
        setProblem(problemOf(e));
      }
    } finally {
      setBusy(false);
      setLive({ speaking: null, thinking: [], queue: [] });
      abort.current = null;
    }
  }, [busy, apply, reload]);

  const send = useCallback(async (message: string, o: { to?: string[]; rounds?: 1 | 2 } = {}) => {
    if (!conversationId || !conversation) return;
    const said = message.trim();
    const room = conversation.kind === "room";
    const order = room ? (o.to?.length ? conversation.seats.filter((k) => o.to!.includes(k)) : conversation.seats)
      : conversation.seats.slice(0, 1);
    const queue = o.rounds === 2 && room ? [...order, ...order] : order;
    const mine: TurnView | undefined = said ? {
      turnId: `local-${Date.now()}`, seq: conversation.turns.length + 1, speakerKind: "person", roleKey: null,
      actor, addressedTo: o.to ?? [], kind: said.endsWith("?") ? "question" : "message", content: said,
      status: "complete", citedActs: [], tokensIn: 0, tokensOut: 0, costUsd: 0, createdAt: new Date().toISOString(),
    } : undefined;
    await stream((signal) => api.current.sendTurn(projectId, conversationId, { message: said, to: o.to, rounds: o.rounds }, signal),
                 queue, mine);
  }, [projectId, conversationId, conversation, actor, stream]);

  const summarize = useCallback(async () => {
    if (!conversationId || !conversation) return;
    await stream((signal) => api.current.summarizeRoom(projectId, conversationId, signal), [conversation.leadRoleKey]);
  }, [projectId, conversationId, conversation, stream]);

  const propose = useCallback(async () => {
    if (!conversationId || !conversation) return;
    await stream((signal) => api.current.proposeStart(projectId, conversationId, signal), [conversation.leadRoleKey]);
  }, [projectId, conversationId, conversation, stream]);

  const start = useCallback(async (proposalId: string, o: { startAnyway?: string[]; inputs?: Record<string, string> } = {}) => {
    if (!conversationId) throw new Error("no conversation");
    const done = await api.current.startFromProposal(projectId, conversationId, proposalId, o);
    setConversation(done.conversation as unknown as ConversationView);
    return done.runId;
  }, [projectId, conversationId]);

  const dismiss = useCallback(async (proposalId: string) => {
    if (!conversationId) return;
    try {
      setConversation(await api.current.dismissProposal(projectId, conversationId, proposalId) as unknown as ConversationView);
    } catch (e) { setProblem(problemOf(e)); }
  }, [projectId, conversationId]);

  /** A summary's next action, addressed by its summary turn and index. */
  const openDecision = useCallback(async (turnId: string, index: number) => {
    if (!conversationId) return "";
    const done = await api.current.openDecision(projectId, conversationId, turnId, index);
    setConversation(done.conversation);
    return done.decisionId;
  }, [projectId, conversationId]);

  /** Stop the answering now: the seat speaking is cut off (Stopped), the rest do not speak. */
  const stop = useCallback(() => abort.current?.abort(), []);

  return { conversation, speaking: live.speaking, thinking: live.thinking, queue: live.queue, busy,
           problem, setProblem, send, summarize, propose, start, dismiss, openDecision, reload, stop };
}

/** `[act:act_…]` citations in an agent's words, split out so the text reads clean and each
 *  becomes a chip ("I ran this"). Only ids the server confirmed (`citedActs`) become chips. */
export function splitCitations(content: string, cited: string[]): { text: string; acts: string[] } {
  const acts: string[] = [];
  const text = content.replace(/\s?\[act:(act_[A-Za-z0-9_]+)\]/g, (_m, id: string) => {
    if (cited.includes(id) && !acts.includes(id)) acts.push(id);
    return "";
  });
  return { text, acts };
}

/** `@role_key` mentions the operator typed that name a seat. */
export function mentionsIn(message: string, seats: string[]): string[] {
  const found: string[] = [];
  for (const m of message.toLowerCase().matchAll(/@([a-z0-9_]+)/g)) {
    if (seats.includes(m[1]) && !found.includes(m[1])) found.push(m[1]);
  }
  return found;
}
