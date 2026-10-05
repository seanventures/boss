"use client";

/**
 * THE FLOOR (command-center spec §4.7): the landing page's hero. Your agents in a ring of teams,
 * You at the centre.
 *
 *   a working agent      its real face (AgFace), lit, in its real ring state
 *   an idle agent        a dim star (a 10px dot): thirty idle agents read as a quiet ring
 *   a hand-off           a curve between two nodes, from an act started in the last hour
 *   a NEW hand-off       a comet along that curve, once, the poll after the ledger records it
 *   a decision for you   an amber dashed thread from the agent that asked, to You
 *   stopped work         a red solid thread
 *
 * EVERY MARK IS REAL. Edges are `flows[]` (agent_acts, last 60 min, known hands only); threads are
 * `attention[]` items that name their agent; faces are `agents[]` presence. Nothing here moves
 * unless one of those changed between two polls, and nothing moves at all when the console is
 * Paused, the tab is hidden, BOSS is unreachable or the host is struggling (`motion="none"`).
 *
 * ONE LOOP. Comets share one requestAnimationFrame loop that exists only while a comet flies
 * (≤ 4 at once, 150ms apart, ≤ 12 per poll; the rest only light their edges). Comet dots are
 * drawn imperatively into one <g>, so a frame costs no React render.
 */
import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { openAgent } from "../../lib/agent-world";
import type { BoardAgent } from "../../lib/agents-live-board";
import {
  arcPath, edgeCurve, edgeWidth, floorLayout, freshFlows, quadAt, recencyOpacity, COMET_CAP,
  type FloorLayout,
} from "../../lib/command-center-graphics";
import { boardOf, type CommandCenterView, type Flow, type MotionMode } from "../../lib/command-center-view";
import { AgFace } from "../agents/agent-face";
import { ANIMATED_STATES, RING_BUDGET, ago, linkTo, navigate, plainClick, runPatch, useNow, type Band } from "../agents/live-util";
import { TeamZones, faceStateOf, teamHue } from "../agents/team-zones";

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
export const STAGE = { width: 720, height: 440 };
/** Where BOSS sits, above You. */
const BOSS_DY = -62;
const COMET_MS = 600;
const COMET_GAP_MS = 150;
const COMET_FLYING_MAX = 4;
const ARRIVE_MS = 400;
const LIT_MS = 1000;
const STOPPED = new Set(["failed_step", "gate_override", "budget_refused"]);

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

/** The footer's words (§4.7): who is working, or how long the floor has been still. */
export function floorFooter(view: CommandCenterView, nowMs: number | null):
    { text: string; tip?: string } {
  const idle = view.agents.filter(isIdle);
  const busy = view.agents.length - idle.length;
  if (busy === 0 && view.flows.length === 0) {
    if (!view.today.everRan) {
      return { text: "Your team is ready", tip: `${view.agents.length} agents, idle until work starts` };
    }
    const last = idle.map((a) => Date.parse(a.since ?? "")).filter(Number.isFinite);
    const latest = last.length ? new Date(Math.max(...last)).toISOString() : view.today.lastWorkAt;
    const age = ago(latest, nowMs ?? Date.parse(view.generatedAt));
    return { text: age ? `All idle · last work ${age}` : "All idle" };
  }
  return { text: `${busy} working · ${idle.length} idle` };
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
        <span className="cc-floor-count" title={footer.tip} data-testid="cc-floor-footer">{footer.text}</span>
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
  const working = view.agents.filter((a) => !isIdle(a));
  const names = new Map(view.agents.map((a) => [a.roleKey, a]));
  const threads = view.attention.filter((a) => a.roleKey && (a.kind === "approval" || STOPPED.has(a.kind)));
  return (
    <ul className={visible ? "cc-floor-list" : "cc-floor-list ag-sr wf-sr"}
        data-testid={visible ? "cc-floor-list" : "cc-floor-sr-list"} aria-label="Team map as a list">
      {working.map((a) => (
        <li key={a.roleKey}>
          <button type="button" className="cc-floor-li" onClick={() => openAgent(a.roleKey)} title={a.line || undefined}>
            <AgFace roleKey={a.roleKey} size={20} state={faceStateOf(a)} decorative />
            <span>{short(a, a.roleKey)}</span>
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

  // Edges: a hand-off is drawn only between two nodes that are on the map — an unknown hander
  // is never drawn to a guessed node (the server drops it too).
  const edges: Edge[] = [];
  for (const f of view.flows) {
    const from = f.fromKind === "person" ? centre : f.fromKind === "boss" ? bossAt : at.get(f.from);
    const to = at.get(f.to);
    if (!from || !to) continue;
    const { d, c } = edgeCurve(from, to, centre);
    edges.push({ key: `${f.fromKind}:${f.from}>${f.to}`, flow: f, a: from, b: to, c, d,
                 fromName: f.fromKind === "person" ? "You" : f.fromKind === "boss" ? "BOSS" : short(byKey.get(f.from), f.from),
                 toName: short(byKey.get(f.to), f.to) });
  }
  const handoffs = view.flows.reduce((n, f) => n + f.n, 0);

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
    const awake = view.agents.filter((a) => !isIdle(a)).map((a) => a.roleKey);
    const out = prev === null || motion === "none" ? new Set<string>() : new Set(awake.filter((k) => !prev.has(k)));
    seenAwake.current = new Set(awake);
    return out;
  }, [view.agents, motion]); // eslint-disable-line react-hooks/exhaustive-deps -- per poll

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
      const t = window.setTimeout(() => setLit((s) => new Set([...s].filter((k) => !light.includes(k)))), LIT_MS);
      void t;
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
      tail.setAttribute("r", "9");
      tail.setAttribute("class", "cc-comet-glow");
      const head = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      head.setAttribute("r", "3");
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

  // ── Keyboard: arrows go round the ring, Enter opens the Passport, Esc leaves.
  const order = layout.nodes.map((n) => n.roleKey);
  const [active, setActive] = useState(0);
  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
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
    if (!a || isIdle(a)) continue;
    const moves = ANIMATED_STATES.has(faceStateOf(a));
    stillOf.set(n.roleKey, !moves || budget <= 0);
    if (moves && budget > 0) budget -= 1;
  }

  const youTone = view.counts.stopped > 0 ? "red" : view.counts.needsYou > 0 ? "amber" : "calm";
  const youTip = motion === "none" && motionWhy ? motionWhy
    : view.counts.needsYou > 0 ? `${view.counts.needsYou} ${view.counts.needsYou === 1 ? "needs" : "need"} you` : "You";
  const label = `Team map: ${view.counts.working} working, ${view.counts.needsYou} waiting on you, `
    + `${handoffs} hand-off${handoffs === 1 ? "" : "s"} in the last hour`;
  const total = view.agents.length;
  const fold = total > 120;
  const { width: W, height: H, radius: R } = layout;

  return (
    <div className="cc-floor-stage" ref={stageRef} data-testid="cc-floor-stage" data-motion={motion}
         data-still={motion !== "full" || undefined} onKeyDown={onKeyDown}>
      <svg className="cc-floor-svg" role="img" aria-label={label} width={W} height={H} viewBox={`0 0 ${W} ${H}`}>
        <defs>
          <radialGradient id="cc-floor-core" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.22" />
            <stop offset="55%" stopColor="var(--accent)" stopOpacity="0.06" />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="cc-floor-you" cx="38%" cy="32%" r="75%">
            <stop offset="0%" stopColor="var(--accent-text)" stopOpacity="0.95" />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity="0.95" />
          </radialGradient>
          <radialGradient id="cc-floor-glow" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="var(--accent-text)" stopOpacity="0.55" />
            <stop offset="100%" stopColor="var(--accent-text)" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="cc-floor-glow-amber" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="var(--amber)" stopOpacity="0.55" />
            <stop offset="100%" stopColor="var(--amber)" stopOpacity="0" />
          </radialGradient>
        </defs>
        {/* Depth: a soft core and two faint orbits. Static: they carry no state. */}
        <circle cx={layout.cx} cy={layout.cy} r={R * 0.95} fill="url(#cc-floor-core)" />
        <circle className="cc-orbit" cx={layout.cx} cy={layout.cy} r={R * 0.55} />
        <circle className="cc-orbit cc-orbit-outer" cx={layout.cx} cy={layout.cy} r={R + 30} />

        {layout.arcs.map((arc) => {
          const mid = (arc.start + arc.end) / 2;
          const lr = R + 44;
          const lx = layout.cx + lr * Math.cos(mid), ly = layout.cy + lr * Math.sin(mid);
          const anchor = Math.abs(Math.cos(mid)) < 0.25 ? "middle" : Math.cos(mid) > 0 ? "start" : "end";
          const hue = teamHue(arc.team);
          const team = view.teams.find((t) => t.key === arc.team);
          return (
            <g key={arc.team} className="cc-arc" data-team={arc.team} style={{ ["--team-hue" as string]: hue }}>
              <path className="cc-arc-band" d={arcPath(layout.cx, layout.cy, R, arc.start, arc.end)} />
              <path className="cc-arc-line" d={arcPath(layout.cx, layout.cy, R, arc.start, arc.end)} />
              <text className="cc-arc-label" x={lx} y={ly} textAnchor={anchor} dominantBaseline="middle">
                {arc.label.toUpperCase()}
              </text>
              {fold && arc.idleFolded ? (
                <text className="cc-arc-fold" x={lx} y={ly + 13} textAnchor={anchor}>{`+${arc.idleFolded} idle`}</text>
              ) : null}
              {team?.tip ? <title>{team.tip}</title> : null}
            </g>
          );
        })}

        {/* Glow sprites under the lit faces: pre-rendered gradients, only their opacity changes. */}
        {layout.nodes.map((n) => {
          const a = byKey.get(n.roleKey);
          if (!a || isIdle(a)) return null;
          return <circle key={`g-${n.roleKey}`} className="cc-node-glow" cx={n.x} cy={n.y} r={30}
                         fill={a.state === "needs_you" ? "url(#cc-floor-glow-amber)" : "url(#cc-floor-glow)"} />;
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
              <path className="cc-edge-line" d={e.d} pathLength={1} strokeWidth={edgeWidth(e.flow.n)} />
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
              <line className="cc-thread-hit" x1={t.a.x} y1={t.a.y} x2={layout.cx} y2={layout.cy} />
              <line className="cc-thread-line" x1={t.a.x} y1={t.a.y} x2={layout.cx} y2={layout.cy} />
              {t.ids.length > 1 ? (
                <g className="cc-thread-badge" transform={`translate(${mx} ${my})`}>
                  <circle r={8} />
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
          <path d="M0 -7 L7 0 L0 7 L-7 0 Z" />
        </g>
      </svg>

      {/* The HTML layer: real faces at the map's positions, and You. */}
      <div className="cc-floor-nodes">
        <button type="button" className="cc-you" data-tone={youTone} data-testid="cc-floor-you"
                style={{ left: layout.cx, top: layout.cy }} title={youTip} aria-label={youTip}
                onClick={onYou}>
          <svg viewBox="0 0 48 48" aria-hidden className="cc-you-disc">
            <circle cx="24" cy="24" r="22" fill="url(#cc-floor-you)" />
          </svg>
          <span className="cc-you-word">You</span>
        </button>
        {layout.nodes.map((n, i) => {
          const a = byKey.get(n.roleKey);
          if (!a) return null;
          const idle = isIdle(a);
          if (idle && fold) return null;
          const name = short(a, a.roleKey);
          const tip = idle
            ? `${name} · idle${a.since ? ` · last work ${ago(a.since, nowMs)}` : ""}`
            : `${name} · ${a.line || faceStateOf(a)}`;
          const side = Math.cos(n.angle);
          return (
            <button key={n.roleKey} type="button" data-role={n.roleKey} data-testid={idle ? "cc-floor-dot" : "cc-floor-face"}
                    className={idle ? "cc-node cc-node-idle" : "cc-node cc-node-live"} data-state={a.state}
                    data-still={stillOf.get(n.roleKey) || undefined} data-woke={woke.has(n.roleKey) || undefined}
                    tabIndex={i === active ? 0 : -1} title={tip} aria-label={tip}
                    ref={(el) => { if (el) nodeEls.current.set(n.roleKey, el); else nodeEls.current.delete(n.roleKey); }}
                    style={{ left: n.x, top: n.y, ["--team-hue" as string]: teamHue(n.team),
                             ["--dot" as string]: `${layout.idleR * 2}px` }}
                    onClick={() => openAgent(n.roleKey)}>
              {idle ? <span className="cc-dot" aria-hidden /> : (
                <>
                  <AgFace roleKey={n.roleKey} size={32} state={faceStateOf(a)} decorative />
                  {a.state === "needs_you" ? <span className="cc-node-pause" aria-hidden>❙❙</span> : null}
                  <span className="cc-node-name" data-side={side > 0.2 ? "right" : side < -0.2 ? "left" : "mid"}>{name}</span>
                </>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
