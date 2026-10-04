/**
 * One resolver: any name BOSS gives an agent → one identity (spec §2.1, §6).
 *
 * A panel records "QA / Test", the captain roster `ux_engineer`, a workflow step `qa`, the dock
 * a callsign. Each used to pick its own colour and initials, so one agent was three faces.
 * `agentIdentity()` lands all of them on one role key, then one face, name and callsign:
 *
 *   1. the install's identities from `GET /v1/agents/identities` (persona, custom faces), once loaded;
 *   2. else the shipped cast (lib/avatar-kit/cast.ts);
 *   3. else a seeded face from the key itself — never a blank.
 *
 * The alias table mirrors the engine's `agent_world_types.ALIASES` exactly; a test reads the
 * Python file and fails on any drift, so the two can never disagree about who a seat is.
 */

import type { AgentIdentity, AvatarSpec, Lifecycle } from "./agent-world-types";
import { CAST, CAST_BY_KEY, SEAT_LOOKS } from "./avatar-kit/cast";
import { dict, own } from "./avatar-kit/dict";
import { seededSpec } from "./agent-avatar";

/** Mirror of engine/boss_engine/services/agent_world_types.py ALIASES (tested for equality). */
export const ALIASES: Readonly<Record<string, string>> = {
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
  "vp_product": "pm",
  "ux_engineer": "frontend_engineer",
  "factory_driver": "engineering_captain",
  "reviewer": "adversarial_reviewer",
  "queue_helper": "program_manager",
  "architect": "chief_architect",
  "engineering captain": "engineering_captain", "backend engineer": "backend_engineer",
  "frontend engineer": "frontend_engineer", "operations engineer": "operations_engineer",
  "site reliability engineer": "sre", "chief architect": "chief_architect",
  "software engineer": "software_engineer", "adversarial reviewer": "adversarial_reviewer",
  "security reviewer": "security_reviewer", "qa lead": "qa", "release manager": "release_manager",
  "design critic": "design_critic", "documentation writer": "docs_writer",
  "docs writer": "docs_writer", "finops sentinel": "finops_sentinel",
};

type Wire = {
  role_key?: string; agent_id?: string | null; display_name?: string; callsign?: string | null;
  tagline?: string | null; archetype?: string | null; avatar?: Partial<AvatarSpec> | null; lifecycle?: string | null;
  /** agent_identity.team_of(spec).key — the one team resolver (agents-home spec §6.1). */
  team?: string | null;
};

// ── The store: identities the API returned, keyed by role key ─────────────────────────────

let loaded = new Map<string, AgentIdentity>();
let serverAliases: Readonly<Record<string, string>> = dict([]);
let byName = new Map<string, string>();
let version = 0;
const listeners = new Set<() => void>();
// Loading: which project the store holds (or is fetching), whether that load succeeded, and a
// generation that a reset, a refresh or a project switch bumps so a late response is dropped.
let loadedFor: string | null = null;
let loadedOk = false;
let inflight: Promise<void> | null = null;
let generation = 0;

function rebuildNames() {
  byName = new Map();
  for (const c of CAST) {
    byName.set(c.name.toLowerCase(), c.roleKey);
    if (c.callsign) byName.set(c.callsign.toLowerCase(), c.roleKey);
  }
  for (const i of loaded.values()) {
    byName.set(i.name.toLowerCase(), i.roleKey);
    if (i.callsign) byName.set(i.callsign.toLowerCase(), i.roleKey);
  }
}
rebuildNames();

function bump() {
  version++;
  for (const l of listeners) l();
}

const AVATAR_FIELDS = new Set(["seed", "hat", "emblem", "palette", "visor"]);
const LIFECYCLES: readonly Lifecycle[] = ["active", "paused", "retired"];

/** Load the install's identities (what `/v1/agents/identities` returns). Also the test seam. */
export function primeIdentities(items: Wire[], aliases: Record<string, string> = {}): void {
  const next = new Map<string, AgentIdentity>();
  for (const w of items) {
    const roleKey = (w.role_key ?? "").trim();
    if (!roleKey) continue;
    next.set(roleKey, fromWire(roleKey, w));
  }
  loaded = next;
  serverAliases = dict(Object.entries(aliases).map(([k, v]) => [k.trim().toLowerCase(), v] as const));
  rebuildNames();
  bump();
}

/** Forget what was loaded (tests; a project switch). */
export function resetIdentities(): void {
  generation++;
  retries = 0;
  loadedFor = null;
  loadedOk = false;
  inflight = null;
  clearStore();
}

function clearStore() {
  loaded = new Map();
  serverAliases = dict([]);
  rebuildNames();
  bump();
}

function fromWire(roleKey: string, w: Wire): AgentIdentity {
  const cast = own(CAST_BY_KEY, roleKey);
  // A shipped role keeps its hand-drawn parts unless the operator picked others; a custom agent
  // is seeded from its agent_id, which never changes, so its face never drifts.
  const avatar: AvatarSpec = {
    ...(cast?.avatar ?? seededSpec(w.agent_id || roleKey)),
    ...Object.fromEntries(Object.entries(w.avatar ?? {}).filter(([k, v]) => AVATAR_FIELDS.has(k) && v && typeof v === "string")),
    kit: "boss-v1",
  } as AvatarSpec;
  if (cast && w.avatar?.seed && !w.avatar.hat && !w.avatar.emblem && !w.avatar.palette && !w.avatar.visor) {
    avatar.seed = cast.avatar.seed; // a bare seed on a shipped role is a default, not a reroll
  }
  const lifecycle = LIFECYCLES.includes(w.lifecycle as Lifecycle) ? (w.lifecycle as Lifecycle) : "active";
  return {
    roleKey, agentId: w.agent_id ?? null,
    name: (w.display_name || cast?.name || humanise(roleKey)).trim(),
    ...(w.callsign || cast?.callsign ? { callsign: (w.callsign || cast?.callsign)! } : {}),
    ...(w.tagline || cast?.tagline ? { tagline: (w.tagline || cast?.tagline)! } : {}),
    archetype: w.archetype ?? (cast?.archetype ? roleKey : null),
    avatar, lifecycle,
    ...(w.team ? { team: w.team } : {}),
  };
}

function humanise(key: string): string {
  const words = key.replace(/[_-]+/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : "Agent";
}

/** Key, title, alias or callsign → the role key, or null when nothing knows the name. */
export function resolveRoleKey(keyOrTitle: string): string | null {
  const raw = (keyOrTitle ?? "").trim();
  if (!raw) return null;
  const known = (k: string) => loaded.has(k) || own(CAST_BY_KEY, k) !== undefined;
  if (known(raw)) return raw;
  const low = raw.toLowerCase();
  if (known(low)) return low;
  const aliased = own(serverAliases, low) ?? own(ALIASES, low) ?? byName.get(low);
  if (aliased) return aliased;
  const snake = low.replace(/[\s/&-]+/g, "_").replace(/_+/g, "_");
  if (known(snake)) return snake;
  return own(ALIASES, snake) ?? null;
}

/** Any name → one identity. Never throws and never returns a blank face. */
export function agentIdentity(keyOrTitle: string): AgentIdentity {
  const roleKey = resolveRoleKey(keyOrTitle);
  if (roleKey) {
    const hit = loaded.get(roleKey);
    if (hit) return hit;
    const cast = own(CAST_BY_KEY, roleKey);
    if (cast) {
      return {
        roleKey, agentId: null, name: cast.name,
        ...(cast.callsign ? { callsign: cast.callsign } : {}), ...(cast.tagline ? { tagline: cast.tagline } : {}),
        archetype: cast.archetype ? roleKey : null, avatar: cast.avatar, lifecycle: "active",
      };
    }
  }
  // Unknown to the kit and the install: still one stable face, seeded from the name itself.
  const key = (keyOrTitle ?? "").trim() || "agent";
  return { roleKey: roleKey ?? key, agentId: null, name: humanise(key), archetype: null, avatar: seededSpec(key), lifecycle: "active" };
}

/**
 * A panel seat's face: its agent's face, plus the seat's own emblem and visor when the seat shares
 * that agent with another seat (SEAT_LOOKS). Every seat in a meeting is then a distinct face,
 * while the agent behind it stays the one identity.
 */
export function seatAvatar(seatTitle: string): AvatarSpec {
  const base = agentIdentity(seatTitle).avatar;
  const look = own(SEAT_LOOKS, (seatTitle ?? "").trim().toLowerCase());
  return look ? { ...base, ...look, seed: `${base.seed}#${look.emblem}` } : base;
}

/** True when the kit or the install knows this agent (a test asks it of every alias). */
export function hasKnownFace(keyOrTitle: string): boolean {
  const k = resolveRoleKey(keyOrTitle);
  return Boolean(k && (loaded.has(k) || own(CAST_BY_KEY, k)));
}

function subscribe(l: () => void) {
  listeners.add(l);
  return () => { listeners.delete(l); };
}
const snapshot = () => version;

/** Hear every identity load (useIdentityVersion, in lib/use-agent-identities.ts). */
export function subscribeIdentities(l: () => void): () => void { return subscribe(l); }
/** The store's version: it moves whenever identities load or change. */
export function identityVersion(): number { return snapshot(); }

/** Every identity loaded for the project, by role key (the shipped cast is added by callers). */
export function loadedIdentities(): ReadonlyMap<string, AgentIdentity> { return loaded; }
/** The project the identities were loaded for: what a card opened anywhere reads its facts by. */
export function identitiesProject(): string | null { return loadedFor; }

/**
 * Fetch the install's identities for a project. A load that succeeded is kept until
 * `refreshIdentities`; one that failed is retried on the next call. Switching project clears the
 * old project's identities first, and a response that lands after a switch or a refresh is
 * dropped. Fails soft: the cast still draws every face.
 */
export function loadIdentities(projectId: string, fetcher: typeof fetch = fetch): Promise<void> {
  if (!projectId) return Promise.resolve();
  if (loadedFor === projectId) {
    if (inflight) return inflight;
    if (loadedOk) return Promise.resolve();
  } else {
    generation++;
    retries = 0;
    loadedFor = projectId;
    loadedOk = false;
    inflight = null;
    if (loaded.size || Object.keys(serverAliases).length) clearStore(); // never show A's faces on B
  }
  const mine = generation;
  const p: Promise<void> = (async () => {
    try {
      const r = await fetcher(`/v1/agents/identities?project_id=${encodeURIComponent(projectId)}`, { cache: "no-store" });
      if (!r.ok) { retryLater(projectId, fetcher, mine); return; }
      const data = await r.json() as unknown;
      if (mine !== generation) return; // a switch or refresh came after this request
      const d = data as { items?: Wire[]; identities?: Wire[]; aliases?: Record<string, string> } | Wire[];
      const items = Array.isArray(d) ? d : (d.items ?? d.identities ?? []);
      primeIdentities(Array.isArray(items) ? items : [], Array.isArray(d) ? {} : (d.aliases ?? {}));
      loadedOk = true;
      retries = 0;
    } catch {
      // The API is down, the request was aborted (a navigation), or the API is older than Agent
      // World: the shipped cast is the answer until the retry below lands.
      retryLater(projectId, fetcher, mine);
    }
  })();
  inflight = p;
  void p.then(() => { if (inflight === p) inflight = null; });
  return p;
}

/**
 * A FAILED LOAD IS RETRIED ON ITS OWN (K19). Nothing else asks again once the page is drawn, so
 * an aborted request left every screen on the bundled names — role labels, and callsigns the
 * install had renamed — until a reload. A few retries, backing off, for the same project only.
 */
const RETRY_MS = [1500, 4000, 10000, 30000];
let retries = 0;
function retryLater(projectId: string, fetcher: typeof fetch, mine: number): void {
  if (retries >= RETRY_MS.length) return;
  const wait = RETRY_MS[retries];
  retries += 1;
  setTimeout(() => {
    if (mine !== generation || loadedOk || loadedFor !== projectId) return;
    void loadIdentities(projectId, fetcher);
  }, wait);
}

/** Load a project's identities again, after a persona or face is saved (the Passport calls it). */
export function refreshIdentities(projectId: string, fetcher: typeof fetch = fetch): Promise<void> {
  if (loadedFor === projectId) {
    generation++;
    retries = 0;
    loadedOk = false;
    inflight = null;
  }
  return loadIdentities(projectId, fetcher);
}
