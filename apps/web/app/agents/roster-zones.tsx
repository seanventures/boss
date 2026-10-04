"use client";

/**
 * Agents › Roster, grouped by what each agent is doing (spec §3.2, the Floor inside the roster).
 *
 * Four zones in a fixed order — NEEDS YOU first, always — and an empty zone is not drawn. The
 * zone comes from presence (`/v1/agents/live`, derived from open acts), never from a model, and
 * falls back to the workforce feed's status for an API from before acts, so a card is in the
 * same zone the pill and the crew view say. When an agent's state changes its card GLIDES to
 * its new zone (FLIP, 240ms) — the moment that makes a roster read as a floor of people at work
 * — and jumps there instantly under reduced motion. Idle faces only breathe and blink, slowly.
 */
import React, { useLayoutEffect, useMemo, useRef, useState } from "react";
import { ChatCircleDots, CheckCircle, ShieldCheck } from "@phosphor-icons/react";

import type { V2Agent } from "../../lib/v2/workforce";
import type { AvatarState } from "../../lib/agent-world-types";
import { ZONES, identityOf, zoneOf, type Zone } from "../../lib/agent-world";
import type { LiveMap, LivePresence } from "../../lib/agents-live";
import { AgFace, AgFaceStack, AgState } from "./agent-face";

/** The feed's status, as presence, for when the live endpoint has nothing to say. */
export function presenceFromStatus(a: V2Agent): LivePresence {
  const last = a.last_touched
    || (a.task === "No work in this window." ? "No recent work" : (a.task || "").replace(/^last: /, "Last: "));
  if (a.status === "AT GATE") return { state: "needs_you", line: a.task || "Waiting on you" };
  if (a.status === "STUCK") return { state: "stuck", line: a.task || "Stuck" };
  if (a.status === "WORKING") return { state: "working", line: a.task || "" };
  return { state: "idle", line: last };
}

/** The one line under a card. Built from data, prefixed by what it means. */
export function presenceLine(p: LivePresence): string {
  const line = (p.line || "").trim();
  if (/^(Now|Waiting on you|Last|In panel|Stuck)\b/i.test(line)) return line;
  if (p.state === "needs_you") return line ? `Waiting on you: ${line}` : "Waiting on you";
  if (p.state === "stuck") return line ? `Stuck: ${line}` : "Stuck";
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
 *  `Show n more` — live agents (needs you, working, in a panel) are never folded away. */
export const ROSTER_FOLD_AT = 30;

/** The face on a roster card. Big enough to read the character and its motion at a glance. */
export const ROSTER_FACE_PX = 64;

export function RosterZones({ agents, live, onOpen, onAsk, onRegister }: {
  agents: V2Agent[];
  live: LiveMap;
  onOpen: (roleKey: string) => void;
  onAsk: (roleKey: string, name: string) => void;
  onRegister?: (name: string) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [unfolded, setUnfolded] = useState(false);
  const placed = useMemo(() => placeAgents(agents, live), [agents, live]);
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
                      onClick={() => setUnfolded(true)} title="Show every idle agent">
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
 * another control lands there too. Talk and Register sit in the card's own footer row — never
 * over the text — and are always visible, so a keyboard and a touch screen get them as well.
 *
 * The callsign is never cut: it wraps if it must. The role and the tagline are the parts that
 * give way, each with its full text as a tooltip.
 */
function RosterCard({ placed, onOpen, onAsk, onRegister }: {
  placed: Placed; onOpen: (k: string) => void; onAsk: (k: string, n: string) => void;
  onRegister?: (name: string) => void;
}) {
  const { agent: a, presence, zone } = placed;
  const who = identityOf(a.id, a.name);
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

/** `n agents · k working`, with the live faces beside it. */
export function RosterSummary({ agents, live }: { agents: V2Agent[]; live: LiveMap }) {
  const placed = placeAgents(agents, live);
  const working = placed.filter((p) => p.zone === "working" || p.zone === "in_panel");
  const needs = placed.filter((p) => p.zone === "needs_you");
  const liveKeys = [...needs, ...working].map((p) => p.agent.id);
  return (
    <span className="ag-roster-sum" data-testid="ag-roster-sum">
      {liveKeys.length ? <AgFaceStack roleKeys={liveKeys} size={20} max={5} /> : null}
      <span>{agents.length} agent{agents.length === 1 ? "" : "s"} · {working.length} working</span>
    </span>
  );
}
