"use client";

/**
 * THE FLOOR (command-center spec §4.7): the landing page's hero. Your agents in a ring of teams,
 * You at the centre.
 *
 *   every agent          its real face (AgFace), sized and lit by what it is doing (floorKinds):
 *                        working and waiting-on-you largest, on a run next, worked in the last
 *                        six hours brighter than at rest, never used small and dim
 *   a hand-off           a soft arc woven through the centre, weighted by volume, faded by age
 *                        (agent_acts started in the last six hours, known hands only)
 *   a NEW hand-off       a comet along that arc, once, the poll after the ledger records it
 *   a run in flight      its chain of faces joined in order (missions[].hops), ending at You in
 *                        amber when it waits on you: a busy day shows its runs even when every
 *                        agent has finished its step and presence says idle
 *   a decision for you   an amber dashed thread from the agent that asked, to You (count badge)
 *   stopped work         a red solid thread
 *
 * EVERY MARK IS REAL. Edges are `flows[]`, chains `missions[]`, threads `attention[]` items that
 * name their agent, faces `agents[]` presence and `since`. Nothing here moves
 * unless one of those changed between two polls, and nothing moves at all when the console is
 * Paused, the tab is hidden, BOSS is unreachable or the host is struggling (`motion="none"`).
 *
 * ONE LOOP. Comets share one requestAnimationFrame loop that exists only while a comet flies
 * (≤ 4 at once, 150ms apart, ≤ 12 per poll; the rest only light their edges). Comet dots are
 * drawn imperatively into one <g>, so a frame costs no React render.
 */
import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { openAgent } from "../../lib/agent-world";
import type { AvatarState } from "../../lib/agent-world-types";
import type { BoardAgent } from "../../lib/agents-live-board";
import {
  arcPath, chainsOf, edgeCurve, edgeWidth, floorCounts, floorKinds, floorLayout, freshFlows, quadAt,
  recencyOpacity, COMET_CAP, KIND_SIZE, FACE_KINDS, fitArcLabel, idleDot, placeNames,
  type FloorKind, type FloorLayout,
} from "../../lib/command-center-graphics";
import { openTalk } from "../../lib/converse-doors";
import { boardOf, type CommandCenterView, type Flow, type MotionMode } from "../../lib/command-center-view";
import { AgFace } from "../agents/agent-face";
import { ANIMATED_STATES, RING_BUDGET, ago, linkTo, navigate, plainClick, runPatch, useNow, type Band } from "../agents/live-util";
import { TeamZones, faceStateOf, teamHue } from "../agents/team-zones";

/** SVG coordinates to 1/100 px: the server and the browser print a float's last digits
 *  differently, and one digit is a hydration mismatch (seen on the walk). */
const q = (n: number): number => Math.round(n * 100) / 100;

/** useLayoutEffect in the browser, nothing on the server (which has no layout to read). */
const useIsoLayoutEffect = typeof window !== "undefined" ? useLayoutEffect : useEffect;

export type FloorProps = {
  view: CommandCenterView;
  projectId: string;
  motion: MotionMode;
  /** Why motion is off when it is (`Motion paused while the host struggles`), for You's tooltip. */
  motionWhy?: string | null;
  band: Band;
  orgId?: string | null;
  /** Press You: scroll to Needs you. */
  onYou: () => void;
  /** Press a thread: focus that attention card. */
  onThread: (attentionId: string) => void;
};

/** The stage before it is measured (and on the server): the 1440px layout's box. */
export const STAGE = { width: 720, height: 500 };
/** Where BOSS sits, above You. */
const BOSS_DY = -64;
const COMET_MS = 600;
const COMET_GAP_MS = 150;
const COMET_FLYING_MAX = 4;
const ARRIVE_MS = 400;
const LIT_MS = 1000;
const STOPPED = new Set(["failed_step", "gate_override", "budget_refused"]);
/** Past this many agents every face shrinks a step. */
const SMALL_FACES = 48;

type Pt = { x: number; y: number };
type Edge = { key: string; flow: Flow; a: Pt; b: Pt; c: Pt; d: string; fromName: string; toName: string };
type Thread = { key: string; roleKey: string; tone: "amber" | "red"; ids: string[]; titles: string[]; a: Pt };

const isIdle = (a: BoardAgent) => a.state === "idle" || a.state === "done" || a.state === "paused" || a.state === "retired";
const short = (a: BoardAgent | undefined, fallback: string) => a?.callsign || a?.name || fallback;

/** The stage's real size, measured once mounted (and on resize). */
function useStageSize(ref: React.RefObject<HTMLDivElement | null>): { width: number; height: number } {
  const [size, setSize] = useState(STAGE);
  useIsoLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const read = () => {
      const w = Math.round(el.clientWidth), h = Math.round(el.clientHeight);
      if (w > 0 && h > 0) setSize((s) => (s.width === w && s.height === h ? s : { width: w, height: h }));
    };
    read();
    if (typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return size;
}

/** `Forge → Vera · 3 hand-offs · 4m ago`. */
export function edgeTip(fromName: string, toName: string, n: number, lastAt: string, nowMs: number): string {
  const age = ago(lastAt, nowMs);
  return `${fromName} → ${toName} · ${n} hand-off${n === 1 ? "" : "s"}${age ? ` · ${age} ago` : ""}`;
}

/** The footer's words (§4.7): what the team is doing, counted from floorKinds — so a busy day
 *  whose runs all wait on you says `5 on runs`, not `0 working · 32 idle`. Each part is its own
 *  short run of words. */
export function floorFooter(view: CommandCenterView, nowMs: number | null):
    { text: string; parts: string[]; tip?: string } {
  const now = nowMs ?? Date.parse(view.generatedAt);
  const kinds = floorKinds(view, now);
  const c = floorCounts(kinds);
  if (c.working === 0 && c.onRuns === 0 && view.flows.length === 0) {
    if (!view.today.everRan) {
      const text = "Your team is ready";
      return { text, parts: [text], tip: `${view.agents.length} agents, idle until work starts` };
    }
    const idle = view.agents.filter(isIdle);
    const last = idle.map((a) => Date.parse(a.since ?? "")).filter(Number.isFinite);
    const latest = last.length ? new Date(Math.max(...last)).toISOString() : view.today.lastWorkAt;
    const age = ago(latest, now);
    const text = age ? `All idle · last work ${age}` : "All idle";
    return { text, parts: age ? ["All idle", `last work ${age}`] : ["All idle"] };
  }
  const parts = [`${c.working} working`, ...(c.onRuns ? [`${c.onRuns} on runs`] : []),
                 ...(c.stuck ? [`${c.stuck} stuck`] : []), `${c.idle} idle`];
  return { text: parts.join(" · "), parts,
           tip: c.onRuns ? "On runs: their run is in flight or waiting on you" : undefined };
}

export function Floor({ view, projectId, motion, motionWhy, band, orgId, onYou, onThread }: FloorProps) {
  const [list, setList] = useState(false);
  const clock = useNow(30_000);
  const nowMs = clock ?? Date.parse(view.generatedAt);
  const footer = floorFooter(view, clock);

  if (view.agents.length === 0) {
    return (
      <div className="cc-floor-body" data-testid="cc-floor-empty">
        <div className="cc-floor-none">
          <span>No agents yet</span>
          <a className="ag-link" href={linkTo({ area: "workforce", view: null, role: null }, orgId)}
             onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate({ area: "workforce", view: null, role: null }); } }}>
            Agents ↗
          </a>
        </div>
      </div>
    );
  }

  const compact = band === "narrow" || band === "phone";
  return (
    <div className="cc-floor-body" data-band={band}>
      {compact ? (
        <div className="cc-floor-compact" data-testid="cc-floor-compact" data-still={motion !== "full" || undefined}>
          <TeamZones projectId={projectId} board={boardOf(view)} density="compact"
                     animateMax={motion === "full" ? RING_BUDGET : 0}
                     onOpenMap={() => navigate({ area: "workforce", view: "organization", role: null })} />
        </div>
      ) : list ? (
        <FloorList view={view} visible onThread={onThread} />
      ) : (
        <Ring view={view} motion={motion} motionWhy={motionWhy} nowMs={nowMs} onYou={onYou} onThread={onThread} />
      )}
      {!list && !compact ? <FloorList view={view} visible={false} onThread={onThread} /> : null}
      <footer className="cc-floor-foot">
        <span className="cc-floor-count" title={footer.tip} data-testid="cc-floor-footer">
          {footer.parts.map((p, i) => (
            <React.Fragment key={p}>{i ? <span className="cc-sep" aria-hidden> · </span> : null}<span>{p}</span></React.Fragment>
          ))}
        </span>
        <span className="ag-spacer" />
        {compact ? null : (
          <button type="button" className="cc-floor-toggle" aria-pressed={list} data-testid="cc-floor-list-toggle"
                  title={list ? "Show the map" : "Show as a list"} onClick={() => setList((v) => !v)}>
            {list ? "Map" : "List"}
          </button>
        )}
      </footer>
    </div>
  );
}

/** The same facts as the ring, as a list: who is working, and every thread to You. Hidden
 *  (for screen readers) beside the map; shown instead of it when `List` is pressed. */
function FloorList({ view, visible, onThread }: { view: CommandCenterView; visible: boolean; onThread: (id: string) => void }) {
  // Who is on the floor right now: working, waiting on you, stuck, or on a run in flight.
  const kinds = floorKinds(view, Date.parse(view.generatedAt));
  const working = view.agents.filter((a) => ["working", "waiting", "stuck", "on_run"].includes(kinds.get(a.roleKey) ?? ""));
  const names = new Map(view.agents.map((a) => [a.roleKey, a]));
  const threads = view.attention.filter((a) => a.roleKey && (a.kind === "approval" || STOPPED.has(a.kind)));
  return (
    <ul className={visible ? "cc-floor-list" : "cc-floor-list ag-sr wf-sr"}
        data-testid={visible ? "cc-floor-list" : "cc-floor-sr-list"} aria-label="Team map as a list">
      {working.map((a) => (
        <li key={a.roleKey}>
          <button type="button" className="cc-floor-li" onClick={() => openAgent(a.roleKey)} title={a.line || undefined}>
            <AgFace roleKey={a.roleKey} size={20} state={faceOf(a, kinds.get(a.roleKey) ?? "never")} decorative />
            <span>{short(a, a.roleKey)}</span>
            <span className="cc-floor-li-k">{KIND_WORD[kinds.get(a.roleKey) ?? "never"]}</span>
          </button>
        </li>
      ))}
      {threads.map((t) => (
        <li key={t.id}>
          <button type="button" className="cc-floor-li" data-tone={t.kind === "approval" ? "amber" : "red"}
                  onClick={() => onThread(t.id)} title={t.title}>
            <span className="cc-floor-li-dot" aria-hidden />
            <span>{short(names.get(t.roleKey ?? ""), "An agent")}</span>
            <span className="cc-floor-li-k">{t.kind === "approval" ? "asks you" : "stopped"}</span>
          </button>
        </li>
      ))}
      {visible && working.length === 0 && threads.length === 0 ? <li className="cc-floor-li-none">All idle</li> : null}
    </ul>
  );
}

// ── The ring ────────────────────────────────────────────────────────────────────────────────

type Comet = { key: string; to: string; a: Pt; c: Pt; b: Pt; t0: number; el: SVGGElement | null };

/** The face each kind shows: the real state where presence has one, otherwise the kind's. */
function faceOf(a: BoardAgent, kind: FloorKind): AvatarState {
  if (kind === "waiting") return "needs_you";
  if (kind === "stuck") return "stuck";
  if (kind === "working") return faceStateOf(a);
  return "idle";
}

const KIND_WORD: Record<FloorKind, string> = {
  working: "Working", waiting: "Waiting on you", stuck: "Stuck", on_run: "On a run",
  recent: "Worked recently", rested: "Resting", never: "No work yet",
};

/** A chain's path through its faces: each step bows toward the centre, the last may run to You. */
function chainPath(pts: Pt[], centre: Pt): string {
  if (!pts.length) return "";
  let d = `M ${pts[0].x.toFixed(1)} ${pts[0].y.toFixed(1)}`;
  for (let i = 1; i < pts.length; i += 1) {
    const { c } = edgeCurve(pts[i - 1], pts[i], centre, 0.32);
    d += ` Q ${c.x} ${c.y} ${pts[i].x.toFixed(1)} ${pts[i].y.toFixed(1)}`;
  }
  return d;
}

function Ring({ view, motion, motionWhy, nowMs, onYou, onThread }: {
  view: CommandCenterView; motion: MotionMode; motionWhy?: string | null; nowMs: number;
  onYou: () => void; onThread: (id: string) => void;
}) {
  const stageRef = useRef<HTMLDivElement>(null);
  const size = useStageSize(stageRef);
  // THE MAP DEPENDS ON WHO IS IN WHICH TEAM, NEVER ON STATE: a key over roster and teams only.
  const rosterKey = view.agents.map((a) => `${a.roleKey}:${a.team}`).sort().join("|");
  const layout: FloorLayout = useMemo(() => floorLayout(view.teams, view.agents, size),
    [rosterKey, size.width, size.height]); // eslint-disable-line react-hooks/exhaustive-deps -- state never moves a node
  const centre = { x: layout.cx, y: layout.cy };
  const bossAt = { x: layout.cx, y: layout.cy + BOSS_DY };
  const at = useMemo(() => new Map(layout.nodes.map((n) => [n.roleKey, n])), [layout]);
  const byKey = useMemo(() => new Map(view.agents.map((a) => [a.roleKey, a])), [view.agents]);
  const kinds = useMemo(() => floorKinds(view, nowMs), [view, nowMs]);
  const total = view.agents.length;
  // Past 48 agents every face shrinks a step, so the ring keeps its gaps.
  const scale = total > SMALL_FACES ? 0.8 : 1;
  const sizeOf = (k: FloorKind) => Math.round(KIND_SIZE[k] * scale);

  // Edges: a hand-off is drawn only between two nodes that are on the map — an unknown hander
  // is never drawn to a guessed node (the server drops it too).
  const edges: Edge[] = [];
  for (const f of view.flows) {
    const from = f.fromKind === "person" ? centre : f.fromKind === "boss" ? bossAt : at.get(f.from);
    const to = at.get(f.to);
    if (!from || !to) continue;
    const { d, c } = edgeCurve(from, to, centre, 0.62);
    edges.push({ key: `${f.fromKind}:${f.from}>${f.to}`, flow: f, a: from, b: to, c, d,
                 fromName: f.fromKind === "person" ? "You" : f.fromKind === "boss" ? "BOSS" : short(byKey.get(f.from), f.from),
                 toName: short(byKey.get(f.to), f.to) });
  }
  const handoffs = view.flows.reduce((n, f) => n + f.n, 0);
  const chains = chainsOf(view.missions, new Set(at.keys())).slice(0, 16);
  const current = new Set(chains.map((c) => c.keys[c.keys.length - 1]));

  // Threads to You: one per agent and tone, with a count past one.
  const threads: Thread[] = [];
  for (const item of view.attention) {
    const tone = item.kind === "approval" ? "amber" : STOPPED.has(item.kind) ? "red" : null;
    if (!tone || !item.roleKey) continue;
    const node = at.get(item.roleKey);
    if (!node) continue;
    const key = `${item.roleKey}:${tone}`;
    const t = threads.find((x) => x.key === key);
    if (t) { t.ids.push(item.id); t.titles.push(item.title); } else {
      threads.push({ key, roleKey: item.roleKey, tone, ids: [item.id], titles: [item.title], a: node });
    }
  }

  // ── What changed since the last poll: new pairs draw in, new acts fly, new threads fade in.
  const prevFlows = useRef<Flow[] | null>(null);
  const seenPairs = useRef<Set<string> | null>(null);
  const seenThreads = useRef<Set<string> | null>(null);
  const seenAwake = useRef<Set<string> | null>(null);
  const [lit, setLit] = useState<Set<string>>(new Set());
  const newPairs = useMemo(() => {
    const prev = seenPairs.current;
    const keys = edges.map((e) => e.key);
    const out = prev === null || motion !== "full" ? new Set<string>() : new Set(keys.filter((k) => !prev.has(k)));
    seenPairs.current = new Set(keys);
    return out;
  }, [view.flows, motion]); // eslint-disable-line react-hooks/exhaustive-deps -- per poll
  const newThreads = useMemo(() => {
    const prev = seenThreads.current;
    const keys = threads.map((t) => t.key);
    const out = prev === null || motion === "none" ? new Set<string>() : new Set(keys.filter((k) => !prev.has(k)));
    seenThreads.current = new Set(keys);
    return out;
  }, [view.attention, motion]); // eslint-disable-line react-hooks/exhaustive-deps -- per poll
  const woke = useMemo(() => {
    const prev = seenAwake.current;
    const awake = [...kinds].filter(([, k]) => k === "working" || k === "waiting").map(([r]) => r);
    const out = prev === null || motion === "none" ? new Set<string>() : new Set(awake.filter((k) => !prev.has(k)));
    seenAwake.current = new Set(awake);
    return out;
  }, [view.agents, view.missions, motion]); // eslint-disable-line react-hooks/exhaustive-deps -- per poll

  // ── Comets: one loop, alive only while one flies.
  const cometLayer = useRef<SVGGElement>(null);
  const nodeEls = useRef(new Map<string, HTMLElement>());
  const engine = useRef<{ queue: Comet[]; flying: Comet[]; raf: number | null; last: number }>(
    { queue: [], flying: [], raf: null, last: 0 });
  const edgesRef = useRef(edges);
  edgesRef.current = edges;

  useEffect(() => {
    const fresh = freshFlows(prevFlows.current, view.flows);
    prevFlows.current = view.flows;
    if (!fresh.length || motion === "none") return;
    const keyOf = (f: Flow) => `${f.fromKind}:${f.from}>${f.to}`;
    const flyable = motion === "full" ? fresh.slice(0, COMET_CAP) : [];
    const light = (motion === "full" ? fresh.slice(COMET_CAP) : fresh).map(keyOf);
    if (light.length) {
      setLit((s) => new Set([...s, ...light]));
      window.setTimeout(() => setLit((s) => new Set([...s].filter((k) => !light.includes(k)))), LIT_MS);
    }
    for (const f of flyable) {
      const e = edgesRef.current.find((x) => x.key === keyOf(f));
      if (!e) continue;
      engine.current.queue.push({ key: e.key, to: f.to, a: e.a, b: e.b, c: e.c, t0: 0, el: null });
    }
    start();
  }, [view.flows]); // eslint-disable-line react-hooks/exhaustive-deps -- per poll

  // Motion off: whatever was flying lands now, and nothing new launches.
  useEffect(() => {
    if (motion === "full") return;
    stop();
  }, [motion]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => stop(), []); // eslint-disable-line react-hooks/exhaustive-deps

  function stop() {
    const en = engine.current;
    if (en.raf !== null && typeof cancelAnimationFrame !== "undefined") cancelAnimationFrame(en.raf);
    en.raf = null;
    for (const c of en.flying) c.el?.remove();
    en.flying = [];
    en.queue = [];
  }

  function start() {
    const en = engine.current;
    if (en.raf !== null || !en.queue.length || typeof requestAnimationFrame === "undefined") return;
    en.raf = requestAnimationFrame(frame);
  }

  function frame(now: number) {
    const en = engine.current;
    const layer = cometLayer.current;
    if (!layer) { en.raf = null; return; }
    while (en.queue.length && en.flying.length < COMET_FLYING_MAX && now - en.last >= COMET_GAP_MS) {
      const c = en.queue.shift()!;
      c.t0 = now;
      en.last = now;
      const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
      g.setAttribute("class", "cc-comet");
      g.setAttribute("data-comet", "");
      const tail = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      tail.setAttribute("r", "11");
      tail.setAttribute("class", "cc-comet-glow");
      const head = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      head.setAttribute("r", "3.5");
      head.setAttribute("class", "cc-comet-head");
      g.append(tail, head);
      g.setAttribute("transform", `translate(${c.a.x} ${c.a.y})`);
      layer.appendChild(g);
      c.el = g;
      en.flying.push(c);
    }
    en.flying = en.flying.filter((c) => {
      const t = Math.min(1, (now - c.t0) / COMET_MS);
      const eased = 1 - (1 - t) * (1 - t);
      const p = quadAt(c.a, c.c, c.b, eased);
      c.el?.setAttribute("transform", `translate(${p.x.toFixed(1)} ${p.y.toFixed(1)})`);
      if (t < 1) return true;
      c.el?.remove();
      const node = nodeEls.current.get(c.to);
      if (node) {
        node.setAttribute("data-arrive", "true");
        window.setTimeout(() => node.removeAttribute("data-arrive"), ARRIVE_MS);
      }
      return false;
    });
    en.raf = en.flying.length || en.queue.length ? requestAnimationFrame(frame) : null;
  }

  // ── The mini card: hover, focus or tap a face. Esc or leaving closes it.
  const [card, setCard] = useState<{ key: string; pinned: boolean } | null>(null);
  const closeTimer = useRef<number | null>(null);
  const show = (key: string, pinned = false) => {
    if (closeTimer.current) { window.clearTimeout(closeTimer.current); closeTimer.current = null; }
    setCard((c) => (c && c.key === key && c.pinned && !pinned ? c : { key, pinned }));
  };
  const hide = (soon = true) => {
    if (closeTimer.current) window.clearTimeout(closeTimer.current);
    if (!soon) { setCard(null); return; }
    closeTimer.current = window.setTimeout(() => setCard((c) => (c?.pinned ? c : null)), 160);
  };
  useEffect(() => () => { if (closeTimer.current) window.clearTimeout(closeTimer.current); }, []);

  // ── Keyboard: arrows go round the ring, Enter opens the Passport, Esc leaves.
  const order = layout.nodes.map((n) => n.roleKey);
  const [active, setActive] = useState(0);
  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape" && card) { setCard(null); return; }
    const key = (e.target as HTMLElement).dataset?.role;
    const i = key ? order.indexOf(key) : -1;
    if (i < 0) return;
    let next = i;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = (i + 1) % order.length;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = (i - 1 + order.length) % order.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = order.length - 1;
    else if (e.key === "Enter") { e.preventDefault(); openAgent(order[i]); return; }
    else if (e.key === "Escape") { (e.target as HTMLElement).blur(); return; }
    else return;
    e.preventDefault();
    setActive(next);
    nodeEls.current.get(order[next])?.focus();
  };

  // THE RING BUDGET: at most eight faces move; the rest hold still and say their state in a word.
  let budget = motion === "full" ? RING_BUDGET : 0;
  const stillOf = new Map<string, boolean>();
  for (const n of layout.nodes) {
    const a = byKey.get(n.roleKey);
    if (!a) continue;
    const moves = ANIMATED_STATES.has(faceOf(a, kinds.get(n.roleKey) ?? "never"));
    stillOf.set(n.roleKey, !moves || budget <= 0);
    if (moves && budget > 0) budget -= 1;
  }

  const youTone = view.counts.stopped > 0 ? "red" : view.counts.needsYou > 0 ? "amber" : "calm";
  const youTip = motion === "none" && motionWhy ? motionWhy
    : view.counts.needsYou > 0 ? `${view.counts.needsYou} ${view.counts.needsYou === 1 ? "needs" : "need"} you` : "You";
  const counts = floorCounts(kinds);
  const label = `Team map: ${counts.working} working, ${counts.onRuns} on runs, `
    + `${view.counts.needsYou} waiting on you, `
    + `${handoffs} hand-off${handoffs === 1 ? "" : "s"} in the last hour`;
  const fold = total > 120;
  const { width: W, height: H, radius: R } = layout;
  // NAMES ONLY ON ACTIVE FACES, and never on top of each other or another face (D9/D16).
  const RANK: Partial<Record<FloorKind, number>> = { working: 0, waiting: 1, stuck: 2, on_run: 3 };
  const names = placeNames(layout.nodes.flatMap((n) => {
    const k = kinds.get(n.roleKey) ?? "never";
    const a = byKey.get(n.roleKey);
    return a && FACE_KINDS.has(k)
      ? [{ key: n.roleKey, x: n.x, y: n.y, angle: n.angle, size: sizeOf(k), text: short(a, a.roleKey), rank: RANK[k] ?? 9 }]
      : [];
  }), { width: W, height: H });
  const dot = idleDot(total);
  const cardAgent = card ? byKey.get(card.key) : undefined;
  const cardNode = card ? at.get(card.key) : undefined;

  return (
    <div className="cc-floor-stage" ref={stageRef} data-testid="cc-floor-stage" data-motion={motion}
         data-still={motion !== "full" || undefined} onKeyDown={onKeyDown}>
      <svg className="cc-floor-svg" role="img" aria-label={label} width={W} height={H} viewBox={`0 0 ${W} ${H}`}>
        <defs>
          <radialGradient id="cc-floor-core" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.30" />
            <stop offset="45%" stopColor="var(--accent)" stopOpacity="0.10" />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="cc-floor-you" cx="38%" cy="32%" r="75%">
            <stop offset="0%" stopColor="var(--accent-text)" stopOpacity="1" />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity="1" />
          </radialGradient>
          <radialGradient id="cc-floor-glow" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="var(--accent-text)" stopOpacity="0.55" />
            <stop offset="100%" stopColor="var(--accent-text)" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="cc-floor-glow-amber" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="var(--amber)" stopOpacity="0.6" />
            <stop offset="100%" stopColor="var(--amber)" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="cc-floor-glow-red" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="var(--red)" stopOpacity="0.55" />
            <stop offset="100%" stopColor="var(--red)" stopOpacity="0" />
          </radialGradient>
          <pattern id="cc-floor-grid" width="22" height="22" patternUnits="userSpaceOnUse">
            <circle cx="1" cy="1" r="0.8" className="cc-grid-dot" />
          </pattern>
        </defs>
        {/* Depth: a dotted floor, a soft core, three faint orbits and the team spokes. Static. */}
        <rect x={0} y={0} width={W} height={H} fill="url(#cc-floor-grid)" className="cc-grid" />
        <circle cx={q(layout.cx)} cy={q(layout.cy)} r={q(R * 1.15)} fill="url(#cc-floor-core)" />
        <circle className="cc-orbit" cx={q(layout.cx)} cy={q(layout.cy)} r={q(R * 0.34)} />
        <circle className="cc-orbit" cx={q(layout.cx)} cy={q(layout.cy)} r={q(R * 0.68)} />
        <circle className="cc-orbit cc-orbit-outer" cx={q(layout.cx)} cy={q(layout.cy)} r={q(R + 40)} />
        {layout.arcs.map((arc) => (
          <line key={`s-${arc.team}`} className="cc-spoke" x1={q(layout.cx + R * 0.36 * Math.cos(arc.start))}
                y1={q(layout.cy + R * 0.36 * Math.sin(arc.start))} x2={q(layout.cx + (R + 40) * Math.cos(arc.start))}
                y2={q(layout.cy + (R + 40) * Math.sin(arc.start))} />
        ))}

        {layout.arcs.map((arc) => {
          const mid = (arc.start + arc.end) / 2;
          const lr = R + 50;
          const anchor = Math.abs(Math.cos(mid)) < 0.3 ? "middle" : Math.cos(mid) > 0 ? "start" : "end";
          // FITTED AT EVERY SIZE (D16, re-test 9): a little smaller, then the team's short name
          // (`Arch.`), then slid inward — never cut, never below a readable size.
          const fit = fitArcLabel(arc.label, layout.cx + lr * Math.cos(mid), layout.cy + lr * Math.sin(mid),
                                  anchor, { width: W, height: H });
          const lx = fit.x, ly = fit.y, scale = fit.scale;
          const hue = teamHue(arc.team);
          const team = view.teams.find((t) => t.key === arc.team);
          const busy = view.agents.filter((a) => a.team === arc.team && kinds.get(a.roleKey) === "working").length;
          return (
            <g key={arc.team} className="cc-arc" data-team={arc.team} style={{ ["--team-hue" as string]: hue }}>
              <path className="cc-arc-band" d={arcPath(layout.cx, layout.cy, R, arc.start, arc.end)} />
              <path className="cc-arc-line" d={arcPath(layout.cx, layout.cy, R + 26, arc.start, arc.end)} />
              <text className="cc-arc-label" x={q(lx)} y={q(ly)} textAnchor={anchor} dominantBaseline="middle"
                    data-testid="cc-floor-arc-label" data-fit-width={fit.width}
                    style={scale < 1 ? { fontSize: `${Math.round(scale * 100)}%` } : undefined}>
                {fit.text}
                {busy > 0 ? <tspan className="cc-arc-busy">{` · ${busy}`}</tspan> : null}
              </text>
              {fold && arc.idleFolded ? (
                <text className="cc-arc-fold" x={q(lx)} y={q(ly + 14)} textAnchor={anchor}>{`+${arc.idleFolded} idle`}</text>
              ) : null}
              {/* The FULL name always, whatever the arc could fit (`Arch.`), then its note. */}
              <title>{[arc.label, team?.tip, busy > 0 ? `${busy} working` : ""].filter(Boolean).join(" · ")}</title>
            </g>
          );
        })}

        {/* Glow sprites under the lit faces: pre-rendered gradients, only their opacity changes. */}
        {layout.nodes.map((n) => {
          const k = kinds.get(n.roleKey);
          if (k !== "working" && k !== "waiting" && k !== "stuck") return null;
          const fill = k === "waiting" ? "url(#cc-floor-glow-amber)" : k === "stuck" ? "url(#cc-floor-glow-red)" : "url(#cc-floor-glow)";
          return <circle key={`g-${n.roleKey}`} className="cc-node-glow" cx={q(n.x)} cy={q(n.y)} r={38} fill={fill} />;
        })}

        {edges.map((e) => {
          const tip = edgeTip(e.fromName, e.toName, e.flow.n, e.flow.lastAt, nowMs);
          return (
            <g key={e.key} className="cc-edge" data-testid="cc-floor-edge" data-tip={tip}
               data-new={newPairs.has(e.key) || undefined} data-lit={lit.has(e.key) || undefined}
               style={{ opacity: lit.has(e.key) ? 1 : recencyOpacity(Date.parse(e.flow.lastAt), nowMs) }}
               onClick={() => { if (e.flow.runId) navigate(runPatch(e.flow.runId)); }}>
              <title>{tip}</title>
              <path className="cc-edge-hit" d={e.d} />
              <path className="cc-edge-soft" d={e.d} strokeWidth={edgeWidth(e.flow.n) * 2.4} />
              <path className="cc-edge-line" d={e.d} pathLength={1} strokeWidth={edgeWidth(e.flow.n)} />
            </g>
          );
        })}

        {/* Runs in flight: each one's chain of faces, joined in order; to You when it waits on you. */}
        {chains.map((c) => {
          const pts = c.keys.map((k) => at.get(k)!).map((n) => ({ x: n.x, y: n.y }));
          const d = chainPath(c.toYou ? [...pts, centre] : pts, centre);
          return (
            <g key={c.runId} className="cc-chain" data-tone={c.tone} data-testid="cc-floor-chain" data-run-id={c.runId} data-tip={c.tip}
               onClick={() => navigate(runPatch(c.runId))}>
              <title>{c.tip}</title>
              <path className="cc-chain-hit" d={d} />
              <path className="cc-chain-glow" d={d} />
              <path className="cc-chain-line" d={d} />
              {pts.map((p, i) => <circle key={i} className="cc-chain-dot" cx={q(p.x)} cy={q(p.y)} r={2.5} />)}
            </g>
          );
        })}

        {threads.map((t) => {
          const title = t.titles.length > 1 ? `${t.titles.length} waiting · ${t.titles[0]}` : t.titles[0];
          const mx = (t.a.x + layout.cx) / 2, my = (t.a.y + layout.cy) / 2;
          return (
            <g key={t.key} className="cc-thread" data-tone={t.tone} data-testid="cc-floor-thread"
               data-new={newThreads.has(t.key) || undefined} data-tip={title} role="button" tabIndex={-1}
               onClick={() => onThread(t.ids[0])}>
              <title>{title}</title>
              <line className="cc-thread-hit" x1={q(t.a.x)} y1={q(t.a.y)} x2={q(layout.cx)} y2={q(layout.cy)} />
              <line className="cc-thread-line" x1={q(t.a.x)} y1={q(t.a.y)} x2={q(layout.cx)} y2={q(layout.cy)} />
              {t.ids.length > 1 ? (
                <g className="cc-thread-badge" transform={`translate(${mx} ${my})`}>
                  <circle r={9} />
                  <text textAnchor="middle" dominantBaseline="central">{t.ids.length}</text>
                </g>
              ) : null}
            </g>
          );
        })}

        <g ref={cometLayer} className="cc-comets" aria-hidden />

        {/* BOSS: where `handed_by_kind = boss` edges start. */}
        <g className="cc-boss" transform={`translate(${bossAt.x} ${bossAt.y})`} data-tip="BOSS">
          <title>BOSS</title>
          <path d="M0 -8 L8 0 L0 8 L-8 0 Z" />
        </g>
      </svg>

      {/* The HTML layer: every agent's real face at the map's position, and You. */}
      <div className="cc-floor-nodes">
        <button type="button" className="cc-you" data-tone={youTone} data-testid="cc-floor-you"
                style={{ left: layout.cx, top: layout.cy }} title={youTip} aria-label={youTip}
                onClick={onYou}>
          <svg viewBox="0 0 48 48" aria-hidden className="cc-you-disc">
            <circle cx="24" cy="24" r="22" fill="url(#cc-floor-you)" />
          </svg>
          <span className="cc-you-word">You</span>
          {view.counts.needsYou > 0 ? (
            <span className="cc-you-count" data-testid="cc-floor-you-count" aria-hidden>{view.counts.needsYou}</span>
          ) : null}
        </button>
        {layout.nodes.map((n, i) => {
          const a = byKey.get(n.roleKey);
          if (!a) return null;
          const kind = kinds.get(n.roleKey) ?? "never";
          if (fold && (kind === "rested" || kind === "never")) return null;
          const age = a.since ? ago(a.since, nowMs) : "";
          const tip = `${short(a, a.roleKey)} · ${kind === "working" && a.line ? a.line : KIND_WORD[kind]}${
            kind !== "working" && age ? ` · last work ${age}` : ""}`;
          const face = FACE_KINDS.has(kind);
          const px = face ? sizeOf(kind) : dot;
          const hit = face ? px + 6 : Math.max(14, dot + 6);
          return (
            <button key={n.roleKey} type="button" data-role={n.roleKey}
                    data-testid={face ? "cc-floor-face" : "cc-floor-dot"}
                    className={face ? "cc-node" : "cc-node cc-node-dot"} data-kind={kind} data-state={a.state}
                    data-current={current.has(n.roleKey) || undefined}
                    data-still={stillOf.get(n.roleKey) || undefined} data-woke={woke.has(n.roleKey) || undefined}
                    tabIndex={i === active ? 0 : -1} title={tip} aria-label={tip}
                    ref={(el) => { if (el) nodeEls.current.set(n.roleKey, el); else nodeEls.current.delete(n.roleKey); }}
                    style={{ left: n.x, top: n.y, width: hit, height: hit, ["--team-hue" as string]: teamHue(n.team),
                             ...(face ? {} : { ["--dot" as string]: `${dot}px` }) }}
                    onMouseEnter={() => show(n.roleKey)} onMouseLeave={() => hide()}
                    onFocus={() => show(n.roleKey)} onBlur={() => hide()}
                    onClick={() => show(n.roleKey, true)}>
              {face ? <AgFace roleKey={n.roleKey} size={px} state={faceOf(a, kind)} decorative aiBadge={false} />
                : <span className="cc-node-pip" aria-hidden />}
              {kind === "waiting" ? <span className="cc-node-pause" aria-hidden>❙❙</span> : null}
            </button>
          );
        })}
        {[...names].map(([key, p]) => {
          const a = byKey.get(key);
          return a ? (
            <span key={`n-${key}`} className="cc-node-name" data-align={p.align} data-testid="cc-floor-name" aria-hidden
                  style={{ left: p.align === "left" ? p.x : p.align === "right" ? p.x - p.w : p.x - p.w / 2, top: p.y - p.h / 2,
                           width: p.w, height: p.h }}>{short(a, a.roleKey)}</span>
          ) : null;
        })}
        {cardAgent && cardNode ? (
          <MiniCard agent={cardAgent} kind={kinds.get(cardAgent.roleKey) ?? "never"} nowMs={nowMs}
                    x={q(cardNode.x)} y={q(cardNode.y)} stage={{ width: W, height: H }}
                    onEnter={() => show(cardAgent.roleKey, card?.pinned)} onLeave={() => hide()}
                    onClose={() => hide(false)} />
        ) : null}
      </div>
    </div>
  );
}

/** Hover, focus or tap a face: who it is, what it is doing (or last did), and two ways in. */
function MiniCard({ agent, kind, nowMs, x, y, stage, onEnter, onLeave, onClose }: {
  agent: BoardAgent; kind: FloorKind; nowMs: number; x: number; y: number;
  stage: { width: number; height: number }; onEnter: () => void; onLeave: () => void; onClose: () => void;
}) {
  const CARD_W = 228, CARD_H = 132;
  const right = x < stage.width / 2;
  const left = Math.max(8, Math.min(stage.width - CARD_W - 8, right ? x + 30 : x - 30 - CARD_W));
  const top = Math.max(8, Math.min(stage.height - CARD_H - 8, y - CARD_H / 2));
  const age = agent.since ? ago(agent.since, nowMs) : "";
  const doing = kind === "working" && agent.line ? agent.line : KIND_WORD[kind];
  return (
    <div className="cc-mini" role="dialog" aria-label={agent.callsign || agent.name} data-testid="cc-floor-card"
         style={{ left, top, width: CARD_W }} onMouseEnter={onEnter} onMouseLeave={onLeave}
         onKeyDown={(e) => { if (e.key === "Escape") onClose(); }}>
      <div className="cc-mini-head">
        <AgFace roleKey={agent.roleKey} size={40} state={faceOf(agent, kind)} decorative />
        <span className="cc-mini-who">
          <b>{agent.callsign || agent.name}</b>
          {agent.callsign ? <span className="cc-mini-role">{agent.name}</span> : null}
        </span>
      </div>
      <div className="cc-mini-line" data-kind={kind}>
        <span className="cc-mini-now" data-user-text title={agent.line || undefined}>{doing}</span>
        {age && kind !== "working" ? <span className="cc-mini-age">{`last work ${age}`}</span> : null}
      </div>
      <div className="cc-mini-acts">
        <button type="button" className="ag-btn ag-btn-primary" data-testid="cc-floor-card-talk"
                title={`Talk with ${agent.callsign || agent.name}`}
                onClick={() => { onClose(); openTalk(agent.roleKey); }}>Talk</button>
        <button type="button" className="ag-link" data-testid="cc-floor-card-passport"
                onClick={() => { onClose(); openAgent(agent.roleKey); }}>Passport ↗</button>
      </div>
    </div>
  );
}
