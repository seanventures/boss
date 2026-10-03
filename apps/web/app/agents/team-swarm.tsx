"use client";

/**
 * Agents › Team › Swarm (spec §3.8): the team as a living graph.
 *
 * The same radial geometry as the Hive (components/v2/HiveScreen.tsx: hub, an inner ring of five,
 * an outer ring offset by 0.35 rad, quadratic edges pushed 28px off the chord), with faces. The
 * agent work flows through most earns the middle. While the page is open, a dot travels an edge
 * the moment work lands on its far end. `Replay last run` plays the newest run's handoffs again at
 * 4× speed, in the order they were recorded — the moment worth recording. Under reduced motion
 * nothing travels: the run's edges are simply lit.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { Play } from "@phosphor-icons/react";

import type { OrgChart, OrgNode } from "../../lib/v2/workforce";
import type { AvatarState } from "../../lib/agent-world-types";
import { getRunChain, identityOf, isLive, toast } from "../../lib/agent-world";
import type { LiveMap } from "../../lib/agents-live";
import { AgFace } from "./agent-face";

// Hive's reference geometry (HiveScreen.tsx), kept in step by hand.
const W = 900;
const H = 640;
const CX = 450;
const CY = 320;
const INNER_R = 150;
const OUTER_R = 270;
const OUTER_OFFSET = 0.35;
const CURVE = 28;

export type SwarmNode = { id: string; name: string; x: number; y: number; ring: "hub" | "inner" | "outer" };
export type SwarmEdge = { from: string; to: string; times: number };

/** Hub = most traffic, inner = next five, outer = the rest; ties by name. */
export function radial(nodes: { id: string; name: string }[], edges: SwarmEdge[]): SwarmNode[] {
  const traffic = new Map<string, number>();
  for (const e of edges) {
    traffic.set(e.from, (traffic.get(e.from) ?? 0) + e.times);
    traffic.set(e.to, (traffic.get(e.to) ?? 0) + e.times);
  }
  const ranked = [...nodes].sort((a, b) => (traffic.get(b.id) ?? 0) - (traffic.get(a.id) ?? 0)
    || a.name.localeCompare(b.name));
  const inner = ranked.slice(1, 6);
  const outer = ranked.slice(6);
  const out: SwarmNode[] = ranked.slice(0, 1).map((n) => ({ ...n, x: CX, y: CY, ring: "hub" as const }));
  inner.forEach((n, i) => {
    const t = -Math.PI / 2 + (i * 2 * Math.PI) / Math.max(inner.length, 1);
    out.push({ ...n, x: Math.round(CX + INNER_R * Math.cos(t)), y: Math.round(CY + INNER_R * Math.sin(t)), ring: "inner" });
  });
  outer.forEach((n, i) => {
    const t = -Math.PI / 2 + OUTER_OFFSET + (i * 2 * Math.PI) / Math.max(outer.length, 1);
    out.push({ ...n, x: Math.round(CX + OUTER_R * Math.cos(t)), y: Math.round(CY + OUTER_R * Math.sin(t)), ring: "outer" });
  });
  return out;
}

export function curvePath(a: { x: number; y: number }, b: { x: number; y: number }): string {
  const mx = (a.x + b.x) / 2;
  const my = (a.y + b.y) / 2;
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len = Math.hypot(dx, dy) || 1;
  return `M${a.x} ${a.y} Q${(mx - (dy / len) * CURVE).toFixed(0)} ${(my + (dx / len) * CURVE).toFixed(0)} ${b.x} ${b.y}`;
}

/** The handoffs of a run, in recorded order: each act's parent role → its role, at 4× speed,
 *  the whole replay squeezed into at most 12 seconds. */
export function replaySchedule(acts: { actId: string; roleKey: string | null; parentActId?: string; startedAt: string }[]):
  { from: string; to: string; at: number }[] {
  const byId = new Map(acts.map((a) => [a.actId, a]));
  const hops = acts
    .filter((a) => a.parentActId && a.roleKey)
    .map((a) => ({ from: byId.get(a.parentActId!)?.roleKey ?? "", to: a.roleKey!, t: Date.parse(a.startedAt) }))
    .filter((h) => h.from && h.from !== h.to && !Number.isNaN(h.t))
    .sort((x, y) => x.t - y.t);
  if (!hops.length) return [];
  const t0 = hops[0].t;
  const span = (hops[hops.length - 1].t - t0) / 4;
  const squeeze = span > 12_000 ? 12_000 / span : 1;
  return hops.map((h, i) => ({ from: h.from, to: h.to, at: Math.max(i * 250, Math.round(((h.t - t0) / 4) * squeeze)) }));
}

type Travel = { key: string; from: string; to: string };

function newestRun(nodes: OrgNode[]): string | null {
  let best: { run: string; at: number } | null = null;
  for (const n of nodes) {
    if (!n.run_id) continue;
    const at = Date.parse(n.last_activity_at ?? "") || 0;
    if (!best || at > best.at) best = { run: n.run_id, at };
  }
  return best?.run ?? null;
}

export function TeamSwarm({ projectId, chart, live }: { projectId: string; chart: OrgChart; live: LiveMap }) {
  const nodes = chart.nodes ?? [];
  const edges: SwarmEdge[] = useMemo(() => (chart.edges ?? []).map((e) => ({ from: e.source, to: e.target, times: e.times })),
                                     [chart.edges]);
  const placed = useMemo(() => radial(nodes.map((n) => ({ id: n.id, name: n.name })), edges), [nodes, edges]);
  const at = useMemo(() => new Map(placed.map((p) => [p.id, p])), [placed]);
  const [travels, setTravels] = useState<Travel[]>([]);
  const [lit, setLit] = useState<Set<string>>(new Set());
  const [replaying, setReplaying] = useState(false);
  const prev = useRef<LiveMap>(new Map());
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const reduce = typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  useEffect(() => () => timers.current.forEach(clearTimeout), []);

  const send = (from: string, to: string) => {
    if (!at.has(from) || !at.has(to)) return;
    const key = `${from}>${to}:${Date.now()}:${Math.random()}`;
    if (reduce) { setLit((s) => new Set(s).add(`${from}>${to}`)); return; }
    setTravels((t) => [...t, { key, from, to }]);
    timers.current.push(setTimeout(() => setTravels((t) => t.filter((x) => x.key !== key)), 700));
  };

  // LIVE: work landing on an agent sends a dot along its busiest incoming edge.
  useEffect(() => {
    for (const [roleKey, p] of live) {
      if (!isLive(p) || isLive(prev.current.get(roleKey))) continue;
      const into = edges.filter((e) => e.to === roleKey).sort((a, b) => b.times - a.times)[0];
      if (into) send(into.from, into.to);
    }
    prev.current = live;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live]);

  const replay = async () => {
    const run = newestRun(nodes);
    if (!run) { toast("error", "No run to replay"); return; }
    setReplaying(true);
    setLit(new Set());
    const r = await getRunChain(run, projectId);
    const plan = r.ok ? replaySchedule(r.data.acts) : [];
    if (!plan.length) { setReplaying(false); toast("error", "No handoffs to replay"); return; }
    for (const hop of plan) timers.current.push(setTimeout(() => send(hop.from, hop.to), reduce ? 0 : hop.at));
    timers.current.push(setTimeout(() => setReplaying(false), (reduce ? 0 : plan[plan.length - 1].at) + 800));
  };

  const stateOf = (id: string): AvatarState => {
    const p = live.get(id);
    return p ? (p.inPanel ? "speaking" : p.state) : "idle";
  };

  if (!placed.length) return <div className="ag-empty"><span>No agents yet</span></div>;
  return (
    <div className="ag-team" data-testid="ag-swarm">
      <div className="ag-filters">
        <button type="button" className="ag-btn" disabled={replaying} data-testid="ag-replay"
                title="Plays the last run's handoffs at 4× speed." onClick={() => void replay()}>
          <Play size={12} aria-hidden /> {replaying ? "Replaying…" : "Replay last run"}
        </button>
      </div>
      <div className="ag-swarm-stage" style={{ aspectRatio: `${W} / ${H}` }}>
        <svg className="ag-swarm-edges" viewBox={`0 0 ${W} ${H}`} aria-hidden>
          {edges.map((e) => {
            const a = at.get(e.from);
            const b = at.get(e.to);
            if (!a || !b || a === b) return null;
            return (
              <path key={`${e.from}>${e.to}`} id={`ag-sw-${e.from}-${e.to}`} d={curvePath(a, b)}
                    className="ag-swarm-edge" data-lit={lit.has(`${e.from}>${e.to}`) || undefined}
                    style={{ strokeWidth: Math.min(1 + Math.log2(1 + e.times), 5) }} />
            );
          })}
          {travels.map((t) => {
            const a = at.get(t.from)!;
            const b = at.get(t.to)!;
            return (
              <circle key={t.key} r={5} className="ag-swarm-dot">
                <animateMotion dur="0.6s" fill="freeze" path={curvePath(a, b)} />
              </circle>
            );
          })}
        </svg>
        {placed.map((n) => {
          const who = identityOf(n.id, n.name);
          return (
            <span key={n.id} className="ag-swarm-node" data-ring={n.ring}
                  style={{ left: `${(n.x / W) * 100}%`, top: `${(n.y / H) * 100}%` }}>
              <AgFace roleKey={n.id} identity={who} size={n.ring === "hub" ? 48 : 32} state={stateOf(n.id)} />
              <span className="ag-swarm-name">{who.callsign || n.name}</span>
            </span>
          );
        })}
      </div>
    </div>
  );
}
