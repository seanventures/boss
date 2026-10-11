"use client";

/**
 * The run surfaces' reads of the work record: a run's chain and one piece of work in detail.
 *
 * The API speaks snake_case column names (services/agent_world_types.py `Act`); the web speaks
 * the frozen `Act` of lib/agent-world-types.ts. This is the one mapping between them for the run
 * surfaces, so the chain strip, the Act card, the step drawer, Credits and the build ribbon can
 * never read the same field two ways.
 *
 * THE EDGE LABEL IS THE SERVER'S. `edge_label()` is the single source of every How an act shows;
 * nothing here, and nothing that renders an Act, builds one.
 * An act that arrives without one shows no How at all rather than a label guessed in the browser
 * (tests/act-labels-are-truthful.test.tsx).
 *
 * Integration note: lib/agent-world.ts (S-agents) is the shared client for the agent endpoints.
 * When it lands these readers can move there unchanged; the proxy is app/api/run-acts.
 */

import { useEffect, useRef, useState } from "react";

import { useOnScreen } from "../../lib/area-shown";

import type { Chain } from "../../lib/agent-world-types";
import { toActDetail, toChain, type ActDetail } from "./act-wire";

export { toAct, toActDetail, toChain, type ActDetail } from "./act-wire";

type Wire = Record<string, unknown>;

async function getJson(params: Record<string, string>): Promise<Wire | null> {
  try {
    const r = await fetch(`/api/run-acts?${new URLSearchParams(params).toString()}`, { cache: "no-store" });
    if (!r.ok) return null;
    const body = await r.json();
    return body && typeof body === "object" && !(body as Wire).error ? (body as Wire) : null;
  } catch {
    return null;
  }
}

/** A run's chain, or null when the API has none to give (an older install, or no work yet). */
export async function fetchChain(projectId: string, runId: string): Promise<Chain | null> {
  const w = await getJson({ view: "chain", project_id: projectId, run_id: runId });
  return w ? toChain(w) : null;
}

export async function fetchAct(projectId: string, actId: string): Promise<ActDetail | null> {
  const w = await getJson({ view: "act", project_id: projectId, act_id: actId });
  return w ? toActDetail(w) : null;
}

const POLL_MS = 4000;

/**
 * A run's chain, kept fresh while the run is live and the tab is visible.
 *
 * One read per open run, shared by everything the run view draws from it (lane faces, the
 * strip, the drawer, Credits), so they cannot disagree about who did what. A hidden tab is not
 * polled: nobody is watching, and the automation tab is always hidden.
 */
export function useRunChain(projectId: string, runId: string | null, live: boolean): {
  chain: Chain | null; loaded: boolean;
} {
  const [state, setState] = useState<{ id: string; chain: Chain | null } | null>(null);
  const liveRef = useRef(live);
  liveRef.current = live;
  const onScreen = useOnScreen();   // off screen, nothing is asked (lib/area-shown.ts)
  const onScreenRef = useRef(onScreen);
  onScreenRef.current = onScreen;
  useEffect(() => {
    if (!runId) return;
    let alive = true;
    let timer: number | undefined;
    const load = async () => {
      const got = await fetchChain(projectId, runId);
      if (alive) setState({ id: runId, chain: got });
    };
    const tick = () => {
      timer = window.setTimeout(async () => {
        if (!alive) return;
        if (onScreenRef.current) await load();
        if (alive && liveRef.current) tick();
      }, POLL_MS);
    };
    void load().then(() => { if (alive && liveRef.current) tick(); });
    return () => { alive = false; if (timer) window.clearTimeout(timer); };
    // `live` re-arms the poll when a finished run is re-run, and reads once more when it ends.
  }, [projectId, runId, live]);
  const mine = state && state.id === runId ? state : null;
  return { chain: mine?.chain ?? null, loaded: !!mine };
}

/** One act in detail, read once per id. */
export function useActDetail(projectId: string, actId: string | null | undefined): ActDetail | null {
  const [got, setGot] = useState<ActDetail | null>(null);
  useEffect(() => {
    if (!actId) { setGot(null); return; }
    let alive = true;
    void fetchAct(projectId, actId).then((d) => { if (alive) setGot(d); });
    return () => { alive = false; };
  }, [projectId, actId]);
  return got && got.actId === actId ? got : null;
}
