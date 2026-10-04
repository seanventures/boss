/**
 * Who is working right now: `useLivePresence(projectId)` (frozen entry point, spec §12.2).
 *
 * One poll per project, however many components ask: the top-bar pill, the Roster zones and the
 * crew view all read the same store, so they can never disagree about who is working. It polls
 * `/v1/agents/live` every 4s ONLY while the tab is visible (§5.4), with If-None-Match so an
 * unchanged answer is a 304 and no re-render. A missing endpoint (an API from before S-acts)
 * backs off to once a minute rather than hammering a 404.
 *
 * WHAT NEEDS YOU RIDES THE SAME POLL (agents-home spec §6.3). The answer carries `attention`
 * — the count every other surface shows (command_center's needs_you), with up to five items —
 * so the pill counts decisions, not agents: three gates on one agent are three. An API from
 * before that sends none, and `useAttention` is null: the pill then falls back to agents.
 * The console's Live/Paused chip stops the poll; a change made anywhere (`onChanged`) asks now.
 */
import { useEffect, useState } from "react";

import type { Presence } from "./agent-world-types";
import { toPresence } from "./agent-world";
import { parseAttentionSummary, type AttentionSummary } from "./agents-live-board";
import { isConsoleLive, subscribeConsoleLive } from "./console-live";
import { onChanged } from "../app/changed";

export type LivePresence = Presence & { inPanel?: boolean };
export type LiveMap = Map<string, LivePresence>;

export const POLL_MS = 4000;
const MISSING_MS = 60_000;
const EMPTY: LiveMap = new Map();

type Store = {
  value: LiveMap; etag: string | null; listeners: Set<(m: LiveMap) => void>;
  attention: AttentionSummary | null; attentionListeners: Set<(a: AttentionSummary | null) => void>;
  timer: ReturnType<typeof setTimeout> | null; stopped: boolean; inflight: boolean;
  /** Bumped whenever the chain is restarted. A tick schedules its successor only while its own
   *  generation is current, so a restart during a pending fetch can never leave two chains. */
  gen: number;
};
const stores = new Map<string, Store>();

/** The wire answer onto a map: `[...]` or `{items: [...]}`, keyed by role_key. */
export function parseLive(body: unknown): LiveMap {
  const items = Array.isArray(body) ? body
    : Array.isArray((body as { items?: unknown })?.items) ? (body as { items: unknown[] }).items : [];
  const out: LiveMap = new Map();
  for (const item of items) {
    const key = String((item as { role_key?: unknown })?.role_key ?? "");
    if (key) out.set(key, toPresence(item));
  }
  return out;
}

/** The pill's slice of what needs you, or null when the API sends none (an older API). */
export function parseLiveAttention(body: unknown): AttentionSummary | null {
  const raw = (body as { attention?: unknown } | null)?.attention;
  return raw && typeof raw === "object" ? parseAttentionSummary(raw) : null;
}

function sameAttention(a: AttentionSummary | null, b: AttentionSummary | null): boolean {
  if (!a || !b) return a === b;
  return a.count === b.count && a.items.length === b.items.length
    && a.items.every((x, i) => x.id === b.items[i].id && x.title === b.items[i].title
                     && x.roleKey === b.items[i].roleKey && x.decideInline === b.items[i].decideInline);
}

function same(a: LiveMap, b: LiveMap): boolean {
  if (a.size !== b.size) return false;
  for (const [k, v] of a) {
    const w = b.get(k);
    if (!w || w.state !== v.state || w.line !== v.line || w.actId !== v.actId || w.inPanel !== v.inPanel) return false;
  }
  return true;
}

const hidden = () => typeof document !== "undefined" && document.visibilityState === "hidden";

async function tick(projectId: string, store: Store, gen = store.gen): Promise<void> {
  if (store.stopped || gen !== store.gen) return;
  let wait = POLL_MS;
  if (!hidden() && isConsoleLive() && !store.inflight) {
    store.inflight = true;
    try {
      const r = await fetch(`/v1/agents/live?project_id=${encodeURIComponent(projectId)}`, {
        cache: "no-store", headers: store.etag ? { "If-None-Match": store.etag } : {},
      });
      if (r.status === 404 || r.status === 405) wait = MISSING_MS;
      else if (r.ok) {
        store.etag = r.headers.get("etag");
        const body = await r.json().catch(() => []);
        const next = parseLive(body);
        if (!same(next, store.value)) {
          store.value = next;
          for (const l of store.listeners) l(next);
        }
        const attention = parseLiveAttention(body);
        if (!sameAttention(attention, store.attention)) {
          store.attention = attention;
          for (const l of store.attentionListeners) l(attention);
        }
      }
    } catch { /* keep what is on screen; a failed poll is not "nobody is working" */ }
    finally { store.inflight = false; }
  }
  // ONE CHAIN PER STORE. If the chain was restarted while this fetch was out (the tab came
  // back), the restart owns the next timer; scheduling here as well would double the poll.
  if (!store.stopped && gen === store.gen) store.timer = setTimeout(() => void tick(projectId, store, gen), wait);
}

let unsubscribeChanged: (() => void) | null = null;
let unsubscribePause: (() => void) | null = null;

/** Ask every store now, not at its next tick: back on the tab, Live pressed again, or a
 *  decision made anywhere in the console. */
export function refreshLive(): void {
  if (hidden() || !isConsoleLive()) return;
  for (const [projectId, store] of stores) {
    if (store.timer) clearTimeout(store.timer);
    store.timer = null;
    store.gen += 1;
    void tick(projectId, store, store.gen);
  }
}

function onVisible(): void {
  refreshLive();
}

function storeFor(projectId: string): Store {
  let store = stores.get(projectId);
  if (!store) {
    store = { value: EMPTY, etag: null, listeners: new Set(), attention: null, attentionListeners: new Set(),
              timer: null, stopped: false, inflight: false, gen: 0 };
    stores.set(projectId, store);
    if (stores.size === 1) {
      if (typeof document !== "undefined") document.addEventListener("visibilitychange", onVisible);
      unsubscribeChanged = onChanged(() => refreshLive());
      unsubscribePause = subscribeConsoleLive((on) => { if (on) refreshLive(); });
    }
    void tick(projectId, store);
  }
  return store;
}

function release(projectId: string, s: Store): void {
  if (s.listeners.size || s.attentionListeners.size) return;
  s.stopped = true;
  if (s.timer) clearTimeout(s.timer);
  stores.delete(projectId);
  if (!stores.size) {
    if (typeof document !== "undefined") document.removeEventListener("visibilitychange", onVisible);
    unsubscribeChanged?.(); unsubscribeChanged = null;
    unsubscribePause?.(); unsubscribePause = null;
  }
}

export function subscribeLive(projectId: string, listener: (m: LiveMap) => void): () => void {
  const store = storeFor(projectId);
  store.listeners.add(listener);
  if (store.value !== EMPTY) listener(store.value);
  return () => { store.listeners.delete(listener); release(projectId, store); };
}

/** What needs you, from the same poll: `attention` on /v1/agents/live. */
export function subscribeAttention(projectId: string,
                                   listener: (a: AttentionSummary | null) => void): () => void {
  const store = storeFor(projectId);
  store.attentionListeners.add(listener);
  if (store.attention) listener(store.attention);
  return () => { store.attentionListeners.delete(listener); release(projectId, store); };
}

/** Presence by role_key. Empty until the first answer, and on a server render. */
export function useLivePresence(projectId: string): LiveMap {
  const [value, setValue] = useState<LiveMap>(EMPTY);
  useEffect(() => {
    if (!projectId) return undefined;
    return subscribeLive(projectId, setValue);
  }, [projectId]);
  return value;
}

/** What needs you — the pill's count and up to five items. Null until the first answer, and
 *  for an API that sends no `attention`. */
export function useAttention(projectId: string): AttentionSummary | null {
  const [value, setValue] = useState<AttentionSummary | null>(null);
  useEffect(() => {
    if (!projectId) return undefined;
    return subscribeAttention(projectId, setValue);
  }, [projectId]);
  return value;
}

/** For tests: forget every store. */
export function resetLiveStores(): void {
  for (const s of stores.values()) { s.stopped = true; if (s.timer) clearTimeout(s.timer); }
  stores.clear();
  unsubscribeChanged?.(); unsubscribeChanged = null;
  unsubscribePause?.(); unsubscribePause = null;
  if (typeof document !== "undefined") document.removeEventListener("visibilitychange", onVisible);
}
