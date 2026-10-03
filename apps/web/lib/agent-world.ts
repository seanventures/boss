/**
 * Agent World, the console's half (S-agents): the API client for identity, acts, lineage,
 * presence, persona, test and card, plus the safe edges onto the other streams' entry points.
 *
 * Every call goes through the same-origin proxy (app/v1/[...path]/route.ts), which attaches the
 * key on the server and streams SSE straight through. Writes go through apiWrite so a double
 * press is one request. The API speaks snake_case; everything returned here is the frozen web
 * shape from lib/agent-world-types.ts, mapped once, here, so no component reads a wire field.
 *
 * The face, the cast and the identity resolver are the kit's (lib/agent-avatar.ts,
 * lib/agent-identity.ts, lib/avatar-kit/): nothing here draws or names an agent its own way. The
 * 1:1 talk (openTalk) belongs to S-converse and throws until it lands, so it is called through a
 * wrapper that falls back to the existing side dock.
 */
import { apiWrite } from "./api-write";
import { openDockAbout } from "./dock-subject";
import { avatarSvg, seededSpec } from "./agent-avatar";
import { agentIdentity } from "./agent-identity";
import { CAST, CAST_BY_KEY } from "./avatar-kit/cast";
import {
  openTalk,
  type Act, type ActHow, type ActKind, type ActStatus, type AgentIdentity, type AvatarSpec,
  type AvatarState, type AvatarVariant, type ConversationContext, type LineageGraph,
  type Passport, type Persona, type Presence, type Voice,
} from "./agent-world-types";

export type {
  Act, ActHow, ActKind, AgentIdentity, AvatarSpec, AvatarState, LineageGraph, Passport, Persona,
  Presence, Voice,
};

const enc = encodeURIComponent;

// ── The cast ────────────────────────────────────────────────────────────────────────────────

/** The hand-designed cast, in the kit's order: the §0.1 crew the operator named first
 *  (engineering captain, backend, frontend, operations, SRE, …), the "thinks like" archetypes last. */
export const CAST_KEYS: readonly string[] = CAST.filter((c) => c.designed === "hand").map((c) => c.roleKey);

/** The "thinks like" archetypes: inspired by a way of thinking, never a person (§0.1.3). */
export const ARCHETYPE_KEYS: readonly string[] = CAST.filter((c) => c.archetype).map((c) => c.roleKey);

export const DISCLOSURE = "AI agent · not a real person";
export const AI_TIP = "An AI agent. Not a real person.";

// ── Identity and face (the kit) ─────────────────────────────────────────────────────────────

const ACRONYMS: Record<string, string> = {
  qa: "QA", cto: "CTO", coo: "COO", cao: "CAO", ux: "UX", vp: "VP", pm: "PM", sre: "SRE", mcp: "MCP",
};
export function humanise(key: string): string {
  return (key || "").split(/[_\s-]+/).filter(Boolean)
    .map((w) => ACRONYMS[w.toLowerCase()] ?? (w.charAt(0).toUpperCase() + w.slice(1)))
    .join(" ");
}

/** One identity for a role key, title or name: the kit's resolver (lib/agent-identity.ts). */
export function identityOf(keyOrTitle: string, name?: string): AgentIdentity {
  const found = agentIdentity(keyOrTitle);
  return name && !found.name ? { ...found, name } : found;
}

/** The face as SVG markup: the kit's one drawing function. */
export function faceMarkup(spec: AvatarSpec, size: number, state?: AvatarState, variant?: AvatarVariant): string {
  return avatarSvg(spec, { size, state, variant });
}

const PARTS = ["hat", "emblem", "palette", "visor"] as const;
/** True when an avatar names a part, not just a seed. */
export function hasParts(a: Partial<AvatarSpec> | null | undefined): boolean {
  return !!a && PARTS.some((k) => !!a[k]);
}

/**
 * An agent's face from its role and its stored avatar, the way the kit resolves an identity:
 * a shipped role's hand-drawn face (a custom agent's seeded one), with the stored parts laid
 * over it only when the operator picked some. A bare seed on a shipped role is a default, not
 * a choice, so Vera is still Vera on a passport that only carries `{seed: agt_…}`.
 */
export function resolveAvatar(roleKey: string, agentId: string | null | undefined,
                              stored?: Partial<AvatarSpec> | null): AvatarSpec {
  const cast = CAST_BY_KEY[roleKey];
  const base = cast?.avatar ?? seededSpec(agentId || roleKey);
  if (!stored || (cast && !hasParts(stored))) return base;
  const picked = Object.fromEntries(Object.entries(stored).filter(([k, v]) => v && k !== "kit"));
  return { ...base, ...picked, kit: "boss-v1" } as AvatarSpec;
}

/** Talk to an agent. The 1:1 conversation when S-converse is there; the side dock, addressed to
 *  that agent by role key, until then (openTalk is still the contract's stub). */
export function askAgent(roleKey: string, name: string, context?: ConversationContext): void {
  try {
    openTalk(roleKey, context);
    return;
  } catch { /* converse has not landed */ }
  openDockAbout({ subject: null, role: name || roleKey, question: `@${name || roleKey}: ` });
}

// ── State words (spec §9) ───────────────────────────────────────────────────────────────────

export const STATE_WORD: Record<AvatarState, string> = {
  idle: "Idle", thinking: "Thinking", speaking: "Speaking", working: "Working",
  needs_you: "Needs you", done: "Done", stuck: "Stuck", paused: "Paused", retired: "Retired",
};

/** Roster zones, in screen order. Stuck sits with Needs you: both wait on a person. */
export type Zone = "needs_you" | "working" | "in_panel" | "idle";
export const ZONES: { key: Zone; label: string }[] = [
  { key: "needs_you", label: "NEEDS YOU" },
  { key: "working", label: "WORKING" },
  { key: "in_panel", label: "IN A PANEL" },
  { key: "idle", label: "IDLE" },
];

/** Presence comes from the API's `state`; `in_panel` is an open seat act and arrives as its own
 *  state word, which AvatarState draws as speaking. */
export function zoneOf(p: Presence | undefined, inPanel = false): Zone {
  if (!p) return "idle";
  if (p.state === "needs_you" || p.state === "stuck") return "needs_you";
  if (inPanel) return "in_panel";
  if (p.state === "working" || p.state === "thinking" || p.state === "speaking") return "working";
  return "idle";
}

export function isLive(p: Presence | undefined): boolean {
  return !!p && p.state !== "idle" && p.state !== "done" && p.state !== "paused" && p.state !== "retired";
}

// ── Wire → web mapping ──────────────────────────────────────────────────────────────────────

type Wire = Record<string, unknown>;
const str = (v: unknown, d = ""): string => (typeof v === "string" ? v : v == null ? d : String(v));
const num = (v: unknown, d = 0): number => {
  const n = typeof v === "number" ? v : typeof v === "string" ? Number(v) : NaN;
  return Number.isFinite(n) ? n : d;
};
const opt = (v: unknown): string | undefined => (typeof v === "string" && v ? v : undefined);
const arr = <T = unknown>(v: unknown): T[] => (Array.isArray(v) ? (v as T[]) : []);
const obj = (v: unknown): Wire => (v && typeof v === "object" && !Array.isArray(v) ? (v as Wire) : {});

const AVATAR_STATES = new Set<AvatarState>(["idle", "thinking", "speaking", "working", "needs_you",
                                            "done", "stuck", "paused", "retired"]);
/** The API's presence words onto the face's states. `in_panel` is a live seat: speaking. */
export function toAvatarState(s: unknown): AvatarState {
  const v = str(s);
  if (v === "in_panel") return "speaking";
  if (v === "awaiting_input") return "needs_you";
  if (v === "error" || v === "stale") return "stuck";
  return AVATAR_STATES.has(v as AvatarState) ? (v as AvatarState) : "idle";
}

export function toPresence(w: unknown): Presence & { inPanel?: boolean } {
  const o = obj(w);
  return {
    state: toAvatarState(o.state), actId: opt(o.act_id ?? o.actId), line: str(o.line),
    since: opt(o.since), inPanel: str(o.state) === "in_panel",
  };
}

const LINKS = ["workflow_run_id", "step_id", "code_task_run_id", "meeting_run_id", "thread_id",
               "decision_id", "subject_kind", "subject_id"];

export function toAct(w: unknown): Act & { from?: { kind: string; id?: string; roleKey?: string } } {
  const o = obj(w);
  const handed = obj(o.handed_by);
  const links: Record<string, string> = { ...(obj(o.links) as Record<string, string>) };
  for (const k of LINKS) if (typeof o[k] === "string" && o[k]) links[k] = o[k] as string;
  const scope = obj(o.scope);
  const from = o.from ? obj(o.from) : null;
  return {
    actId: str(o.act_id ?? o.actId), roleKey: (o.role_key ?? o.roleKey ?? null) as string | null,
    kind: str(o.kind, "step") as ActKind, status: str(o.status, "active") as ActStatus,
    parentActId: opt(o.parent_act_id), rootActId: str(o.root_act_id ?? o.act_id),
    handedBy: {
      kind: (str(handed.kind ?? o.handed_by_kind, "boss") as "person" | "agent" | "boss"),
      id: opt(handed.id ?? o.handed_by_id),
    },
    how: str(o.how, "named") as ActHow,
    // THE API'S LABEL, NEVER OURS: edge_label() is the single source (spec §9, test 13).
    edgeLabel: str(o.edge_label ?? o.edgeLabel),
    why: opt(o.why),
    considered: arr<unknown>(o.considered).map((c) => (typeof c === "string" ? c : str(obj(c).role_key))),
    scope: { skills: arr<string>(scope.skills).map(String), mcp: arr<string>(scope.mcp).map(String) },
    scopeOk: typeof o.scope_ok === "boolean" ? o.scope_ok : undefined,
    accountable: str(o.accountable), label: str(o.label), links,
    artifacts: arr<Wire>(o.artifacts).map((a) => ({ kind: str(a.kind), ref: str(a.ref), url: opt(a.url) })),
    tokensIn: num(o.tokens_in), tokensOut: num(o.tokens_out), costUsd: num(o.cost_usd),
    reconstructed: o.reconstructed === true, startedAt: str(o.started_at), endedAt: opt(o.ended_at),
    version: o.identity_version != null ? num(o.identity_version) : undefined,
    ...(from ? { from: { kind: str(from.kind), id: opt(from.id), roleKey: opt(from.role_key) } } : {}),
  };
}

function toPersona(w: unknown): Persona {
  const o = obj(w);
  const voice = o.voice ? obj(o.voice) : null;
  const avatar = o.avatar ? obj(o.avatar) : null;
  return {
    version: 1, archetype: (o.archetype ?? null) as string | null,
    callsign: opt(o.callsign), tagline: opt(o.tagline), about_me: opt(o.about_me),
    ...(voice ? { voice: { blunt: num(voice.blunt, 2), thorough: num(voice.thorough, 2),
                           bold: num(voice.bold, 2), casual: num(voice.casual, 2) } } : {}),
    phrases: arr<string>(o.phrases).map(String), never: arr<string>(o.never).map(String),
    ...(avatar ? { avatar: toAvatar(avatar) } : {}),
  };
}

function toAvatar(w: unknown, seed = ""): AvatarSpec {
  const o = obj(w);
  return {
    kit: "boss-v1", seed: str(o.seed, seed) || seed, hat: opt(o.hat), emblem: opt(o.emblem),
    palette: opt(o.palette), visor: opt(o.visor),
  };
}

export function toPassport(w: unknown): Passport {
  const o = obj(w);
  const born = obj(o.born);
  const vitals = obj(o.vitals);
  const persona = toPersona(o.persona);
  const agentId = str(o.agent_id);
  // A face counts only when it is a choice: parts picked, or a custom agent's own (re)seed. A
  // bare seed on a shipped role is a default (the kit draws the cast face for it), and keeping it
  // in the persona would write it back on the next Save of any voice field.
  const roleKey = str(o.role_key);
  if (CAST_BY_KEY[roleKey] && persona.avatar && !hasParts(persona.avatar)) delete persona.avatar;
  if (!persona.avatar && o.avatar && (hasParts(obj(o.avatar)) || !CAST_BY_KEY[roleKey])) {
    persona.avatar = toAvatar(o.avatar, agentId);
  }
  return {
    agentId, roleKey, displayName: str(o.display_name),
    persona,
    born: { at: str(born.at), by: str(born.by), from: obj(born.from) },
    sponsor: opt(o.sponsor), lifecycle: (str(o.lifecycle, "active") as Passport["lifecycle"]),
    version: num(o.version, 1), fingerprint: str(o.fingerprint), reportsTo: opt(o.reports_to),
    vitals: {
      trust: num(vitals.trust), runs: num(vitals.runs), cleanRate: num(vitals.clean_rate),
      cost7d: num(vitals.cost_7d), panels: num(vitals.panels), apps: num(vitals.apps),
    },
    presence: toPresence(o.presence),
    identity: { mode: "local", tokens: false },
  };
}

export function toLineage(w: unknown): LineageGraph {
  const o = obj(w);
  return {
    nodes: arr<Wire>(o.nodes).map((n) => ({
      id: str(n.id), kind: (str(n.kind, "agent") as "agent" | "person" | "boss"), roleKey: opt(n.role_key),
    })),
    edges: arr<Wire>(o.edges).map((e) => ({
      from: str(e.from), to: str(e.to), acts: num(e.acts, 1), howTop: str(e.how_top, "named") as ActHow,
      label: str(e.label), lastAt: str(e.last_at), reconstructed: e.reconstructed === true,
    })),
  };
}

// ── Reads ───────────────────────────────────────────────────────────────────────────────────

export type Result<T> = { ok: true; data: T } | { ok: false; status: number; error: string };

/** The API's sentence, whichever shape it came in: `detail` string, `{field, message}`, a
 *  pydantic list, or `{code, message}`. */
export function sentence(body: unknown, status: number): string {
  const b = obj(body);
  const d = b.detail ?? b.error ?? b;
  if (typeof d === "string" && d.trim()) return d;
  const dd = obj(d);
  if (typeof dd.message === "string" && dd.message) return dd.message;
  if (Array.isArray(d) && d.length) return d.map((x) => str(obj(x).msg ?? x)).join("; ");
  if (status === 403) return "Not allowed";
  if (status === 404) return "Not found";
  return `HTTP ${status}`;
}

/** The field a 422 names, when it names one: `{detail: {field, message}}` or `{field, message}`. */
export function fieldOf(body: unknown): string | undefined {
  const b = obj(body);
  return opt(obj(b.detail).field) ?? opt(b.field);
}

async function getJson<T>(path: string, map: (w: unknown) => T): Promise<Result<T>> {
  try {
    const r = await fetch(path, { cache: "no-store" });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, status: r.status, error: sentence(body, r.status) };
    return { ok: true, data: map(body) };
  } catch (e) {
    return { ok: false, status: 0, error: String((e as Error)?.message ?? e) };
  }
}

export function getPassport(projectId: string, roleKey: string): Promise<Result<Passport>> {
  return getJson(`/v1/agents/${enc(roleKey)}/passport?project_id=${enc(projectId)}`, toPassport);
}

export type ActsPage = { items: ReturnType<typeof toAct>[]; next: string | null };
export function listActs(projectId: string, roleKey: string,
                         o: { kind?: ActKind | ""; days?: number; cursor?: string | null } = {}): Promise<Result<ActsPage>> {
  const q = new URLSearchParams({ project_id: projectId });
  if (o.kind) q.set("kind", o.kind);
  if (o.days) q.set("days", String(o.days));
  if (o.cursor) q.set("cursor", o.cursor);
  return getJson(`/v1/agents/${enc(roleKey)}/acts?${q}`, (w) => ({
    items: arr(obj(w).items).map(toAct), next: opt(obj(w).next) ?? null,
  }));
}

export function getLineage(projectId: string, roleKey: string,
                           o: { days: number; mode: "observed" | "declared" }): Promise<Result<LineageGraph>> {
  return getJson(`/v1/agents/${enc(roleKey)}/lineage?project_id=${enc(projectId)}&days=${o.days}`
    + `&mode=${o.mode}`, toLineage);
}

/** The A2A card, exactly as the API serves it: Copy card JSON copies these bytes. */
export function getAgentCard(projectId: string, roleKey: string): Promise<Result<Wire>> {
  return getJson(`/v1/agents/${enc(roleKey)}/card?project_id=${enc(projectId)}`, obj);
}

export type RunChainLite = { acts: ReturnType<typeof toAct>[] };
export function getRunChain(workflowRunId: string, projectId: string): Promise<Result<RunChainLite>> {
  return getJson(`/v1/workflow-runs/${enc(workflowRunId)}/chain?project_id=${enc(projectId)}`,
                 (w) => ({ acts: arr(obj(w).acts).map(toAct) }));
}

/** One act, as `/v1/acts/{act_id}` returns it. */
export function getAct(projectId: string, actId: string): Promise<Result<ReturnType<typeof toAct>>> {
  return getJson(`/v1/acts/${enc(actId)}?project_id=${enc(projectId)}`, toAct);
}

/** Who really handed this act its work: the agent named on it, else its parent act's agent.
 *  null when the work came from a person or BOSS, or the parent is unknown — never a guess. */
export async function handedByRole(projectId: string, actId: string): Promise<string | null> {
  const r = await getAct(projectId, actId);
  if (!r.ok) return null;
  const a = r.data;
  if (a.from?.kind === "agent" && a.from.roleKey) return a.from.roleKey;
  if (a.handedBy.kind === "agent" && a.handedBy.id) return a.handedBy.id;
  if (!a.parentActId) return null;
  const parent = await getAct(projectId, a.parentActId);
  return parent.ok && parent.data.roleKey ? parent.data.roleKey : null;
}

/** The project's workflow runs, most recently active first (ids of `workflow_runs`, which is
 *  what `/v1/workflow-runs/{id}/chain` is keyed by). */
export function listRecentRunIds(projectId: string, limit = 5): Promise<Result<string[]>> {
  return getJson(`/v1/delivery-workflows/runs?project_id=${enc(projectId)}&limit=${limit}`,
                 (w) => arr<Wire>(obj(w).runs).map((r) => str(r.id)).filter(Boolean));
}

export type Memory = { id: string; body: string; source?: string };
/** "What I remember": this agent's role-scoped memories, newest use first, at most 5.
 *  INTEGRATION: /v1/memory must pass `role` through to recall() for these to appear. */
export function listRoleMemories(projectId: string, actor: string, roleKey: string): Promise<Result<Memory[]>> {
  return getJson(`/v1/memory?project_id=${enc(projectId)}&actor=${enc(actor)}&role=${enc(roleKey)}`
    + "&include_proposed=false", (w) => arr<Wire>(obj(w).memories)
    .filter((m) => m.scope === "role" && str(m.scope_ref).toLowerCase() === roleKey.toLowerCase()
      && str(m.status, "active") === "active")
    .slice(0, 5)
    .map((m) => ({ id: str(m.id), body: str(m.body), source: opt(m.source_thread_id) })));
}

// ── Writes ──────────────────────────────────────────────────────────────────────────────────

async function write<T>(path: string, method: "POST" | "PATCH", body: Wire, map: (w: unknown) => T,
                        press: { scope: string; label: string }): Promise<Result<T> & { body?: unknown }> {
  try {
    const r = await apiWrite(path, {
      method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }, { press });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, status: r.status, error: sentence(data, r.status), body: data };
    return { ok: true, data: map(data) };
  } catch (e) {
    return { ok: false, status: 0, error: String((e as Error)?.message ?? e) };
  }
}

export type Sealed = { version: number; fingerprint: string; warnings: string[] };

/** Save the voice. The API lints it (422 `Persona can't change what the agent may do`), seals a
 *  new version only when something changed, and warns on a real person's name. */
export async function savePersona(projectId: string, actor: string, roleKey: string, persona: Persona):
  Promise<Result<Sealed> & { field?: string }> {
  const r = await write(`/v1/agents/${enc(roleKey)}`, "PATCH", { project_id: projectId, actor, persona },
    (w) => ({ version: num(obj(w).version, 1), fingerprint: str(obj(w).fingerprint),
              warnings: arr(obj(w).warnings).map((x) => (typeof x === "string" ? x : str(obj(x).message))) }),
    { scope: `agent-${roleKey}-persona`, label: "Save voice" });
  return r.ok ? r : { ...r, field: fieldOf(r.body) };
}

export function setLifecycle(projectId: string, actor: string, roleKey: string,
                             state: Passport["lifecycle"], reason = ""): Promise<Result<Passport>> {
  return write(`/v1/agents/${enc(roleKey)}/lifecycle`, "POST",
    { project_id: projectId, actor, state, reason }, toPassport,
    { scope: `agent-${roleKey}-lifecycle`, label: state === "active" ? "Resume" : state === "paused" ? "Pause" : "Retire" });
}

/** One model call in the agent's own voice. Not saved: it fills the field for the operator to
 *  keep or change. `persona` lets an unsaved draft be heard. */
export function draftPersonaField(projectId: string, actor: string, roleKey: string,
                                  field: "about_me" | "tagline", persona?: Persona): Promise<Result<string>> {
  return write(`/v1/agents/${enc(roleKey)}/persona/draft`, "POST",
    { project_id: projectId, actor, field, ...(persona ? { persona } : {}) },
    (w) => str(obj(w).draft), { scope: `agent-${roleKey}-draft`, label: "Draft it" });
}

export function forgetMemory(projectId: string, actor: string, memoryId: string): Promise<Result<unknown>> {
  return write(`/v1/memory/${enc(memoryId)}/forget`, "POST", { project_id: projectId, actor },
               (w) => w, { scope: `memory-${memoryId}`, label: "Forget" });
}

// ── Test (SSE) ──────────────────────────────────────────────────────────────────────────────

export type Probe = "intro" | "push_back" | "out_of_scope";
export type TestResult = { voice: number; inRemit: boolean; saysAi: boolean; fingerprint: string };
export type TestEvent =
  | { type: "token"; text: string }
  | { type: "result"; result: TestResult }
  | { type: "error"; message: string; code?: string };

/** Server-sent events, from a fetch body: `event:` names the type, or the data carries `type`. */
export async function* readSse(res: Response): AsyncGenerator<{ event: string; data: unknown }> {
  const reader = res.body?.getReader();
  if (!reader) return;
  const decoder = new TextDecoder();
  let buf = "";
  const flush = function* (block: string) {
    let event = "message";
    const lines: string[] = [];
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) lines.push(line.slice(5).replace(/^ /, ""));
    }
    if (!lines.length) return;
    const raw = lines.join("\n");
    let data: unknown = raw;
    try { data = JSON.parse(raw); } catch { /* plain text data */ }
    yield { event, data };
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let cut: number;
    while ((cut = buf.search(/\r?\n\r?\n/)) >= 0) {
      const block = buf.slice(0, cut);
      buf = buf.slice(cut).replace(/^\r?\n\r?\n/, "");
      yield* flush(block);
    }
  }
  if (buf.trim()) yield* flush(buf);
}

export async function* runPersonaTest(projectId: string, actor: string, roleKey: string, probe: Probe,
                                      persona?: Persona, signal?: AbortSignal): AsyncGenerator<TestEvent> {
  let res: Response;
  try {
    res = await apiWrite(`/v1/agents/${enc(roleKey)}/test`, {
      method: "POST", signal, cache: "no-store",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ project_id: projectId, actor, probe, ...(persona ? { persona } : {}) }),
    }, { press: { scope: `agent-${roleKey}-test`, label: "Test" } });
  } catch (e) {
    yield { type: "error", message: String((e as Error)?.message ?? e) };
    return;
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    yield { type: "error", message: sentence(body, res.status), code: opt(obj(obj(body).detail).code ?? obj(body).code) };
    return;
  }
  for await (const { event, data } of readSse(res)) {
    const d = obj(data);
    const kind = event !== "message" ? event : str(d.type);
    if (kind === "token") yield { type: "token", text: typeof data === "string" ? data : str(d.text ?? d.token) };
    else if (kind === "result") {
      const r = obj(d.result ?? d);
      yield { type: "result", result: { voice: num(r.voice), inRemit: r.in_remit === true,
                                        saysAi: r.says_ai === true, fingerprint: str(r.fingerprint) } };
    } else if (kind === "error") {
      const err = obj(d.error ?? d);
      yield { type: "error", message: str(err.headline ?? err.message ?? "The model call failed"), code: opt(err.code ?? err.kind) };
    }
  }
}

// ── Small shared helpers ────────────────────────────────────────────────────────────────────

/** A fresh face seed for a new agent's Reroll: 10 base32 chars, like an agent_id's tail. */
export function newSeed(): string {
  const abc = "0123456789abcdefghjkmnpqrstvwxyz";
  const bytes = new Uint8Array(10);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => abc[b % 32]).join("");
}

/** `agt_7f3k…` for toasts and tight places. */
export function shortId(agentId: string): string {
  return agentId.length > 8 ? `${agentId.slice(0, 8)}…` : agentId;
}

/** `v4 · 9f3a·c21e`: the version, and the fingerprint as two groups of four. */
export function versionLine(version: number, fingerprint: string): string {
  const f = (fingerprint || "").replace(/^sha256:/, "");
  return f ? `v${version} · ${f.slice(0, 4)}·${f.slice(4, 8)}` : `v${version}`;
}

export function money(n: number): string {
  return n >= 1 || n === 0 ? `$${n.toFixed(2)}` : `$${n.toFixed(3)}`;
}

/** `3 Oct 2026`, in UTC, so server and browser render the same text. */
export function bornDate(iso: string): string {
  const d = new Date(iso);
  if (!iso || Number.isNaN(d.getTime())) return "—";
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${d.getUTCDate()} ${months[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

/** `From`: where the agent came from, in the passport's words. */
export function bornFromLabel(from: Record<string, unknown>): string {
  const kind = str(from.kind);
  if (kind === "pack") return from.module_version ? `BOSS pack v${str(from.module_version)}` : "BOSS pack";
  if (kind === "template") return `Template: ${humanise(str(from.template_key))}`;
  if (kind === "archetype") return `Archetype: ${identityOf(str(from.archetype)).name}`;
  if (kind === "clone") return "Copy";
  return "Custom";
}

export const VOICE_AXES: { key: keyof Voice; low: string; high: string }[] = [
  { key: "blunt", low: "Gentle", high: "Blunt" },
  { key: "thorough", low: "Brief", high: "Thorough" },
  { key: "bold", low: "Cautious", high: "Bold" },
  { key: "casual", low: "Formal", high: "Casual" },
];

/** A persona with every optional field present, for an editor to bind to. The avatar is left
 *  as stored — absent stays absent — so saving a voice never writes a face nobody picked. */
export function fullPersona(p?: Partial<Persona> | null): Persona {
  return {
    version: 1, archetype: p?.archetype ?? null, callsign: p?.callsign ?? "", tagline: p?.tagline ?? "",
    about_me: p?.about_me ?? "",
    voice: { blunt: 2, thorough: 2, bold: 2, casual: 2, ...(p?.voice ?? {}) },
    phrases: [...(p?.phrases ?? [])], never: [...(p?.never ?? [])],
    ...(p?.avatar ? { avatar: { ...p.avatar, kit: "boss-v1" as const } } : {}),
  };
}

/** Only what the operator filled: empty strings and lists are left out of what is sent. */
export function compactPersona(p: Persona): Persona {
  const out: Persona = { version: 1, archetype: p.archetype ?? null };
  if (p.callsign?.trim()) out.callsign = p.callsign.trim();
  if (p.tagline?.trim()) out.tagline = p.tagline.trim();
  if (p.about_me?.trim()) out.about_me = p.about_me.trim();
  if (p.voice) out.voice = p.voice;
  if (p.phrases?.length) out.phrases = p.phrases;
  if (p.never?.length) out.never = p.never;
  if (p.avatar) {
    const a: AvatarSpec = { kit: "boss-v1", seed: p.avatar.seed };
    if (p.avatar.hat) a.hat = p.avatar.hat;
    if (p.avatar.emblem) a.emblem = p.avatar.emblem;
    if (p.avatar.palette) a.palette = p.avatar.palette;
    if (p.avatar.visor) a.visor = p.avatar.visor;
    out.avatar = a;
  }
  return out;
}

/** The decorative passport strip: `BOSS<AGENT<<AGT_7F3K2Q4M<<BOSS-V1<<V4<<<`. Not an id format. */
export function mrz(agentId: string, version: number, name: string): string[] {
  const clean = (s: string) => s.toUpperCase().replace(/[^A-Z0-9]+/g, "<");
  const line1 = `BOSS<AGENT<<${clean(name)}`.padEnd(44, "<").slice(0, 44);
  const line2 = `${clean(agentId)}<<BOSS-V1<<V${version}`.padEnd(44, "<").slice(0, 44);
  return [line1, line2];
}

export function toast(type: "success" | "error", message: string): void {
  try {
    window.dispatchEvent(new CustomEvent("boss:toast", { detail: { type, message } }));
  } catch { /* no window */ }
}
