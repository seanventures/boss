"use client";

/**
 * Teams as soft zones (agents-home spec §4.6): Live › Agent force. Organization › Teams
 * (org-teams.tsx) draws the same teams, in the same order and hues, as scorecards, and reuses
 * the pieces exported here (Badges, Face, nextInGrid, teamHue), so the two teach one spatial
 * memory (org-view spec §4.2).
 *
 * TEAMS COME FROM ONE PLACE: the board's `teams` (agent_identity.team_of on the server). Faces
 * are in their REAL state from presence; idle faces are still. A team is a soft tint of its hue
 * from the avatar kit's palette, never a new colour.
 *
 * Organization's old full-density map (Zones) and its drill-down list are retired: Teams and
 * Flow keep every behaviour they had (org-view spec §5).
 */
import React, { useEffect, useMemo, useState } from "react";
import { Bell, ChatCircleDots, Gear } from "@phosphor-icons/react";

import type { AvatarState } from "../../lib/agent-world-types";
import {
  TEAM_LABELS, TEAM_ORDER, type BoardAgent, type LiveBoard, type TeamSummary,
} from "../../lib/agents-live-board";
import { askAgent, identityOf, isLive, openAgent } from "../../lib/agent-world";
import { PALETTE_BY_ID } from "../../lib/avatar-kit/palettes";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { AgFace, AgFaceStack } from "./agent-face";

export type TeamZonesProps = {
  projectId: string;
  board: LiveBoard;
  density: "compact";
  /** The header's `Find an agent` text: non-matching faces dim to 30%. */
  search?: string;
  /** A team focused from Live's `Open map ↗` (`?focus=<team>`). */
  focusTeam?: string | null;
  /** Compact only: `Open map ↗`, optionally with a team to focus. */
  onOpenMap?: (teamKey?: string) => void;
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

export function matches(search: string, m: Pick<ZoneMember, "roleKey" | "name">): boolean {
  const q = search.trim().toLowerCase();
  if (!q) return true;
  const who = identityOf(m.roleKey);
  return `${m.name} ${who.name} ${m.roleKey}`.toLowerCase().includes(q);
}

export function reducedMotion(): boolean {
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

/** `2 working`, `1 needs you`: words, never colour alone. */
export function Badges({ team }: { team: Pick<TeamSummary, "working" | "needsYou"> }) {
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
export function Face({ m, size, still, dim }: { m: ZoneMember; size: 24 | 32 | 48; still: boolean; dim?: string }) {
  return (
    <span className="ag-team-face" data-still={still || undefined} data-dim={dim} data-state={m.state}>
      <AgFace roleKey={m.roleKey} size={size} state={m.state} decorative aiBadge={false} />
    </span>
  );
}

// ── Component ───────────────────────────────────────────────────────────────────────────────

export function TeamZones(props: TeamZonesProps): React.ReactElement | null {
  const v = useIdentityVersion(); // names and faces are resolved at render: repaint when identities load
  const teams = useMemo(() => zoneTeams(props.board), [props.board, v]); // eslint-disable-line react-hooks/exhaustive-deps -- `v` versions the store zoneTeams reads
  return <CompactZones {...props} teams={teams} />;
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
