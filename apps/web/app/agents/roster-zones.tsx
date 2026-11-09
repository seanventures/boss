"use client";

/**
 * Agents › Roster, grouped by what each agent is doing (spec §3.2, the Floor inside the roster).
 *
 * Four zones in a fixed order, NEEDS YOU first, always, and an empty zone is not drawn. The
 * zone comes from presence (`/v1/agents/live`, derived from open acts), never from a model, and
 * falls back to the workforce feed's status for an API from before acts, so a card is in the
 * same zone the pill and the crew view say. When an agent's state changes its card GLIDES to
 * its new zone (FLIP, 240ms), the moment that makes a roster read as a floor of people at work
 *, and jumps there instantly under reduced motion. Idle faces only breathe and blink, slowly.
 */
import React, { useLayoutEffect, useMemo, useRef, useState } from "react";
import { ChatCircleDots, CheckCircle, ShieldCheck } from "@phosphor-icons/react";

import type { V2Agent } from "../../lib/v2/workforce";
import type { AvatarState } from "../../lib/agent-world-types";
import { ZONES, zoneOf, type Zone } from "../../lib/agent-world";
import { useIdentity, useIdentityVersion } from "../../lib/use-agent-identities";
import type { LiveMap, LivePresence } from "../../lib/agents-live";
import { AgFace, AgState } from "./agent-face";
import { TEAM_ORDER, teamOfAgent } from "../../lib/agents-live-board";

/** The feed's status, as presence, for when the live endpoint has nothing to say. */
/** The workforce wire's word for a failed step: action, an em dash, then "failed". */
const FAILED_TASK = /\s+\u2014\s+failed$/;

export function presenceFromStatus(a: V2Agent): LivePresence {
  const last = a.last_touched
    || (a.task === "No work in this window." ? "No recent work" : (a.task || "").replace(/^last: /, "Last: "));
  // A COUNT IS NOT WORK. An older API's idle line was "last: <stage>, N calls in 30d", a
  // volume, not what the agent did (agents-home spec §3.3). Never shown: the server's
  // `Last: <verb>, <label>, 2h` replaces it, and until then the card says it has none.
  if (a.status === "IDLE" && /\bcalls? in \d+d\b/i.test(last)) return { state: "idle", line: "No work yet" };
  if (a.status === "AT GATE") return { state: "needs_you", line: a.task || "Waiting on you" };
  // STUCK covers two things on this wire (console_v2_workforce._status_of): a step that FAILED
  // (action, an em dash, then "failed") and one running with no progress. Only the second is stuck.
  if (a.status === "STUCK" && FAILED_TASK.test(a.task || "")) {
    return { state: "failed", line: (a.task || "").replace(FAILED_TASK, "") || "Failed" };
  }
  if (a.status === "STUCK") return { state: "stuck", line: a.task || "Stuck" };
  if (a.status === "WORKING") return { state: "working", line: a.task || "" };
  return { state: "idle", line: last };
}

/** The one line under a card. Built from data, prefixed by what it means. */
export function presenceLine(p: LivePresence): string {
  const line = (p.line || "").trim();
  if (/^(Now|Waiting on you|Last|In panel|In a panel|Stuck|Failed|Stopped|No work yet)\b/i.test(line)) return line;
  if (p.state === "needs_you") return line ? `Waiting on you: ${line}` : "Waiting on you";
  if (p.state === "stuck") return line ? `Stuck: ${line}` : "Stuck";
  if (p.state === "failed") return line ? `Failed: ${line}` : "Failed";
  if (p.state === "stopped") return line ? `Stopped: ${line}` : "Stopped";
  if (p.inPanel) return line ? `In panel: ${line}` : "In a panel";
  if (p.state === "working" || p.state === "thinking" || p.state === "speaking") return line ? `Now: ${line}` : "Working";
  return line || "No recent work";
}

export type Placed = { agent: V2Agent; presence: LivePresence; zone: Zone };

/** Each agent with its presence and zone. Live presence wins over the feed's status. */
export function placeAgents(agents: V2Agent[], live: LiveMap): Placed[] {
  return agents.map((agent) => {
    const presence = live.get(agent.id) ?? presenceFromStatus(agent);
    return { agent, presence, zone: zoneOf(presence, presence.inPanel) };
  });
}

/** FLIP: a card that moved since the last render starts where it was and glides to where it is. */
function useGlide(container: React.RefObject<HTMLElement | null>, signature: string): void {
  const last = useRef<Map<string, DOMRect>>(new Map());
  useLayoutEffect(() => {
    const host = container.current;
    if (!host) return;
    const reduce = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    const next = new Map<string, DOMRect>();
    host.querySelectorAll<HTMLElement>("[data-glide]").forEach((el) => {
      const key = el.dataset.glide!;
      const now = el.getBoundingClientRect();
      next.set(key, now);
      const was = last.current.get(key);
      if (reduce || !was || typeof el.animate !== "function") return;
      const dx = was.left - now.left;
      const dy = was.top - now.top;
      if (Math.abs(dx) < 1 && Math.abs(dy) < 1) return;
      el.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "translate(0, 0)" }],
                 { duration: 240, easing: "cubic-bezier(.2,.7,.2,1)" });
    });
    last.current = next;
  }, [container, signature]);
}

/** The roster shows everyone. Only a very large roster folds its idle tail behind one quiet
 *  `Show n more`, live agents (needs you, working, in a panel) are never folded away.
 *  Presence grouping only: Directory groups by team and shows every agent. */
export const ROSTER_FOLD_AT = 30;

/** The face on a roster card. Big enough to read the character and its motion at a glance. */
export const ROSTER_FACE_PX = 64;

/** Directory's sections: each agent's team (agent_identity.team_of, via identities), in the
 *  shipped order, then any team an operator named. An empty team is not drawn. */
export function groupByTeam(placed: Placed[]): { key: string; label: string; tip?: string; members: Placed[] }[] {
  const groups = new Map<string, { key: string; label: string; tip?: string; members: Placed[] }>();
  for (const p of placed) {
    const team = p.agent.registered === false ? teamOfAgent("") : teamOfAgent(p.agent.id);
    const key = p.agent.registered === false ? "yours" : team.key;
    if (!groups.has(key)) groups.set(key, { ...(p.agent.registered === false ? { key: "yours", label: "Yours" } : team), members: [] });
    groups.get(key)!.members.push(p);
  }
  const rank = (k: string) => {
    const i = TEAM_ORDER.indexOf(k);
    return i < 0 ? TEAM_ORDER.length - 1 - 0.5 : i; // operator teams sit before Yours
  };
  return [...groups.values()].sort((a, b) => rank(a.key) - rank(b.key) || a.label.localeCompare(b.label));
}

export function RosterZones({ agents, live, onOpen, onAsk, onRegister, groupBy = "presence" }: {
  agents: V2Agent[];
  live: LiveMap;
  onOpen: (roleKey: string) => void;
  onAsk: (roleKey: string, name: string) => void;
  onRegister?: (name: string) => void;
  /** presence: NEEDS YOU / WORKING / IN A PANEL / IDLE. team: Directory's team sections. */
  groupBy?: "presence" | "team";
}) {
  const host = useRef<HTMLDivElement>(null);
  const [unfolded, setUnfolded] = useState(false);
  const placed = useMemo(() => placeAgents(agents, live), [agents, live]);
  if (groupBy === "team") {
    return <TeamSections placed={placed} onOpen={onOpen} onAsk={onAsk} onRegister={onRegister} />;
  }
  return <PresenceZones placed={placed} host={host} unfolded={unfolded} onUnfold={() => setUnfolded(true)}
                        onOpen={onOpen} onAsk={onAsk} onRegister={onRegister} />;
}

/** Directory: the same cards, in team sections. The glide still runs when a filter or a search
 *  changes the layout. */
function TeamSections({ placed, onOpen, onAsk, onRegister }: {
  placed: Placed[]; onOpen: (k: string) => void; onAsk: (k: string, n: string) => void;
  onRegister?: (name: string) => void;
}) {
  useIdentityVersion(); // each agent's team comes from the identity store
  const host = useRef<HTMLDivElement>(null);
  const groups = groupByTeam(placed);
  useGlide(host, groups.map((g) => `${g.key}:${g.members.map((p) => p.agent.id).join(",")}`).join("|"));
  return (
    <div className="ag-roster ag-dir" ref={host} data-testid="roster-grid" data-group="team">
      {groups.map((g) => (
        <section key={g.key} className="ag-zone ag-dir-team" data-team={g.key} data-testid={`ag-dir-team-${g.key}`}
                 aria-label={`${g.label} ${g.members.length}`}>
          <header className="ag-zone-head">
            <span className="ag-zone-label" title={g.tip}>{g.label}</span>
            <span className="ag-zone-count">{g.members.length}</span>
          </header>
          <div className="ag-zone-grid">
            {g.members.map((p) => (
              <RosterCard key={p.agent.id} placed={p} onOpen={onOpen} onAsk={onAsk} onRegister={onRegister} />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

function PresenceZones({ placed, host, unfolded, onUnfold, onOpen, onAsk, onRegister }: {
  placed: Placed[]; host: React.RefObject<HTMLDivElement | null>; unfolded: boolean; onUnfold: () => void;
  onOpen: (k: string) => void; onAsk: (k: string, n: string) => void; onRegister?: (name: string) => void;
}) {
  const byZone = useMemo(() => {
    const m = new Map<Zone, Placed[]>(ZONES.map((z) => [z.key, []]));
    for (const p of placed) m.get(p.zone)!.push(p);
    return m;
  }, [placed]);
  useGlide(host, placed.map((p) => `${p.agent.id}:${p.zone}`).join("|"));

  // Room left for idle cards before the fold; live zones always show in full.
  const liveCount = placed.length - (byZone.get("idle")?.length ?? 0);
  const idleRoom = unfolded || placed.length <= ROSTER_FOLD_AT ? Infinity : Math.max(0, ROSTER_FOLD_AT - liveCount);

  return (
    <div className="ag-roster" ref={host} data-testid="roster-grid">
      {ZONES.map(({ key, label }) => {
        const all = byZone.get(key) ?? [];
        if (!all.length) return null;
        const shown = key === "idle" ? all.slice(0, idleRoom) : all;
        const hidden = all.length - shown.length;
        return (
          <section key={key} className="ag-zone" data-zone={key} data-testid={`ag-zone-${key}`}
                   aria-label={`${label} ${all.length}`}>
            <header className="ag-zone-head">
              <span className="ag-zone-label">{label}</span>
              <span className="ag-zone-count">{all.length}</span>
            </header>
            {shown.length ? (
              <div className="ag-zone-grid">
                {shown.map((p) => (
                  <RosterCard key={p.agent.id} placed={p} onOpen={onOpen} onAsk={onAsk} onRegister={onRegister} />
                ))}
              </div>
            ) : null}
            {hidden > 0 ? (
              <button type="button" className="ag-zone-more" data-testid="ag-idle-more"
                      onClick={onUnfold} title="Show every idle agent">
                Show {hidden} more
              </button>
            ) : null}
          </section>
        );
      })}
    </div>
  );
}

/**
 * One agent. THE WHOLE CARD OPENS ITS PASSPORT: the name is the card's one link-like control
 * (a real button, so Tab and Enter reach it) and a press anywhere on the card that is not
 * another control lands there too. Talk and Register sit in the card's own footer row, never
 * over the text, and are always visible, so a keyboard and a touch screen get them as well.
 *
 * The callsign is never cut: it wraps if it must. The role and the tagline are the parts that
 * give way, each with its full text as a tooltip.
 */
function RosterCard({ placed, onOpen, onAsk, onRegister }: {
  placed: Placed; onOpen: (k: string) => void; onAsk: (k: string, n: string) => void;
  onRegister?: (name: string) => void;
}) {
  const { agent: a, presence, zone } = placed;
  const who = useIdentity(a.id, a.name); // a card drawn before identities load re-renders when they do
  const primary = who.callsign || a.name;
  const state: AvatarState = presence.inPanel ? "speaking" : presence.state;
  const runs = a.successful_runs ?? 0;
  const line = presenceLine(presence);
  const open = (e: React.MouseEvent) => {
    // Another control on the card (Talk, Register) does its own thing.
    const hit = (e.target as Element).closest("button, a, input, select, textarea");
    if (hit && !hit.classList.contains("ag-card-open")) return;
    onOpen(a.id);
  };
  return (
    <article className="ag-card" data-glide={a.id} data-zone={zone} data-status={a.status}
             data-testid={`wf-agent-${a.id}`} onClick={open}>
      <span className="ag-card-face" aria-hidden>
        <AgFace roleKey={a.id} identity={who} size={ROSTER_FACE_PX} state={state} decorative aiBadge={false} />
      </span>
      <div className="ag-card-head">
        <button type="button" className="ag-card-open" data-testid={`wf-open-${a.id}`}
                aria-label={`Open ${primary}${who.callsign ? `, ${a.name}` : ""}`}>
          <span className="ag-card-name" data-testid={`ag-name-${a.id}`} data-user-text>{primary}</span>
        </button>
        {who.callsign ? (
          <span className="ag-card-role ag-trunc" title={a.name} data-user-text>{a.name}</span>
        ) : null}
        {who.tagline ? (
          <span className="ag-card-tagline ag-trunc" title={who.tagline} data-user-text>{who.tagline}</span>
        ) : null}
      </div>
      <div className="ag-card-line" title={line} data-testid={`ag-line-${a.id}`}>
        {zone !== "idle" ? <AgState state={state} compact /> : null}
        <span className="ag-trunc" data-user-text>{line}</span>
      </div>
      <div className="ag-card-foot">
        {runs > 0 ? (
          <span className="ag-card-vitals" aria-label="Vitals">
            <span title="Trust score"><ShieldCheck size={13} aria-hidden /> {a.trust_score}</span>
            <span title="Successful runs"><CheckCircle size={13} aria-hidden /> {runs}</span>
          </span>
        ) : <span className="ag-card-vitals" />}
        <span className="ag-card-actions">
          {!a.registered && onRegister ? (
            <button type="button" className="ag-btn ag-btn-sm" onClick={() => onRegister(a.name)}
                    title="Make it an agent: skills, limits and a trust score"
                    data-testid={`wf-register-${a.id}`}>Register</button>
          ) : null}
          <button type="button" className="ag-icon-btn ag-card-talk" onClick={() => onAsk(a.id, a.name)}
                  data-testid={`wf-ask-${a.id}`} aria-label={`Talk to ${primary}`} title={`Talk to ${primary}`}>
            <ChatCircleDots size={18} weight="bold" aria-hidden />
          </button>
        </span>
      </div>
    </article>
  );
}

