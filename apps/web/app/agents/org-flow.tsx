"use client";

/**
 * Organization › Flow (org-view spec §4.3): where work goes between teams, drawn the way service
 * maps are — never a hairball.
 *
 *   - One node per team, in FIXED COLUMNS by where the team usually joins a run (`flow_col`,
 *     computed by the server per window, so polling never moves a node). Ties by TEAM_ORDER.
 *     Teams with no runs sit in a last, muted column. `You` leads when a person handed work.
 *   - At most 12 team edges are drawn, by volume; the rest are counted into the Table. Width is
 *     1 + log2(1 + handoffs), capped at 6. Self-team work is `↻ n inside` on the node, not a loop.
 *   - An edge where handed work failed or waits says so in a word and a glyph, never colour alone.
 *   - Pressing a team isolates it (the Datadog move): its neighbours stay lit, everything else
 *     dims, and the node opens to its agents with agent-level edges (still ≤12).
 *   - `?run=` draws one run's path at agent level, hop by hop (400ms, ≤4s; all at once when
 *     still). A live handoff sends one dot along an edge only if that edge is drawn.
 *   - The Table (`From · To · Handoffs · Failed · Waiting · Last`) is the accessible Flow: always
 *     in the DOM, visually hidden in Map mode, and the whole of Flow under 640px.
 */
import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { CaretDown, CaretRight, Play, X } from "@phosphor-icons/react";

import { getRunChain, handedByRole, identityOf, isLive, openAgent, toast } from "../../lib/agent-world";
import type { LiveMap } from "../../lib/agents-live";
import { TEAM_ORDER } from "../../lib/agents-live-board";
import { ageWords, openStatusWord, windowWords, type OrgBoard, type OrgDays, type OrgEdge, type OrgTeam } from "../../lib/agents-org-board";
import { openPlan } from "../../lib/converse-doors";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { AgFace } from "./agent-face";
import { edgeSummary, teamOfRole, type EdgeFocus } from "./org-edge-panel";
import { StuckItem, WaitItem, consoleHref, liveFirst, liveState, nameOf, runHref, teamLabel } from "./org-shared";
import { shownTeams } from "./org-teams";
import { curvePath, lastRunReplay } from "./team-swarm";
import { matches, reducedMotion, teamHue } from "./team-zones";

export const EDGE_CAP = 12;
export const ISOLATE_FACES = 16;
const NODE_FACES = 4;

/** A run on the map: its agents in order, and the ones whose work there ended in an error. */
export type FlowFocus = { roles: string[]; subject: string; runId: string; notFound?: boolean; failed?: string[] };

export type OrgFlowProps = {
  projectId: string;
  board: OrgBoard;
  live: LiveMap;
  search: string;
  days: OrgDays;
  narrow: boolean;
  isolate: string | null;
  onIsolate: (team: string | null) => void;
  selected: string | null;
  onSelect: (roleKey: string | null) => void;
  onOpenEdge: (focus: EdgeFocus) => void;
  focusRunId?: string | null;
  onClearFocus?: () => void;
  onWindow: (days: OrgDays) => void;
  /** `T` and the toggle share this; the shell keeps it across polls. */
  mode: "map" | "table";
  onMode: (mode: "map" | "table") => void;
  /** The `You ⏸` label: show what waits on you (Teams, or Live). */
  onShowWaits?: () => void;
};

export function edgeWidth(handoffs: number): number {
  return Math.min(1 + Math.log2(1 + handoffs), 6);
}

/** The words an edge's state says: `2 failed ✕`, `1 waiting ⏸`, `1 stale`. */
export function edgeStateWords(e: Pick<OrgEdge, "failed" | "waiting" | "stale">): string[] {
  return [e.failed ? `${e.failed} failed ✕` : "", e.waiting ? `${e.waiting} waiting ⏸` : "",
          e.stale ? `${e.stale} stale` : ""].filter(Boolean);
}

/** A run's agents in the order they took the work, from its chain; its subject from the run's
 *  own root act. A run the API does not know is `notFound`, never a guessed path. */
export async function runPath(runId: string, projectId: string): Promise<FlowFocus> {
  const chain = await getRunChain(runId, projectId);
  if (!chain.ok) return { roles: [], subject: runId, runId, notFound: true };
  const acts = [...chain.data.acts].sort((a, b) => a.startedAt.localeCompare(b.startedAt));
  const roles = acts.filter((a) => a.roleKey).map((a) => a.roleKey!).filter((r, i, all) => r !== all[i - 1]);
  const root = acts.find((a) => a.kind === "run");
  const failed = [...new Set(acts.filter((a) => a.roleKey && a.status === "error").map((a) => a.roleKey!))];
  return { roles, subject: root?.label || "This run", runId, failed };
}

type Column = { key: string; teams: string[]; muted?: boolean; you?: boolean; gate?: boolean };

/** Fixed columns: `You` first (when a person handed work), then by flow_col, then the muted
 *  `No work` column. Inside a column, TEAM_ORDER then label. */
export function flowColumns(board: OrgBoard, teams: OrgTeam[]): Column[] {
  const rank = (k: string) => { const i = TEAM_ORDER.indexOf(k); return i < 0 ? TEAM_ORDER.length - 1.5 : i; };
  const byCol = new Map<number, string[]>();
  const idle: string[] = [];
  for (const t of teams) {
    if (t.flowCol === null) idle.push(t.key);
    else byCol.set(t.flowCol, [...(byCol.get(t.flowCol) ?? []), t.key]);
  }
  const order = (ks: string[]) => [...ks].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
  const cols: Column[] = [];
  if (board.teamEdges.some((e) => e.from === "person")) cols.push({ key: "you", teams: ["person"], you: true });
  for (const c of [...byCol.keys()].sort((a, b) => a - b)) cols.push({ key: `c${c}`, teams: order(byCol.get(c)!) });
  // WHERE WORK WAITS ON YOU (walk D3): gates hold a team's finished work and have no agent, so
  // they are a node of their own — `You ⏸` — at the end of the flow, fed by the teams they hold.
  if (teams.some((t) => t.waitingYou > 0)) cols.push({ key: "gate", teams: ["gate"], gate: true });
  if (idle.length) cols.push({ key: "idle", teams: order(idle), muted: true });
  return cols;
}

type Drawn = { key: string; from: string; to: string; fromPt: string; toPt: string; edge: OrgEdge;
               level: "team" | "agent"; dim?: boolean; lit?: boolean; failedHop?: boolean; gate?: boolean };
type Pt = { l: number; r: number; y: number; t: number; b: number };

/** Every edge's geometry, from measured boxes (walk D2, D4):
 *  - neighbouring columns: a curve, side to side;
 *  - columns apart: routed BELOW the columns it passes (never through a node), each such edge
 *    in its own lane;
 *  - a pair that hands work both ways: two curves, offset apart, each with its own label;
 *  - one column (agents of one team): an arc to the right of the column.
 *  Returns the path and where its label sits. */
export function routeEdges(drawn: Pick<Drawn, "key" | "from" | "to" | "fromPt" | "toPt" | "level">[],
                           points: Map<string, Pt>, colOf: (pt: string) => number,
                           colBottom: (from: number, to: number) => number): Map<string, { d: string; x: number; y: number }> {
  const out = new Map<string, { d: string; x: number; y: number }>();
  const keys = new Set(drawn.map((d) => `${d.level}:${d.key}`));
  let lane = 0;
  for (const e of drawn) {
    const a = points.get(e.fromPt);
    const b = points.get(e.toPt);
    if (!a || !b) continue;
    const ca = colOf(e.fromPt);
    const cb = colOf(e.toPt);
    const both = keys.has(`${e.level}:${e.to}>${e.from}`);
    const dy = both ? (e.from < e.to ? -7 : 7) : 0;
    const ay = a.y + dy;
    const by = b.y + dy;
    if (ca === cb) {
      const x = Math.max(a.r, b.r) + 36;
      out.set(`${e.level}:${e.key}`, { d: `M${a.r} ${ay} C${x} ${ay} ${x} ${by} ${b.r} ${by}`, x: x - 9, y: (ay + by) / 2 });
      continue;
    }
    const forward = cb > ca;
    const sx = forward ? a.r : a.l;
    const ex = forward ? b.l : b.r;
    if (Math.abs(cb - ca) === 1) {
      out.set(`${e.level}:${e.key}`, { d: curvePath({ x: sx, y: ay }, { x: ex, y: by }), x: (sx + ex) / 2, y: (ay + by) / 2 });
      continue;
    }
    const ly = colBottom(Math.min(ca, cb) + 1, Math.max(ca, cb) - 1) + 18 + 10 * (lane % 4);
    lane += 1;
    const k = forward ? 28 : -28;
    const d = `M${sx} ${ay} C${sx + k} ${ay} ${sx + k} ${ly} ${sx + 2 * k} ${ly} L${ex - 2 * k} ${ly} `
      + `C${ex - k} ${ly} ${ex - k} ${by} ${ex} ${by}`;
    out.set(`${e.level}:${e.key}`, { d, x: (sx + ex) / 2, y: ly });
  }
  return out;
}

export function OrgFlow(props: OrgFlowProps): React.ReactElement {
  useIdentityVersion();
  const { board, live, search, days, narrow, isolate, selected, mode, projectId } = props;
  // A team node is its agents: a team with none (Yours holding only an empty seat) is not one.
  const teams = useMemo(() => shownTeams(board).filter((t) => t.members.length > 0), [board]);
  const teamOf = useCallback((r: string) => teamOfRole(board, r), [board]);
  const [focus, setFocus] = useState<FlowFocus | null>(null);
  const [litCount, setLitCount] = useState(0);
  const [replaying, setReplaying] = useState(false);
  const [travels, setTravels] = useState<{ key: string; edge: string }[]>([]);
  const [flash, setFlash] = useState<Set<string>>(new Set());
  const [tableTeam, setTableTeam] = useState<string | null>(null);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; timers.current.forEach(clearTimeout); }; }, []);

  // ONE RUN'S PATH (`?run=`): hops light in order, 400ms each and 4s at most; all at once when still.
  useEffect(() => {
    if (!props.focusRunId) { if (!replaying) { setFocus(null); setLitCount(0); } return undefined; }
    let alive = true;
    const local: ReturnType<typeof setTimeout>[] = [];
    void runPath(props.focusRunId, projectId).then((p) => {
      if (!alive) return;
      setFocus(p);
      if (reducedMotion() || p.roles.length <= 1) { setLitCount(p.roles.length); return; }
      const step = Math.min(400, 4000 / p.roles.length);
      setLitCount(1);
      for (let i = 2; i <= p.roles.length; i += 1) local.push(setTimeout(() => { if (alive) setLitCount(i); }, step * (i - 1)));
    });
    return () => { alive = false; local.forEach(clearTimeout); };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the run, not every board poll
  }, [props.focusRunId, projectId]);

  // The map draws the teams that worked; the rest are one muted line under it, so the columns
  // that matter share the width (walk: a seventh column pushed the map past the panel).
  const allCols = useMemo(() => flowColumns(board, teams), [board, teams]);
  const cols = useMemo(() => allCols.filter((c) => !c.muted), [allCols]);
  const idleTeams = allCols.find((c) => c.muted)?.teams ?? [];

  // WHAT OPENS TO ITS AGENTS: a run's path (just its agents), else the selected agent's team,
  // else the isolated team (≤16 by volume).
  const volume = useMemo(() => {
    const v = new Map<string, number>();
    for (const e of board.agentEdges) { v.set(e.from, (v.get(e.from) ?? 0) + e.handoffs); v.set(e.to, (v.get(e.to) ?? 0) + e.handoffs); }
    return v;
  }, [board.agentEdges]);
  const expanded = useMemo(() => {
    const m = new Map<string, { roles: string[]; more: number }>();
    if (focus && focus.roles.length) {
      for (const r of focus.roles) {
        const t = teamOf(r);
        const x = m.get(t) ?? { roles: [], more: 0 };
        if (!x.roles.includes(r)) x.roles.push(r);
        m.set(t, x);
      }
      return m;
    }
    if (selected) { m.set(teamOf(selected), { roles: [selected], more: 0 }); return m; }
    if (isolate) {
      const members = teams.find((t) => t.key === isolate)?.members ?? [];
      const ranked = [...members].sort((a, b) => (volume.get(b) ?? 0) - (volume.get(a) ?? 0) || a.localeCompare(b));
      m.set(isolate, { roles: ranked.slice(0, ISOLATE_FACES), more: Math.max(0, ranked.length - ISOLATE_FACES) });
    }
    return m;
  }, [focus, selected, isolate, teams, teamOf, volume]);

  // WHAT IS DRAWN. Team edges: the top 12 by volume. Inside a focus, agent edges replace the
  // focused team's (still ≤12); a run's path is its hops.
  const teamEdges = useMemo(() => board.teamEdges.filter((e) => e.from !== e.to), [board.teamEdges]);
  const topTeam = teamEdges.slice(0, EDGE_CAP);
  const hidden = Math.max(0, teamEdges.length - EDGE_CAP);
  // Work held at a gate: each team's finished work waiting on you, as an edge into `You ⏸`.
  const gateEdges: Drawn[] = useMemo(() => teams.filter((t) => t.waitingYou > 0).map((t) => ({
    key: `${t.key}>gate`, from: t.key, to: "gate", fromPt: `t:${t.key}`, toPt: "t:gate", level: "team" as const, gate: true,
    edge: { from: t.key, to: "gate", handoffs: t.waitingYou, failed: 0, waiting: t.waitingYou, stale: 0, lastAt: null },
  })), [teams]);
  const pointOf = useCallback((role: string): string => {
    const t = teamOf(role);
    return expanded.get(t)?.roles.includes(role) ? `a:${role}` : `t:${t}`;
  }, [expanded, teamOf]);
  const drawn: Drawn[] = useMemo(() => {
    const out: Drawn[] = [];
    if (focus && focus.roles.length) {
      // Only the hops lit so far are drawn: the path draws itself in, hop by hop.
      focus.roles.slice(1, Math.max(1, litCount)).forEach((to, i) => {
        const from = focus.roles[i];
        const e = board.agentEdges.find((x) => x.from === from && x.to === to)
          ?? { from, to, handoffs: 1, failed: 0, waiting: 0, stale: 0, lastAt: null };
        out.push({ key: `${from}>${to}`, from, to, fromPt: pointOf(from), toPt: pointOf(to), edge: e, level: "agent",
                   lit: true, failedHop: focus.failed?.includes(to) });
      });
      for (const e of topTeam) out.push({ key: `${e.from}>${e.to}`, from: e.from, to: e.to, fromPt: `t:${e.from}`,
                                          toPt: `t:${e.to}`, edge: e, level: "team", dim: true });
      return out;
    }
    const focusTeam = selected ? teamOf(selected) : isolate;
    if (focusTeam) {
      const mine = board.agentEdges
        .filter((e) => (selected ? e.from === selected || e.to === selected
          : teamOf(e.from) === focusTeam || teamOf(e.to) === focusTeam))
        .slice(0, EDGE_CAP);
      for (const e of mine) {
        out.push({ key: `${e.from}>${e.to}`, from: e.from, to: e.to, fromPt: pointOf(e.from), toPt: pointOf(e.to),
                   edge: e, level: "agent" });
      }
      for (const e of topTeam) {
        if (e.from === focusTeam || e.to === focusTeam) continue;
        out.push({ key: `${e.from}>${e.to}`, from: e.from, to: e.to, fromPt: `t:${e.from}`, toPt: `t:${e.to}`,
                   edge: e, level: "team", dim: true });
      }
      out.push(...gateEdges.map((g) => ({ ...g, dim: g.from !== focusTeam })));
      return out;
    }
    for (const e of topTeam) out.push({ key: `${e.from}>${e.to}`, from: e.from, to: e.to, fromPt: `t:${e.from}`,
                                        toPt: `t:${e.to}`, edge: e, level: "team" });
    out.push(...gateEdges);
    return out;
  }, [focus, litCount, board.agentEdges, topTeam, selected, isolate, pointOf, teamOf, gateEdges]);

  // Which teams stay lit.
  const litTeams = useMemo(() => {
    if (focus && focus.roles.length) return new Set(focus.roles.map(teamOf));
    if (selected) {
      const s = new Set([teamOf(selected)]);
      for (const e of board.agentEdges) {
        if (e.from === selected) s.add(teamOf(e.to));
        if (e.to === selected) s.add(e.from === "person" ? "person" : teamOf(e.from));
      }
      return s;
    }
    if (isolate) {
      const s = new Set([isolate]);
      for (const e of teamEdges) { if (e.from === isolate) s.add(e.to); if (e.to === isolate) s.add(e.from); }
      return s;
    }
    return null;
  }, [focus, selected, isolate, board.agentEdges, teamEdges, teamOf]);
  const onPath = new Set(focus?.roles.slice(0, litCount) ?? []);

  // ── measuring, so curves meet the nodes and faces ─────────────────────────────────────────
  const stage = useRef<HTMLDivElement>(null);
  const anchors = useRef(new Map<string, HTMLElement>());
  const [points, setPoints] = useState<Map<string, Pt>>(new Map());
  const [size, setSize] = useState({ w: 0, h: 0 });
  const measure = useCallback(() => {
    const box = stage.current?.getBoundingClientRect();
    if (!box) return;
    const next = new Map<string, Pt>();
    for (const [k, el] of anchors.current) {
      const r = el.getBoundingClientRect();
      next.set(k, { l: r.left - box.left, r: r.right - box.left, y: r.top - box.top + r.height / 2,
                    t: r.top - box.top, b: r.bottom - box.top });
    }
    setPoints(next);
    setSize({ w: box.width, h: box.height });
  }, []);
  useLayoutEffect(() => { measure(); }, [measure, board, expanded, mode, narrow]);
  useEffect(() => {
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [measure]);
  const colIndex = useMemo(() => {
    const m = new Map<string, number>();
    cols.forEach((c, i) => c.teams.forEach((t) => m.set(t, i)));
    return m;
  }, [cols]);
  const colOf = useCallback((pt: string) => {
    const [kind, id] = [pt.slice(0, 1), pt.slice(2)];
    return colIndex.get(kind === "a" ? teamOf(id) : id) ?? 0;
  }, [colIndex, teamOf]);
  const colBottom = useCallback((from: number, to: number) => {
    let b = 0;
    for (let i = from; i <= to; i += 1) b = Math.max(b, points.get(`c:${i}`)?.b ?? 0);
    return b;
  }, [points]);
  const routes = useMemo(() => routeEdges(drawn, points, colOf, colBottom), [drawn, points, colOf, colBottom]);
  const pathOf = (d: Drawn): string | null => routes.get(`${d.level}:${d.key}`)?.d ?? null;
  const maxVolume = Math.max(1, ...drawn.filter((d) => !d.gate).map((d) => d.edge.handoffs));
  const anchor = (k: string) => (el: HTMLElement | null) => { if (el) anchors.current.set(k, el); else anchors.current.delete(k); };

  // ── live: a dot only along a drawn edge ───────────────────────────────────────────────────
  const drawnKeys = useMemo(() => new Map(drawn.filter((d) => !d.dim).map((d) => [d.key, d] as const)), [drawn]);
  const prev = useRef<LiveMap>(new Map());
  const send = useCallback((edgeKey: string) => {
    if (reducedMotion()) {
      setFlash((s) => new Set(s).add(edgeKey));
      timers.current.push(setTimeout(() => setFlash((s) => { const n = new Set(s); n.delete(edgeKey); return n; }), 1000));
      return;
    }
    const t = { key: `${edgeKey}:${Date.now()}:${Math.random()}`, edge: edgeKey };
    setTravels((x) => [...x, t]);
    timers.current.push(setTimeout(() => setTravels((x) => x.filter((y) => y.key !== t.key)), 700));
  }, []);
  const drawnRef = useRef(drawnKeys);
  drawnRef.current = drawnKeys;
  useEffect(() => {
    for (const [roleKey, p] of live) {
      if (!isLive(p) || isLive(prev.current.get(roleKey)) || !p.actId) continue;
      void handedByRole(projectId, p.actId).then((from) => {
        if (!mounted.current || !from) return;
        const agentKey = `${from}>${roleKey}`;
        const teamKey = `${teamOf(from)}>${teamOf(roleKey)}`;
        const d = drawnRef.current.get(agentKey) ?? drawnRef.current.get(teamKey);
        if (d) send(d.key);
      });
    }
    prev.current = live;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- a new presence answer is the trigger
  }, [live]);

  // ── Replay last run: a run focus with dots, 4×, ≤12s; lit at once when still ──────────────
  const replay = async () => {
    setReplaying(true);
    const found = await lastRunReplay(projectId);
    const plan = found?.plan ?? [];
    if (!found || !plan.length) { setReplaying(false); toast("error", "No handoffs to replay"); return; }
    const roles = [plan[0].from, ...plan.map((h) => h.to)];
    const still = reducedMotion();
    setFocus({ roles, subject: (await runPath(found.runId, projectId)).subject, runId: found.runId });
    setLitCount(still ? roles.length : 1);
    if (!still) {
      plan.forEach((hop, i) => {
        timers.current.push(setTimeout(() => { if (!mounted.current) return; setLitCount(i + 2); send(`${hop.from}>${hop.to}`); }, hop.at));
      });
    }
    timers.current.push(setTimeout(() => { if (mounted.current) setReplaying(false); }, (still ? 0 : plan[plan.length - 1].at) + 800));
  };
  const clearFocus = () => {
    if (props.focusRunId) props.onClearFocus?.();
    setFocus(null);
    setLitCount(0);
  };

  // ── keyboard: a roving list of nodes in column order; T toggles Map/Table ─────────────────
  const nodeOrder = useMemo(() => cols.map((c) => c.teams), [cols]);
  const [activeNode, setActiveNode] = useState<string>(cols[0]?.teams[0] ?? "");
  const nodeButtons = useRef(new Map<string, HTMLElement>());
  const onKeyDown = (e: React.KeyboardEvent) => {
    const target = e.target as HTMLElement;
    if (target.closest("input, textarea, select")) return;
    if ((e.key === "t" || e.key === "T") && !e.metaKey && !e.ctrlKey && !e.altKey) {
      e.preventDefault();
      const next = mode === "map" ? "table" : "map";
      props.onMode(next);
      // Focus stays on the toggle (walk D10): the map may unmount, and T must work again.
      requestAnimationFrame(() => document.querySelector<HTMLElement>(`[data-testid="ag-flow-mode-${next}"]`)?.focus());
      return;
    }
    const team = target.dataset?.flowNode;
    if (!team) return;
    const ci = nodeOrder.findIndex((c) => c.includes(team));
    const ri = nodeOrder[ci]?.indexOf(team) ?? -1;
    let next: string | undefined;
    if (e.key === "ArrowRight") next = nodeOrder[Math.min(ci + 1, nodeOrder.length - 1)]?.[Math.max(0, Math.min(ri, (nodeOrder[ci + 1]?.length ?? 1) - 1))];
    else if (e.key === "ArrowLeft") next = nodeOrder[Math.max(ci - 1, 0)]?.[Math.max(0, Math.min(ri, (nodeOrder[ci - 1]?.length ?? 1) - 1))];
    else if (e.key === "ArrowDown") next = nodeOrder[ci]?.[Math.min(ri + 1, nodeOrder[ci].length - 1)];
    else if (e.key === "ArrowUp") next = nodeOrder[ci]?.[Math.max(ri - 1, 0)];
    else if (e.key === "Home") next = nodeOrder[0]?.[0];
    else if (e.key === "End") next = nodeOrder[nodeOrder.length - 1]?.slice(-1)[0];
    if (next) {
      e.preventDefault();
      setActiveNode(next);
      nodeButtons.current.get(next)?.focus();
    }
  };

  const noEdges = teamEdges.length === 0;
  const gateTotal = teams.reduce((n, t) => n + t.waitingYou, 0);
  const edgeTitle = (d: Drawn) => {
    if (d.gate) return `${teamLabel(d.from, board)} work waiting on you · ${d.edge.waiting}`;
    const name = (k: string) => (d.level === "team" ? teamLabel(k, board) : nameOf(k));
    return `${name(d.from)} → ${name(d.to)} · ${edgeSummary(d.edge)}${d.edge.lastAt ? ` · last ${ageWords(d.edge.lastAt)} ago` : ""}`;
  };
  const showMap = !narrow && mode === "map";
  const tableRows = teamEdges.filter((e) => !tableTeam || e.from === tableTeam || e.to === tableTeam);

  return (
    <div className="ag-flow" data-testid="ag-flow" data-mode={showMap ? "map" : "table"} onKeyDown={onKeyDown}>
      <div className="ag-flow-bar">
        {narrow ? null : (
          <span className="ag-seg" role="radiogroup" aria-label="Flow as">
            {(["map", "table"] as const).map((m) => (
              <button key={m} type="button" role="radio" aria-checked={mode === m} aria-pressed={mode === m}
                      data-testid={`ag-flow-mode-${m}`} title={m === "map" ? "Teams as a map (T)" : "Every handoff as a table (T)"}
                      onClick={() => props.onMode(m)}>{m === "map" ? "Map" : "Table"}</button>
            ))}
          </span>
        )}
        <button type="button" className="ag-btn" disabled={replaying} data-testid="ag-replay"
                title="Plays the last run's handoffs at 4× speed." onClick={() => void replay()}>
          <Play size={12} aria-hidden /> {replaying ? "Replaying…" : "Replay last run"}
        </button>
        {focus ? (
          <span className="ag-flow-chips">
            <button type="button" className="ag-chip ag-zones-focus" data-testid="ag-zones-focus"
                    title="Clear the run focus" onClick={clearFocus}>
              <span data-user-text>{focus.notFound ? "Run not found" : focus.subject}</span> <X size={11} weight="bold" aria-hidden />
            </button>
            {!focus.notFound ? <a className="ag-link" href={runHref(focus.runId)}>Open run ↗</a> : null}
          </span>
        ) : null}
        {isolate && !focus ? (
          <button type="button" className="ag-chip" data-testid="ag-flow-isolate" title="Show every team"
                  onClick={() => props.onIsolate(null)}>
            {teamLabel(isolate, board)} <X size={11} weight="bold" aria-hidden />
          </button>
        ) : null}
        <span className="ag-spacer" />
        {showMap && hidden > 0 ? (
          <button type="button" className="ag-link" data-testid="ag-flow-hidden" onClick={() => props.onMode("table")}>
            +{hidden} smaller handoffs in Table
          </button>
        ) : null}
      </div>

      {noEdges ? (
        <div className="ag-empty ag-flow-empty" data-testid="ag-flow-empty">
          {days === 30 ? (
            <>
              <span>No handoffs yet</span>
              <a className="ag-btn" href={consoleHref({ area: "delivery_workflows", view: null, layout: null, days: null,
                                                         isolate: null, run: null, focus: null, role: null })}>Start work ↗</a>
              <button type="button" className="ag-link" onClick={() => openPlan()}>
                Plan it with <span data-user-text>{identityOf("pm").callsign || identityOf("pm").name}</span>
              </button>
            </>
          ) : (
            <>
              <span>No handoffs in {windowWords(days)}</span>
              <button type="button" className="ag-btn" data-testid="ag-flow-widen" onClick={() => props.onWindow(30)}>30d</button>
            </>
          )}
        </div>
      ) : null}

      {showMap ? (
        <div className="ag-flow-stage" ref={stage} role="group" aria-label="Team flow">
          <svg className="ag-flow-edges" width={size.w} height={size.h} aria-hidden>
            <defs>
              {["base", "failed", "waiting", "lit", "gate"].map((k) => (
                <marker key={k} id={`ag-flow-arrow-${k}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7"
                        markerHeight="7" orient="auto-start-reverse">
                  <path d="M0 0 L10 5 L0 10 z" className={`ag-flow-arrow ag-flow-arrow-${k}`} />
                </marker>
              ))}
            </defs>
            {drawn.map((d) => {
              const path = pathOf(d);
              if (!path) return null;
              const state = d.failedHop || d.edge.failed ? "failed" : d.edge.waiting ? "waiting" : d.edge.stale ? "stale" : undefined;
              const lit = d.lit || flash.has(d.key);
              const arrow = d.gate ? "gate" : state === "failed" ? "failed" : state === "waiting" ? "waiting" : lit ? "lit" : "base";
              return (
                <path key={`${d.level}:${d.key}`} d={path} className="ag-flow-edge" data-edge={d.key} data-level={d.level}
                      data-state={state} data-dim={d.dim || undefined} data-gate={d.gate || undefined}
                      data-lit={lit || undefined} markerEnd={`url(#ag-flow-arrow-${arrow})`}
                      style={{ strokeWidth: edgeWidth(d.edge.handoffs),
                               strokeOpacity: d.dim ? undefined : 0.55 + 0.45 * (Math.min(d.edge.handoffs, maxVolume) / maxVolume) }}
                      onClick={() => (d.gate ? undefined : props.onOpenEdge({ from: d.from, to: d.to, level: d.level }))}>
                  <title>{edgeTitle(d)}</title>
                </path>
              );
            })}
            {travels.map((t) => {
              const d = drawn.find((x) => x.key === t.edge);
              const path = d ? pathOf(d) : null;
              if (!path) return null;
              return (
                <circle key={t.key} r={5} className="ag-swarm-dot" data-edge={t.edge}>
                  <animateMotion dur="0.6s" fill="freeze" path={path} />
                </circle>
              );
            })}
          </svg>
          {/* EVERY EDGE SAYS ITS VOLUME (walk D5) and its state in words: `×10 · 1 failed ✕`. Each
              of a two-way pair has its own label, offset with its curve (D4). */}
          {drawn.filter((d) => !d.dim).map((d) => {
            const at = routes.get(`${d.level}:${d.key}`);
            if (!at) return null;
            const words = d.gate ? [`${d.edge.waiting} waiting ⏸`]
              : [`×${d.edge.handoffs}`, ...(d.failedHop && !d.edge.failed ? ["failed ✕"] : []), ...edgeStateWords(d.edge)];
            const state = d.failedHop || d.edge.failed ? "failed" : d.edge.waiting ? "waiting" : d.edge.stale ? "stale" : undefined;
            return (
              <button key={`label:${d.level}:${d.key}`} type="button" className="ag-flow-edge-label"
                      data-edge={d.key} data-state={state} data-gate={d.gate || undefined}
                      style={{ left: at.x, top: at.y }} title={edgeTitle(d)}
                      onClick={() => (d.gate ? props.onShowWaits?.() : props.onOpenEdge({ from: d.from, to: d.to, level: d.level }))}>
                {words.join(" · ")}
              </button>
            );
          })}
          <div className="ag-flow-cols">
            {cols.map((c, ci) => (
              <div key={c.key} className="ag-flow-col" data-muted={c.muted || undefined} ref={anchor(`c:${ci}`)}>
                <span className="ag-flow-colhead">
                  {c.muted ? `No work in ${windowWords(days)}`
                    : ci === 0 ? "Starts here"
                      : ci === cols.filter((x) => !x.muted).length - 1 ? "Ends here" : " "}
                </span>
                {c.teams.map((key) => key === "gate" ? (
                  <div key="gate" className="ag-flow-node ag-flow-gate" data-team="gate"
                       data-dim={litTeams && ![...litTeams].some((t) => teams.find((x) => x.key === t)?.waitingYou) ? "isolate" : undefined}>
                    <span className="ag-flow-node-btn ag-flow-you" ref={anchor("t:gate")} data-testid="ag-flow-gate"
                          title="Work held at a gate, waiting on your decision">
                      <span aria-hidden>⏸</span> You <span className="ag-flow-waits" data-state="waiting">{gateTotal} waiting</span>
                    </span>
                  </div>
                ) : key === "person" ? (
                  <div key="person" className="ag-flow-node" data-team="person"
                       data-dim={litTeams && !litTeams.has("person") ? "isolate" : undefined}>
                    <span className="ag-flow-node-btn ag-flow-you" ref={anchor("t:person")}>
                      <span className="ag-org-you" aria-hidden>●</span> You
                    </span>
                  </div>
                ) : (
                  <FlowNode key={key} {...props} team={teams.find((t) => t.key === key)!} anchor={anchor}
                            expanded={expanded.get(key)} onPath={onPath} failedRoles={focus?.failed}
                            dim={litTeams && !litTeams.has(key) ? "isolate" : undefined}
                            active={activeNode === key} setActive={setActiveNode}
                            nodeRef={(el) => { if (el) nodeButtons.current.set(key, el); else nodeButtons.current.delete(key); }}
                            onMore={() => { setTableTeam(key); props.onMode("table"); }} />
                ))}
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {showMap && idleTeams.length ? (
        <span className="ag-flow-idle" data-testid="ag-flow-idle" title="Nobody on these teams was handed work">
          No work in {windowWords(days)} · {idleTeams.map((k) => teamLabel(k, board)).join(" · ")}
        </span>
      ) : null}
      <StuckNow board={board} teams={teams} />

      <FlowTable board={board} rows={tableRows} search={search} visible={!showMap} teamOf={teamOf}
                 filter={tableTeam} onClearFilter={() => setTableTeam(null)} onOpenEdge={props.onOpenEdge} />
    </div>
  );
}

function FlowNode(p: OrgFlowProps & {
  team: OrgTeam; anchor: (k: string) => (el: HTMLElement | null) => void;
  expanded?: { roles: string[]; more: number }; onPath: Set<string>; dim?: string; active: boolean;
  setActive: (k: string) => void; nodeRef: (el: HTMLElement | null) => void; onMore: () => void;
  failedRoles?: string[];
}) {
  const { team, board, live, search, anchor, expanded, onPath, dim, isolate } = p;
  const label = teamLabel(team.key, board);
  const out = board.teamEdges.filter((e) => e.from === team.key && e.to !== team.key);
  const top = out[0];
  const failed = out.reduce((n, e) => n + e.failed, 0);
  const aria = `${label}, ${team.members.length} agents${top ? `, hands to ${teamLabel(top.to, board)} ${top.handoffs} times` : ""}${failed ? `, ${failed} failed` : ""}`;
  const faces = liveFirst(team.members, live).slice(0, NODE_FACES);
  return (
    <div className="ag-flow-node" data-team={team.key} data-dim={dim} data-isolated={isolate === team.key || undefined}
         data-expanded={expanded ? true : undefined} style={{ ["--team-hue" as string]: teamHue(team.key) }}>
      <button type="button" className="ag-flow-node-btn" data-flow-node={team.key}
              ref={(el) => { p.nodeRef(el); anchor(`t:${team.key}`)(el); }}
              data-testid={`ag-flow-node-${team.key}`} aria-label={aria} aria-pressed={isolate === team.key}
              tabIndex={p.active ? 0 : -1} onFocus={() => p.setActive(team.key)}
              onClick={() => p.onIsolate(isolate === team.key ? null : team.key)}>
        <span className="ag-team-label">{label}</span>
        <span className="ag-team-count">{team.members.length}</span>
        <span className="ag-flow-node-faces" aria-hidden>
          {faces.map((r) => (
            <span key={r} className="ag-team-face" data-dim={matches(search, { roleKey: r, name: nameOf(r) }) ? undefined : "search"}>
              <AgFace roleKey={r} size={24} state={liveState(live, r)} decorative aiBadge={false} />
            </span>
          ))}
        </span>
        {team.selfHandoffs ? <span className="ag-flow-inside" title="Handoffs inside this team">↻ {team.selfHandoffs} inside</span> : null}
        {team.waitingTotal || team.work.failed ? (
          <span className="ag-flow-waits-row">
            {team.waitingTotal ? (
              <span className="ag-flow-waits" data-state="waiting" title={`Open since ${team.waiting[0]?.since ? new Date(team.waiting[0].since).toLocaleString() : "unknown"}`}>
                Waiting {team.waitingTotal} · oldest {ageWords(team.waiting[0]?.since ?? null)}
              </span>
            ) : null}
            {team.work.failed ? (
              <span className="ag-flow-waits" data-state="failed" title="Ended in an error in this window">Failed {team.work.failed}</span>
            ) : null}
          </span>
        ) : null}
      </button>
      {expanded ? (
        <div className="ag-flow-agents">
          {expanded.roles.map((r) => (
            <button key={r} type="button" className="ag-flow-agent" ref={anchor(`a:${r}`)} data-role={r}
                    data-testid={`ag-flow-agent-${r}`} data-lit={onPath.has(r) || undefined}
                    data-failed={p.failedRoles?.includes(r) || undefined}
                    data-selected={p.selected === r || undefined}
                    data-dim={matches(search, { roleKey: r, name: nameOf(r) }) ? undefined : "search"}
                    aria-label={`${nameOf(r)}, ${label}`} title={live.get(r)?.line || nameOf(r)}
                    onClick={(e) => {
                      if (e.shiftKey) { openAgent(r); return; }
                      p.onSelect(p.selected === r ? null : r);
                    }}
                    onKeyDown={(e) => { if (e.key === "Enter" && e.shiftKey) { e.preventDefault(); openAgent(r); } }}>
              <span className="ag-team-face" data-still={!isLive(live.get(r)) || undefined}>
                <AgFace roleKey={r} size={32} state={liveState(live, r)} decorative aiBadge={false} />
              </span>
              <span className="ag-flow-agent-name" data-user-text>{nameOf(r)}</span>
            </button>
          ))}
          {expanded.more > 0 ? (
            <button type="button" className="ag-link" onClick={p.onMore}>+{expanded.more} more</button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function FlowTable({ board, rows, search, visible, teamOf, filter, onClearFilter, onOpenEdge }: {
  board: OrgBoard; rows: OrgEdge[]; search: string; visible: boolean; teamOf: (r: string) => string;
  filter: string | null; onClearFilter: () => void; onOpenEdge: (f: EdgeFocus) => void;
}) {
  const [open, setOpen] = useState<Set<string>>(new Set());
  const q = search.trim();
  const pairsOf = (e: OrgEdge) => board.agentEdges.filter((a) => teamOf(a.from) === e.from && teamOf(a.to) === e.to
    && (!q || matches(q, { roleKey: a.from, name: nameOf(a.from) }) || matches(q, { roleKey: a.to, name: nameOf(a.to) })));
  const shown = q ? rows.filter((e) => pairsOf(e).length) : rows;
  return (
    <div className={visible ? "ag-flow-tablewrap" : "ag-sr"}>
      {filter && visible ? (
        <button type="button" className="ag-chip" onClick={onClearFilter} title="Show every edge">
          {teamLabel(filter, board)} <X size={11} weight="bold" aria-hidden />
        </button>
      ) : null}
      <table className="ag-flow-table" data-testid="ag-flow-table" aria-label="Handoffs between teams" tabIndex={visible ? undefined : 0}>
        <thead><tr><th>From</th><th>To</th><th>Handoffs</th><th>Failed</th><th>Waiting</th><th>Last</th></tr></thead>
        <tbody>
          {shown.map((e) => {
            const key = `${e.from}>${e.to}`;
            const isOpen = open.has(key);
            const pairs = pairsOf(e);
            return (
              <React.Fragment key={key}>
                <tr data-testid={`ag-flow-row-${e.from}-${e.to}`} data-state={e.failed ? "failed" : e.waiting ? "waiting" : undefined}>
                  <td>
                    <button type="button" className="ag-icon-btn" aria-expanded={isOpen}
                            aria-label={`${isOpen ? "Hide" : "Show"} agent pairs`} title="Agent pairs"
                            onClick={() => setOpen((s) => { const n = new Set(s); if (n.has(key)) n.delete(key); else n.add(key); return n; })}>
                      {isOpen ? <CaretDown size={12} aria-hidden /> : <CaretRight size={12} aria-hidden />}
                    </button>
                    <button type="button" className="ag-link" onClick={() => onOpenEdge({ from: e.from, to: e.to, level: "team" })}>
                      {teamLabel(e.from, board)}
                    </button>
                  </td>
                  <td><span className="ag-flow-arrow" aria-hidden>→ </span>{teamLabel(e.to, board)}</td>
                  <td data-label="Handoffs">{e.handoffs}</td>
                  <td data-label="Failed">{e.failed ? `${e.failed} failed ✕` : "0"}</td>
                  <td data-label="Waiting">{e.waiting ? `${e.waiting} waiting ⏸` : "0"}</td>
                  <td data-label="Last" title={e.lastAt ? new Date(e.lastAt).toLocaleString() : undefined}>{e.lastAt ? `${ageWords(e.lastAt)} ago` : ""}</td>
                </tr>
                {isOpen ? pairs.map((a) => (
                  <tr key={`${key}:${a.from}>${a.to}`} className="ag-flow-pair">
                    <td><button type="button" className="ag-link" onClick={() => onOpenEdge({ from: a.from, to: a.to, level: "agent" })}>
                      <span data-user-text>{nameOf(a.from)}</span></button></td>
                    <td><span className="ag-flow-arrow" aria-hidden>→ </span><span data-user-text>{nameOf(a.to)}</span></td>
                    <td data-label="Handoffs">{a.handoffs}</td>
                    <td data-label="Failed">{a.failed ? `${a.failed} failed ✕` : "0"}</td>
                    <td data-label="Waiting">{a.waiting ? `${a.waiting} waiting ⏸` : "0"}</td>
                    <td data-label="Last">{a.lastAt ? `${ageWords(a.lastAt)} ago` : ""}</td>
                  </tr>
                )) : null}
              </React.Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** `Stuck now` (walk D3): under the map, where work is stuck — the oldest waits and the newest
 *  failures, each with who, how long, what, and the one link that acts on it. */
function StuckNow({ board, teams }: { board: OrgBoard; teams: OrgTeam[] }) {
  const waits = teams.flatMap((t) => t.waiting).sort((a, b) => (a.since ?? "").localeCompare(b.since ?? "")).slice(0, 5);
  const failed = board.failures.slice(0, 5);
  if (!waits.length && !failed.length) return null;
  return (
    <section className="ag-flow-stuck" data-testid="ag-flow-stuck" aria-labelledby="ag-flow-stuck-title">
      <span className="ag-flow-stuck-title" id="ag-flow-stuck-title">Stuck now</span>
      <div className="ag-flow-stuck-cols">
        {waits.length ? (
          <ul className="ag-org-list" aria-label="Waiting">
            {waits.map((w) => (
              <li key={w.actId} data-testid={`ag-flow-stuck-wait-${w.actId}`}>
                <AgFace roleKey={w.roleKey} size={24} decorative />
                <WaitItem item={w} />
              </li>
            ))}
          </ul>
        ) : null}
        {failed.length ? (
          <ul className="ag-org-list" aria-label="Failed">
            {failed.map((f) => (
              <li key={f.actId} data-testid={`ag-flow-stuck-failed-${f.actId}`}>
                <AgFace roleKey={f.roleKey} size={24} decorative />
                <StuckItem roleKey={f.roleKey} age={`${ageWords(f.at)} ago`} ageTip={f.at ? new Date(f.at).toLocaleString() : undefined}
                           state="failed" word="Failed ✕" label={f.label}
                           action={f.runId ? <a className="ag-link" href={runHref(f.runId)}>Open run ↗</a> : null} />
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    </section>
  );
}
