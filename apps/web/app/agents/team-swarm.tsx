/**
 * The Swarm's pure helpers (agents-home spec §3.8), kept for Organization › Flow.
 *
 * The radial Swarm itself is retired (org-view spec §5): it drew every handoff at once and moved
 * agents as traffic changed. Flow draws teams in fixed columns instead, and keeps what was worth
 * keeping from here: the curve, `Replay last run` (the newest run's handoffs at 4× speed, ≤12s)
 * and its schedule.
 */
import { getRunChain, listRecentRunIds } from "../../lib/agent-world";

// Hive's reference geometry (HiveScreen.tsx), kept in step by hand.
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

/** The newest workflow run that has handoffs to play, and its schedule. The org chart's
 *  `run_id` is an agent-process run, not a workflow run, so it can never key a chain. */
export async function lastRunReplay(projectId: string):
  Promise<{ runId: string; plan: ReturnType<typeof replaySchedule> } | null> {
  const runs = await listRecentRunIds(projectId);
  if (!runs.ok) return null;
  for (const runId of runs.data) {
    const chain = await getRunChain(runId, projectId);
    const plan = chain.ok ? replaySchedule(chain.data.acts) : [];
    if (plan.length) return { runId, plan };
  }
  return null;
}
