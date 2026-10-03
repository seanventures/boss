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
import { useEffect, useMemo, useSyncExternalStore } from "react";

import type { AgentIdentity, AvatarSpec, Lifecycle } from "./agent-world-types";
import { CAST, CAST_BY_KEY } from "./avatar-kit/cast";
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
};

// ── The store: identities the API returned, keyed by role key ─────────────────────────────

let loaded = new Map<string, AgentIdentity>();
let serverAliases: Record<string, string> = {};
let byName = new Map<string, string>();
let version = 0;
const listeners = new Set<() => void>();
let loadedFor: string | null = null;
let inflight: Promise<void> | null = null;

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
  serverAliases = Object.fromEntries(Object.entries(aliases).map(([k, v]) => [k.trim().toLowerCase(), v]));
  rebuildNames();
  bump();
}

/** Forget what was loaded (tests; a project switch). */
export function resetIdentities(): void {
  loaded = new Map();
  serverAliases = {};
  loadedFor = null;
  inflight = null;
  rebuildNames();
  bump();
}

function fromWire(roleKey: string, w: Wire): AgentIdentity {
  const cast = CAST_BY_KEY[roleKey];
  // A shipped role keeps its hand-drawn parts unless the operator picked others; a custom agent
  // is seeded from its agent_id, which never changes, so its face never drifts.
  const avatar: AvatarSpec = {
    ...(cast?.avatar ?? seededSpec(w.agent_id || roleKey)),
    ...Object.fromEntries(Object.entries(w.avatar ?? {}).filter(([k, v]) => v && k !== "kit")),
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
  if (loaded.has(raw) || CAST_BY_KEY[raw]) return raw;
  const low = raw.toLowerCase();
  if (loaded.has(low) || CAST_BY_KEY[low]) return low;
  const aliased = serverAliases[low] ?? ALIASES[low] ?? byName.get(low);
  if (aliased) return aliased;
  const snake = low.replace(/[\s/&-]+/g, "_").replace(/_+/g, "_");
  if (loaded.has(snake) || CAST_BY_KEY[snake]) return snake;
  return ALIASES[snake] ?? null;
}

/** Any name → one identity. Never throws and never returns a blank face. */
export function agentIdentity(keyOrTitle: string): AgentIdentity {
  const roleKey = resolveRoleKey(keyOrTitle);
  if (roleKey) {
    const hit = loaded.get(roleKey);
    if (hit) return hit;
    const cast = CAST_BY_KEY[roleKey];
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

/** True when the kit or the install knows this agent (a test asks it of every alias). */
export function hasKnownFace(keyOrTitle: string): boolean {
  const k = resolveRoleKey(keyOrTitle);
  return Boolean(k && (loaded.has(k) || CAST_BY_KEY[k]));
}

function subscribe(l: () => void) {
  listeners.add(l);
  return () => { listeners.delete(l); };
}
const snapshot = () => version;

/** Re-render when identities load. Every face subscribes, so a callsign or custom face lands everywhere at once. */
export function useIdentityVersion(): number {
  return useSyncExternalStore(subscribe, snapshot, () => 0);
}

/** Fetch the install's identities once per project. Fails soft: the cast still draws every face. */
export function loadIdentities(projectId: string, fetcher: typeof fetch = fetch): Promise<void> {
  if (!projectId) return Promise.resolve();
  if (loadedFor === projectId && inflight) return inflight;
  loadedFor = projectId;
  inflight = (async () => {
    try {
      const r = await fetcher(`/v1/agents/identities?project_id=${encodeURIComponent(projectId)}`, { cache: "no-store" });
      if (!r.ok) return;
      const data = await r.json() as unknown;
      const d = data as { items?: Wire[]; identities?: Wire[]; aliases?: Record<string, string> } | Wire[];
      const items = Array.isArray(d) ? d : (d.items ?? d.identities ?? []);
      primeIdentities(Array.isArray(items) ? items : [], Array.isArray(d) ? {} : (d.aliases ?? {}));
    } catch {
      // The API is down or older than Agent World: the shipped cast is the answer.
    }
  })();
  return inflight;
}

/** The identities for a project, loading them on first use. */
export function useAgentIdentities(projectId: string): Map<string, AgentIdentity> {
  const v = useIdentityVersion();
  useEffect(() => { void loadIdentities(projectId); }, [projectId]);
  return useMemo(() => {
    const out = new Map<string, AgentIdentity>();
    for (const c of CAST) out.set(c.roleKey, agentIdentity(c.roleKey));
    for (const [k, i] of loaded) out.set(k, i);
    return out;
  }, [v]); // eslint-disable-line react-hooks/exhaustive-deps -- `loaded` is the store `v` versions
}
