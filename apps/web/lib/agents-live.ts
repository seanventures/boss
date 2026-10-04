/**
 * Who is working right now: `useLivePresence(projectId)` (frozen entry point, spec §12.2).
 *
 * One poll per project, however many components ask: the top-bar pill, the Roster zones and the
 * crew view all read the same store, so they can never disagree about who is working. It polls
 * `/v1/agents/live` every 4s ONLY while the tab is visible (§5.4), with If-None-Match so an
 * unchanged answer is a 304 and no re-render. A missing endpoint (an API from before S-acts)
 * backs off to once a minute rather than hammering a 404.
 */
import { useEffect, useState } from "react";

import type { Presence } from "./agent-world-types";
import { toPresence } from "./agent-world";

export type LivePresence = Presence & { inPanel?: boolean };
export type LiveMap = Map<string, LivePresence>;

export const POLL_MS = 4000;
const MISSING_MS = 60_000;
const EMPTY: LiveMap = new Map();

type Store = {
  value: LiveMap; etag: string | null; listeners: Set<(m: LiveMap) => void>;
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
  if (!hidden() && !store.inflight) {
    store.inflight = true;
    try {
      const r = await fetch(`/v1/agents/live?project_id=${encodeURIComponent(projectId)}`, {
        cache: "no-store", headers: store.etag ? { "If-None-Match": store.etag } : {},
      });
      if (r.status === 404 || r.status === 405) wait = MISSING_MS;
      else if (r.ok) {
        store.etag = r.headers.get("etag");
        const next = parseLive(await r.json().catch(() => []));
        if (!same(next, store.value)) {
          store.value = next;
          for (const l of store.listeners) l(next);
        }
      }
    } catch { /* keep what is on screen; a failed poll is not "nobody is working" */ }
    finally { store.inflight = false; }
  }
  // ONE CHAIN PER STORE. If the chain was restarted while this fetch was out (the tab came
  // back), the restart owns the next timer; scheduling here as well would double the poll.
  if (!store.stopped && gen === store.gen) store.timer = setTimeout(() => void tick(projectId, store, gen), wait);
}

function onVisible(): void {
  if (hidden()) return;
  // Back on the tab: answer now rather than up to 4s later.
  for (const [projectId, store] of stores) {
    if (store.timer) clearTimeout(store.timer);
    store.timer = null;
    store.gen += 1;
    void tick(projectId, store, store.gen);
  }
}

export function subscribeLive(projectId: string, listener: (m: LiveMap) => void): () => void {
  let store = stores.get(projectId);
  if (!store) {
    store = { value: EMPTY, etag: null, listeners: new Set(), timer: null, stopped: false, inflight: false, gen: 0 };
    stores.set(projectId, store);
    if (stores.size === 1 && typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVisible);
    }
    void tick(projectId, store);
  }
  store.listeners.add(listener);
  if (store.value !== EMPTY) listener(store.value);
  const s = store;
  return () => {
    s.listeners.delete(listener);
    if (s.listeners.size) return;
    s.stopped = true;
    if (s.timer) clearTimeout(s.timer);
    stores.delete(projectId);
    if (!stores.size && typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", onVisible);
    }
  };
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

/** For tests: forget every store. */
export function resetLiveStores(): void {
  for (const s of stores.values()) { s.stopped = true; if (s.timer) clearTimeout(s.timer); }
  stores.clear();
}
