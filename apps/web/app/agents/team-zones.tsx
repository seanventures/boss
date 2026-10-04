"use client";

/**
 * Teams as soft zones (agents-home spec §4.6): Live › Agent force (`compact`) and
 * Organization › Zones (`full`) — one component at two densities, so the two teach the same
 * spatial memory.
 *
 * TEAMS COME FROM ONE PLACE: the board's `teams` (agent_identity.team_of on the server). Faces
 * are in their REAL state from presence; idle faces are still. A team is a soft tint of its hue
 * from the avatar kit's palette, never a new colour.
 *
 * NO SPAGHETTI. Full density draws edges for ONE thing at a time: the selected agent's observed
 * handoffs over 7 days, or one focused run's chain (`?run=`). A live handoff sends a dot only
 * along an edge that is already drawn; otherwise the receiving face's ring just changes state.
 * Under 640px the map becomes a drill-down list: team rows, then their members.
 */
import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Bell, ChatCircleDots, DotsThree, Gear, X } from "@phosphor-icons/react";

import type { AvatarState } from "../../lib/agent-world-types";
import {
  TEAM_LABELS, TEAM_ORDER, type BoardAgent, type LiveBoard, type TeamSummary,
} from "../../lib/agents-live-board";
import {
  STATE_WORD, askAgent, getLineage, getRunChain, handedByRole, identityOf, isLive, openAgent, toast,
} from "../../lib/agent-world";
import type { LiveMap } from "../../lib/agents-live";
import { updateAgent } from "../../lib/agents";
import { PALETTE_BY_ID } from "../../lib/avatar-kit/palettes";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { AgFace, AgFaceStack } from "./agent-face";
import { curvePath } from "./team-swarm";

export type TeamZonesProps = {
  projectId: string;
  board: LiveBoard;
  density: "compact" | "full";
  /** The header's `Find an agent` text: non-matching faces dim to 30%. */
  search?: string;
  /** Organization mission focus (`?run=`): that run's agents ring, others dim. */
  focusRunId?: string | null;
  onClearFocus?: () => void;
  /** A team focused from Live's `Open map ↗` (`?focus=<team>`). */
  focusTeam?: string | null;
  /** Compact only: `Open map ↗`, optionally with a team to focus. */
  onOpenMap?: (teamKey?: string) => void;
  /** OPTIONAL (S3): presence with act ids, so a live handoff can send its dot on a drawn edge. */
  live?: LiveMap;
  /** OPTIONAL (S3): the signed-in actor. `⋯ → Move to…` is offered only when it is given. */
  actor?: string;
  /** OPTIONAL (S3): called after `Move to…` saved, so the shell can refresh the board. */
  onChanged?: () => void;
  /** OPTIONAL (S3): most faces that may animate at once (spec §5.2 budget). Default 8. */
  animateMax?: number;
};

// ── Model ───────────────────────────────────────────────────────────────────────────────────

/** Each shipped team's hue: a palette from the avatar kit (never a new colour). */
const TEAM_HUE: Record<string, string> = {
  leadership: "blue", architecture: "purple", engineering: "teal", quality: "coral",
  operations: "amber", cost: "green", media: "pink", thinkers: "mint", yours: "slate",
};
export function teamHue(key: string): string {
  return (PALETTE_BY_ID[TEAM_HUE[key] ?? "slate"] ?? PALETTE_BY_ID.slate).body;
}

export type ZoneMember = { roleKey: string; name: string; state: AvatarState; line: string; live: boolean };
export type ZoneTeam = TeamSummary & { tip?: string; people: ZoneMember[] };

const LIVE_ORDER: Partial<Record<AvatarState, number>> = { needs_you: 0, stuck: 1, working: 2, speaking: 3, thinking: 4 };

export function faceStateOf(a: Pick<BoardAgent, "state"> | undefined): AvatarState {
  if (!a) return "idle";
  return a.state === "in_panel" ? "speaking" : a.state;
}

/** Teams in the shipped order (operator-named teams before Yours), members live-first. */
export function zoneTeams(board: LiveBoard, moved: Record<string, string> = {}): ZoneTeam[] {
  const agents = new Map(board.agents.map((a) => [a.roleKey, a]));
  const teams = new Map<string, ZoneTeam>();
  for (const t of board.teams) {
    teams.set(t.key, { ...t, tip: t.tip ?? TEAM_LABELS[t.key]?.tip, members: [...t.members], people: [] });
  }
  // A move saved here shows at once; the next board read agrees with it.
  for (const [roleKey, to] of Object.entries(moved)) {
    for (const t of teams.values()) t.members = t.members.filter((m) => m !== roleKey);
    if (!teams.has(to)) {
      teams.set(to, { key: to, label: TEAM_LABELS[to]?.label ?? to, tip: TEAM_LABELS[to]?.tip, members: [],
                      working: 0, needsYou: 0, inPanel: 0, people: [] });
    }
    teams.get(to)!.members.push(roleKey);
  }
  for (const t of teams.values()) {
    t.people = t.members.map((roleKey) => {
      const a = agents.get(roleKey);
      const who = identityOf(roleKey, a?.name);
      const state = faceStateOf(a);
      return { roleKey, name: a?.callsign || who.callsign || a?.name || who.name, state, line: a?.line ?? "",
               live: isLive({ state, line: "" }) };
    }).sort((x, y) => (LIVE_ORDER[x.state] ?? 9) - (LIVE_ORDER[y.state] ?? 9));
    if (Object.keys(moved).length) {
      t.working = t.people.filter((p) => ["working", "thinking", "speaking"].includes(p.state)).length;
    }
    // NEEDS YOU IS THE SERVER'S: this team's agents' own open asks, counted as items. A gate
    // waiting on you is yours, never a team's, so it is not counted here (agents-home D4).
  }
  // An empty team is not a place: `Yours 0` is never drawn.
  for (const [key, t] of teams) if (key === "yours" && t.members.length === 0) teams.delete(key);
  const rank = (k: string) => {
    const i = TEAM_ORDER.indexOf(k);
    return i < 0 ? TEAM_ORDER.length - 1.5 : i;
  };
  return [...teams.values()].sort((a, b) => rank(a.key) - rank(b.key) || a.label.localeCompare(b.label));
}

function matches(search: string, m: ZoneMember): boolean {
  const q = search.trim().toLowerCase();
  if (!q) return true;
  const who = identityOf(m.roleKey);
  return `${m.name} ${who.name} ${m.roleKey}`.toLowerCase().includes(q);
}

function reducedMotion(): boolean {
  return typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}

/** True under 640px: the map becomes a list. */
export function useNarrow(): boolean {
  const query = "(max-width: 639px)";
  const [narrow, setNarrow] = useState(() => typeof window !== "undefined" && !!window.matchMedia?.(query).matches);
  useEffect(() => {
    const mq = typeof window !== "undefined" ? window.matchMedia?.(query) : undefined;
    if (!mq) return undefined;
    const on = () => setNarrow(mq.matches);
    on();
    mq.addEventListener?.("change", on);
    return () => mq.removeEventListener?.("change", on);
  }, []);
  return narrow;
}

/** `1 needs you` / `2 need you`: the grammar every count of items waiting on you uses. */
export function needYouWords(n: number): string {
  return `${n} ${n === 1 ? "needs" : "need"} you`;
}

/** The face an arrow key moves to: Left/Right along the row the face sits in, Up/Down to the
 *  nearest face in the row above or below (across zones), Home/End to the first and last.
 *  Pure, from the faces' rectangles, so the grid is 2D whatever the zones' layout. */
export function nextInGrid(rects: { left: number; top: number; width: number; height: number }[],
                           at: number, key: string): number {
  const cur = rects[at];
  if (!cur) return at;
  if (key === "Home") return 0;
  if (key === "End") return rects.length - 1;
  // No layout (a server render, a test DOM): every face sits at one point. Fall back to order.
  if (rects.every((r) => r.left === cur.left && r.top === cur.top)) {
    if (key === "ArrowRight" || key === "ArrowDown") return Math.min(rects.length - 1, at + 1);
    if (key === "ArrowLeft" || key === "ArrowUp") return Math.max(0, at - 1);
    return at;
  }
  const cx = (r: typeof cur) => r.left + r.width / 2;
  const cy = (r: typeof cur) => r.top + r.height / 2;
  const sameRow = (r: typeof cur) => Math.abs(cy(r) - cy(cur)) < cur.height / 2;
  let best = -1;
  let score = Infinity;
  rects.forEach((r, i) => {
    if (i === at) return;
    let d = Infinity;
    if (key === "ArrowRight" && sameRow(r) && cx(r) > cx(cur)) d = cx(r) - cx(cur);
    else if (key === "ArrowLeft" && sameRow(r) && cx(r) < cx(cur)) d = cx(cur) - cx(r);
    else if (key === "ArrowDown" && cy(r) - cy(cur) >= cur.height / 2) d = (cy(r) - cy(cur)) * 1000 + Math.abs(cx(r) - cx(cur));
    else if (key === "ArrowUp" && cy(cur) - cy(r) >= cur.height / 2) d = (cy(cur) - cy(r)) * 1000 + Math.abs(cx(r) - cx(cur));
    if (d < score) { score = d; best = i; }
  });
  return best < 0 ? at : best;
}

/** `2 working` · `1 needs you`: words, never colour alone. */
function Badges({ team }: { team: Pick<TeamSummary, "working" | "needsYou"> }) {
  return (
    <>
      {team.needsYou ? (
        <span className="ag-team-badge" data-kind="needs_you"><Bell size={11} weight="bold" aria-hidden />{needYouWords(team.needsYou)}</span>
      ) : null}
      {team.working ? (
        <span className="ag-team-badge" data-kind="working"><Gear size={11} weight="bold" aria-hidden />{team.working} working</span>
      ) : null}
    </>
  );
}

/** A face that may move only if it is live and inside the budget; everything else is still. */
function Face({ m, size, still, dim }: { m: ZoneMember; size: 24 | 32 | 48; still: boolean; dim?: string }) {
  return (
    <span className="ag-team-face" data-still={still || undefined} data-dim={dim} data-state={m.state}>
      <AgFace roleKey={m.roleKey} size={size} state={m.state} decorative aiBadge={false} />
    </span>
  );
}

// ── Component ───────────────────────────────────────────────────────────────────────────────

export function TeamZones(props: TeamZonesProps): React.ReactElement | null {
  const v = useIdentityVersion(); // names and faces are resolved at render: repaint when identities load
  const [moved, setMoved] = useState<Record<string, string>>({});
  const teams = useMemo(() => zoneTeams(props.board, moved), [props.board, moved, v]); // eslint-disable-line react-hooks/exhaustive-deps -- `v` versions the store zoneTeams reads
  const narrow = useNarrow();
  if (props.density === "compact") return <CompactZones {...props} teams={teams} />;
  if (narrow) return <ZoneList teams={teams} search={props.search ?? ""} />;
  return <FullZones {...props} teams={teams} onMoved={(k, t) => setMoved((m) => ({ ...m, [k]: t }))} />;
}

// ── Compact: Live › Agent force ─────────────────────────────────────────────────────────────

const COMPACT_FACES = 6;

function CompactZones({ teams, search = "", focusTeam, onOpenMap, animateMax = 8 }: TeamZonesProps & { teams: ZoneTeam[] }) {
  // THE MOTION BUDGET: at most `animateMax` live faces move; past that they are drawn still and
  // their state is still said by the badge words.
  let budget = animateMax;
  return (
    <div className="ag-team-force" data-testid="ag-team-force" data-density="compact">
      {teams.map((t) => {
        const shown = t.people.slice(0, COMPACT_FACES);
        const stills = shown.map((m) => {
          if (!m.live) return true;
          if (budget <= 0) return true;
          budget -= 1;
          return false;
        });
        return <CompactTeam key={t.key} team={t} shown={shown} stills={stills} search={search}
                            focused={focusTeam === t.key} onOpenMap={onOpenMap} />;
      })}
    </div>
  );
}

function CompactTeam({ team, shown, stills, search, focused, onOpenMap }: {
  team: ZoneTeam; shown: ZoneMember[]; stills: boolean[]; search: string; focused: boolean;
  onOpenMap?: (teamKey?: string) => void;
}) {
  useIdentityVersion(); // names are resolved at render
  const [open, setOpen] = useState(focused);
  useEffect(() => { if (focused) setOpen(true); }, [focused]);
  const allIdle = !team.people.some((m) => m.live);
  const more = team.people.length - shown.length;
  const label = TEAM_LABELS[team.key]?.label ?? team.label;
  return (
    <div className="ag-team-zone" data-team={team.key} data-open={open || undefined} data-idle={allIdle || undefined}
         style={{ ["--team-hue" as string]: teamHue(team.key) }}>
      <button type="button" className="ag-team-pill" aria-expanded={open} data-testid={`ag-team-${team.key}`}
              aria-label={`${label}, ${team.people.length}`} onClick={() => setOpen((v) => !v)}>
        <span className="ag-team-label" title={team.tip}>{label}</span>
        <span className="ag-team-count">{team.people.length}</span>
        {allIdle ? (
          team.people.length ? <AgFaceStack roleKeys={team.people.map((m) => m.roleKey)} size={24} max={3} decorative /> : null
        ) : (
          <span className="ag-team-faces" aria-hidden>
            {shown.map((m, i) => (
              <Face key={m.roleKey} m={m} size={24} still={stills[i]} dim={matches(search, m) ? undefined : "search"} />
            ))}
            {more > 0 ? <span className="ag-team-more">+{more}</span> : null}
          </span>
        )}
        <Badges team={team} />
      </button>
      {open ? (
        <div className="ag-team-members" data-testid={`ag-team-members-${team.key}`}>
          <ul className="ag-team-list">
            {team.people.map((m) => (
              <li key={m.roleKey} className="ag-team-row" data-dim={matches(search, m) ? undefined : "search"}>
                <button type="button" className="ag-team-who" onClick={() => openAgent(m.roleKey)}
                        data-testid={`ag-team-open-${m.roleKey}`} title={`Open ${m.name}`}>
                  <Face m={m} size={24} still={!m.live} />
                  <span className="ag-team-name" data-user-text>{m.name}</span>
                  <span className="ag-team-line ag-trunc" title={m.line} data-user-text>{m.line}</span>
                </button>
                <button type="button" className="ag-icon-btn" aria-label={`Talk to ${m.name}`} title={`Talk to ${m.name}`}
                        onClick={() => askAgent(m.roleKey, m.name, { from: "roster" })}>
                  <ChatCircleDots size={16} weight="bold" aria-hidden />
                </button>
              </li>
            ))}
          </ul>
          {onOpenMap ? (
            <button type="button" className="ag-link" data-testid={`ag-team-map-${team.key}`}
                    onClick={() => onOpenMap(team.key)}>Open map ↗</button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

// ── Narrow: the drill-down list ─────────────────────────────────────────────────────────────

function ZoneList({ teams, search }: { teams: ZoneTeam[]; search: string }) {
  useIdentityVersion(); // names are resolved at render
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div className="ag-zones ag-zones-list" data-testid="ag-zones" data-layout="list">
      {teams.map((t) => {
        const label = TEAM_LABELS[t.key]?.label ?? t.label;
        return (
          <div key={t.key} className="ag-zones-lrow" data-team={t.key}
               style={{ ["--team-hue" as string]: teamHue(t.key) }}>
            <button type="button" className="ag-zones-lhead" aria-expanded={open === t.key}
                    data-testid={`ag-zones-team-${t.key}`} onClick={() => setOpen(open === t.key ? null : t.key)}>
              <span className="ag-team-label" title={t.tip}>{label}</span>
              <span className="ag-team-count">{t.people.length}</span>
              <Badges team={t} />
            </button>
            {open === t.key ? (
              <ul className="ag-zones-lmembers">
                {t.people.filter((m) => matches(search, m)).map((m) => (
                  <li key={m.roleKey}>
                    <button type="button" className="ag-zones-lmember" onClick={() => openAgent(m.roleKey)}
                            data-testid={`ag-zones-member-${m.roleKey}`}>
                      <Face m={m} size={24} still />
                      <span data-user-text>{m.name}</span>
                      <span className="ag-zones-lstate"> · {STATE_WORD[m.state]}</span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

// ── Full: Organization › Zones ──────────────────────────────────────────────────────────────

type Edge = { from: string; to: string; acts: number; label: string; order?: number };

/** Where each zone sits: Leadership a band on top; Architecture, Engineering, Quality; then
 *  Operations, Cost, Media, Thinkers; Yours (and operator teams) last. Never force-directed. */
/** The fixed map (spec §4.6): Leadership a band, then three, then four, then everyone else. */
const MAP_ROW: Record<string, "band" | "mid" | "low"> = {
  leadership: "band", architecture: "mid", engineering: "mid", quality: "mid",
  operations: "low", cost: "low", media: "low", thinkers: "low",
};
export function mapRowOf(key: string): "band" | "mid" | "low" | "rest" {
  return MAP_ROW[key] ?? "rest";
}

/** A run's agents in the order they took the work, from the board's lane or the run's chain. */
async function runPath(board: LiveBoard, runId: string, projectId: string): Promise<{ roles: string[]; subject: string }> {
  const m = board.missions.find((x) => x.runId === runId);
  const fromHops = (m?.hops ?? []).flatMap((h) => (h.kind === "hop" && h.roleKey ? [h.roleKey] : h.kind === "loop" ? [h.a, h.b] : []));
  let roles = fromHops;
  if (!roles.length) {
    const chain = await getRunChain(runId, projectId);
    roles = chain.ok ? chain.data.acts.filter((a) => a.roleKey).sort((a, b) => a.startedAt.localeCompare(b.startedAt))
      .map((a) => a.roleKey!) : [];
  }
  const path = roles.filter((r, i) => r !== roles[i - 1]);
  return { roles: path, subject: m?.subject || m?.workflowName || "This run" };
}

function FullZones({ projectId, board, teams, search = "", focusRunId, onClearFocus, focusTeam, live, actor,
                     onChanged, onMoved }: TeamZonesProps & { teams: ZoneTeam[]; onMoved: (roleKey: string, team: string) => void }) {
  useIdentityVersion(); // names are resolved at render
  const host = useRef<HTMLDivElement>(null);
  const faces = useRef(new Map<string, HTMLElement>());
  const [selected, setSelected] = useState<string | null>(null);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [path, setPath] = useState<{ roles: string[]; subject: string } | null>(null);
  const [litCount, setLitCount] = useState(0);
  const [points, setPoints] = useState<Map<string, { x: number; y: number }>>(new Map());
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [travels, setTravels] = useState<{ key: string; from: string; to: string }[]>([]);
  const [flash, setFlash] = useState<Set<string>>(new Set());
  const [active, setActive] = useState(0);
  const [menu, setMenu] = useState(false);
  const order = useMemo(() => teams.flatMap((t) => t.people.map((m) => m.roleKey)), [teams]);
  const people = useMemo(() => new Map(teams.flatMap((t) => t.people.map((m) => [m.roleKey, m] as const))), [teams]);

  // ONE AGENT'S EDGES: its observed handoffs, 7 days, to faces on this map.
  useEffect(() => {
    if (!selected) { setEdges([]); return undefined; }
    let alive = true;
    void getLineage(projectId, selected, { days: 7, mode: "observed" }).then((r) => {
      if (!alive || !r.ok) return;
      const agentOf = (id: string) => (id.startsWith("agent:") ? id.slice(6) : "");
      setEdges(r.data.edges.map((e) => ({ from: agentOf(e.from), to: agentOf(e.to), acts: e.acts, label: e.label }))
        .filter((e) => e.from && e.to && e.from !== e.to));
    });
    return () => { alive = false; };
  }, [projectId, selected]);

  // ONE RUN'S PATH: its hops light in order, 400ms each and 4s at most; all at once when still.
  useEffect(() => {
    if (!focusRunId) { setPath(null); setLitCount(0); return undefined; }
    let alive = true;
    const timers: ReturnType<typeof setTimeout>[] = [];
    void runPath(board, focusRunId, projectId).then((p) => {
      if (!alive) return;
      setPath(p);
      if (reducedMotion() || p.roles.length <= 1) { setLitCount(p.roles.length); return; }
      const step = Math.min(400, 4000 / p.roles.length);
      setLitCount(1);
      for (let i = 2; i <= p.roles.length; i += 1) timers.push(setTimeout(() => { if (alive) setLitCount(i); }, step * (i - 1)));
    });
    return () => { alive = false; timers.forEach(clearTimeout); };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the run, not every board poll
  }, [focusRunId, projectId]);

  const focusEdges: Edge[] = useMemo(() => {
    if (!path) return [];
    return path.roles.slice(1, Math.max(1, litCount)).map((to, i) => ({ from: path.roles[i], to, acts: 1, label: "", order: i }));
  }, [path, litCount]);
  const drawn = selected ? edges : focusEdges;
  const drawnKeys = useMemo(() => new Set(drawn.map((e) => `${e.from}>${e.to}`)), [drawn]);

  // Measure faces after layout, so the curves meet them.
  const measure = useCallback(() => {
    const box = host.current?.getBoundingClientRect();
    if (!box) return;
    const next = new Map<string, { x: number; y: number }>();
    for (const [k, el] of faces.current) {
      const r = el.getBoundingClientRect();
      next.set(k, { x: r.left - box.left + r.width / 2, y: r.top - box.top + r.height / 2 });
    }
    setPoints(next);
    setSize({ w: box.width, h: box.height });
  }, []);
  useLayoutEffect(() => { measure(); }, [measure, teams, selected, path]);
  useEffect(() => {
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [measure]);

  // A LIVE HANDOFF on a drawn edge sends one dot; anywhere else the ring just changes.
  const prev = useRef<LiveMap>(new Map());
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);
  useEffect(() => {
    if (!live) return;
    for (const [roleKey, p] of live) {
      if (!isLive(p) || isLive(prev.current.get(roleKey)) || !p.actId) continue;
      void handedByRole(projectId, p.actId).then((from) => {
        const key = `${from}>${roleKey}`;
        if (!mounted.current || !from || !drawnKeys.has(key)) return;
        if (reducedMotion()) {
          setFlash((s) => new Set(s).add(key));
          setTimeout(() => setFlash((s) => { const n = new Set(s); n.delete(key); return n; }), 1000);
          return;
        }
        const t = { key: `${key}:${Date.now()}`, from, to: roleKey };
        setTravels((x) => [...x, t]);
        setTimeout(() => setTravels((x) => x.filter((y) => y.key !== t.key)), 700);
      });
    }
    prev.current = live;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- a new presence answer is the trigger
  }, [live]);

  // Esc clears the selection, then the run focus.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (menu) setMenu(false);
      else if (selected) setSelected(null);
      else if (focusRunId) onClearFocus?.();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected, focusRunId, onClearFocus, menu]);

  useEffect(() => {
    if (!focusTeam) return;
    host.current?.querySelector(`[data-team="${focusTeam}"]`)?.scrollIntoView?.({ block: "nearest" });
  }, [focusTeam]);

  const onPath = new Set(path?.roles.slice(0, litCount) ?? []);
  const connected = new Set(edges.flatMap((e) => [e.from, e.to]));
  const dimOf = (m: ZoneMember): string | undefined => {
    if (!matches(search, m)) return "search";
    if (selected && m.roleKey !== selected && !connected.has(m.roleKey)) return "other";
    if (path && !path.roles.includes(m.roleKey)) return "other";
    return undefined;
  };

  // Roving tabindex: arrows move, Enter selects, Shift+Enter opens the Passport.
  const onKeyDown = (e: React.KeyboardEvent) => {
    const at = order.indexOf((e.target as HTMLElement).dataset?.role ?? "");
    if (at < 0) return;
    let next = at;
    if (["ArrowRight", "ArrowLeft", "ArrowUp", "ArrowDown", "Home", "End"].includes(e.key)) {
      const rects = order.map((k) => {
        const r = faces.current.get(k)?.getBoundingClientRect();
        return r ? { left: r.left, top: r.top, width: r.width || 1, height: r.height || 1 }
          : { left: 0, top: 0, width: 1, height: 1 };
      });
      next = nextInGrid(rects, at, e.key);
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (e.shiftKey) openAgent(order[at]);
      else setSelected((s) => (s === order[at] ? null : order[at]));
      return;
    } else return;
    e.preventDefault();
    setActive(next);
    faces.current.get(order[next])?.focus();
  };

  const move = async (roleKey: string, team: string) => {
    setMenu(false);
    if (!actor) return;
    const r = await updateAgent(projectId, actor, roleKey, { team });
    if (!r.ok) { toast("error", r.error); return; }
    onMoved(roleKey, team);
    toast("success", `Moved to ${TEAM_LABELS[team]?.label ?? team}`);
    onChanged?.();
  };

  const sel = selected ? people.get(selected) : undefined;
  return (
    <div className="ag-zones" data-testid="ag-zones" data-layout="map" data-selected={selected || undefined}
         data-focus-run={focusRunId || undefined}>
      {path || sel ? (
        <div className="ag-zones-bar">
          {path ? (
            <button type="button" className="ag-chip ag-zones-focus" data-testid="ag-zones-focus"
                    title="Clear the run focus" onClick={() => onClearFocus?.()}>
              <span data-user-text>{path.subject}</span> <X size={11} weight="bold" aria-hidden />
            </button>
          ) : null}
          {sel ? (
            <span className="ag-zones-sel" data-testid="ag-zones-selected">
              <span data-user-text>{sel.name}</span>
              {actor ? (
                <span className="ag-zones-menu-wrap">
                  <button type="button" className="ag-icon-btn" aria-label="More" aria-haspopup="menu" aria-expanded={menu}
                          data-testid="ag-zones-more" onClick={() => setMenu((v) => !v)}>
                    <DotsThree size={16} weight="bold" aria-hidden />
                  </button>
                  {menu ? (
                    <span className="ag-zones-menu" role="menu" aria-label="Move to…">
                      <span className="ag-zones-menu-head">Move to…</span>
                      {TEAM_ORDER.map((k) => (
                        <button key={k} type="button" role="menuitem" data-testid={`ag-zones-move-${k}`}
                                disabled={teams.find((t) => t.key === k)?.members.includes(sel.roleKey)}
                                title={TEAM_LABELS[k]?.tip} onClick={() => void move(sel.roleKey, k)}>
                          {TEAM_LABELS[k]?.label ?? k}
                        </button>
                      ))}
                    </span>
                  ) : null}
                </span>
              ) : null}
            </span>
          ) : null}
        </div>
      ) : null}
      <div className="ag-zones-map" ref={host} onKeyDown={onKeyDown} role="group" aria-label="Team map">
        <svg className="ag-zones-edges" width={size.w} height={size.h} aria-hidden>
          {drawn.map((e) => {
            const a = points.get(e.from);
            const b = points.get(e.to);
            if (!a || !b) return null;
            const key = `${e.from}>${e.to}`;
            return (
              <path key={key} d={curvePath(a, b)} className="ag-zones-edge" data-edge={key}
                    data-new={e.order !== undefined || undefined} data-lit={flash.has(key) || undefined}
                    style={{ strokeWidth: Math.min(1 + Math.log2(1 + e.acts), 5) }}>
                {e.label ? <title>{e.label}</title> : null}
              </path>
            );
          })}
          {travels.map((t) => {
            const a = points.get(t.from);
            const b = points.get(t.to);
            if (!a || !b) return null;
            return (
              <circle key={t.key} r={5} className="ag-swarm-dot" data-edge={`${t.from}>${t.to}`}>
                <animateMotion dur="0.6s" fill="freeze" path={curvePath(a, b)} />
              </circle>
            );
          })}
        </svg>
        {teams.map((t) => {
          const label = TEAM_LABELS[t.key]?.label ?? t.label;
          return (
            <section key={t.key} className="ag-zones-zone" data-team={t.key}
                     data-row={mapRowOf(t.key)} data-focus={focusTeam === t.key || undefined}
                     aria-label={`${label} ${t.people.length}`} style={{ ["--team-hue" as string]: teamHue(t.key) }}>
              <header className="ag-zones-head">
                <span className="ag-team-label" title={t.tip}>{label}</span>
                <span className="ag-team-count">{t.people.length}</span>
                <Badges team={t} />
              </header>
              <div className="ag-zones-faces">
                {t.people.map((m) => {
                  const idx = order.indexOf(m.roleKey);
                  const lit = onPath.has(m.roleKey);
                  return (
                    <button key={m.roleKey} type="button" className="ag-zones-agent"
                            ref={(el) => { if (el) faces.current.set(m.roleKey, el); else faces.current.delete(m.roleKey); }}
                            data-role={m.roleKey} data-testid={`ag-zones-agent-${m.roleKey}`}
                            data-dim={dimOf(m)} data-selected={selected === m.roleKey || undefined}
                            data-lit={lit || undefined}
                            tabIndex={idx === active ? 0 : -1}
                            aria-pressed={selected === m.roleKey}
                            aria-label={`${m.name}, ${STATE_WORD[m.state]}${m.line ? `: ${m.line}` : ""}`}
                            title={m.line || undefined}
                            onFocus={() => setActive(idx)}
                            onClick={(e) => {
                              if (e.shiftKey) { openAgent(m.roleKey); return; }
                              setSelected((s) => (s === m.roleKey ? null : m.roleKey));
                            }}>
                      <Face m={m} size={48} still={!m.live} />
                      <span className="ag-zones-name" data-user-text>{m.name}</span>
                    </button>
                  );
                })}
              </div>
            </section>
          );
        })}
      </div>
      {/* The structured equivalent of the map, for a screen reader (spec §7). */}
      <table className="ag-sr" tabIndex={0} aria-label="Teams" data-testid="ag-zones-table">
        <thead><tr><th>Team</th><th>Agent</th><th>State</th><th>Now</th></tr></thead>
        <tbody>
          {teams.flatMap((t) => t.people.map((m) => (
            <tr key={`${t.key}:${m.roleKey}`}>
              <td>{TEAM_LABELS[t.key]?.label ?? t.label}</td><td data-user-text>{m.name}</td>
              <td>{STATE_WORD[m.state]}</td><td data-user-text>{m.line}</td>
            </tr>
          )))}
        </tbody>
      </table>
    </div>
  );
}
