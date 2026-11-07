"use client";

/**
 * The top-bar presence pill, and the crew view behind it — the Human Checkpoint Dock
 * (agent-world spec §3.3, agents-home spec §4.10).
 *
 * `4 working · 2 need you` with up to four faces; hidden only when nothing is working and
 * nothing needs you. Pressed, it opens the CREW: what needs you first, one row per item with
 * `Review`, then who is working on what — pressing an agent there opens a 1:1 talk.
 *
 * NEEDS YOU COUNTS DECISIONS, NOT AGENTS. It is `attention.count` from /v1/agents/live: the
 * same number as the Command Center, Approvals and Agents › Live (command_center's needs_you),
 * so three gates on one agent say 3, and a pull request nobody is attributed to is counted.
 * An API from before that sends no `attention`, and only then does the pill count agents.
 *
 * Each new item is said once to a screen reader (aria-live polite), by item id — a second gate
 * on the same agent is still announced — and the count prefixes the tab title as `(n)`.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";

import { askAgent, identityOf, isLive, zoneOf } from "../../lib/agent-world";
import { useAttention, useLivePresence, type LiveMap } from "../../lib/agents-live";
import type { AttentionSummary } from "../../lib/agents-live-board";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { AgFace, AgFaceStack, AgState } from "./agent-face";
import { RunFace } from "./run-face";
import { presenceLine } from "./roster-zones";
import { ago, decisionPatch, localTime, navigate, useNow } from "./live-util";
import { useConsoleLive } from "../../lib/console-live";
import { Bell } from "@phosphor-icons/react";

export type CrewRow = { roleKey: string; name: string; zone: ReturnType<typeof zoneOf>; line: string;
                        state: import("../../lib/agent-world-types").AvatarState; since?: string };

/** Live agents, needs-you first, then working, then in a panel; by name inside each. */
export function crewOf(live: LiveMap): CrewRow[] {
  const order = { needs_you: 0, working: 1, in_panel: 2, idle: 3 } as const;
  const rows: CrewRow[] = [];
  for (const [roleKey, p] of live) {
    if (!isLive(p)) continue;
    const who = identityOf(roleKey);
    rows.push({ roleKey, name: who.callsign || who.name, zone: zoneOf(p, p.inPanel), line: presenceLine(p),
                state: p.inPanel ? "speaking" : p.state, since: p.since });
  }
  return rows.sort((a, b) => order[a.zone] - order[b.zone] || a.name.localeCompare(b.name));
}

/** `4 working · 2 need you` / `1 needs you`. Either half is left out when it is zero. */
export function pillText(working: number, needs: number): string {
  return [working ? `${working} working` : "",
          needs ? (needs === 1 ? "1 needs you" : `${needs} need you`) : ""].filter(Boolean).join(" · ");
}

/** The older words, from agents alone: for an API that sends no `attention`. */
export function pillWords(rows: CrewRow[]): string {
  const needs = rows.filter((r) => r.zone === "needs_you").length;
  return pillText(rows.filter((r) => r.zone === "working" || r.zone === "in_panel").length, needs);
}

function openAgents(patch: Record<string, string | null> = {}): void {
  navigate({ area: "workforce", view: null, role: null, run: null, focus: null, ...patch });
}

/** Where `Review` goes: Live, on the card, when it can be decided there (or is a run's step or
 *  setup, which only Live draws); otherwise the decision in Approvals. */
export function reviewPatch(item: AttentionSummary["items"][number]): Record<string, string | null> {
  // What only a person can set up (a prod host, a value) opens where it is set (X-14).
  if (item.kind === "setup" && item.href) {
    const patch: Record<string, string | null> = { view: null, role: null, run: null, focus: null };
    new URLSearchParams(item.href.replace(/^[^?]*\?/, "")).forEach((v, k) => { patch[k] = v; });
    return patch;
  }
  if (item.decideInline || item.id.startsWith("run:") || item.id.startsWith("ready:")) {
    return { area: "workforce", view: null, role: null, run: null, focus: item.id };
  }
  return decisionPatch(item.id);
}

/** The bell's words for a screen reader and its tooltip: what needs you, and who is working. */
export function bellLabel(working: number, needs: number): string {
  return pillText(working, needs) || "Nothing needs you";
}

/**
 * `variant="bell"` is the top bar's NOTIFICATIONS BELL (BOSSAPP-914 screen 1): the same counts and
 * the same crew behind it, drawn as a bell with the needs-you count on it (a teal dot while agents
 * work), and always present — an empty bell is an answer, a missing pill is not.
 */
export function PresencePill({ projectId, live: given, attention: givenAttention, variant = "pill" }: {
  projectId: string; live?: LiveMap; attention?: AttentionSummary | null; variant?: "pill" | "bell";
}) {
  const polled = useLivePresence(given ? "" : projectId);
  const polledAttention = useAttention(given ? "" : projectId);
  const live = given ?? polled;
  // PAUSED MEANS STILL: the console's Live/Paused chip stops the pill's halos too (motion rule).
  const consoleLive = useConsoleLive();
  const attention = given ? (givenAttention ?? null) : polledAttention;
  const v = useIdentityVersion(); // the crew is named at render: repaint when identities load
  const now = useNow(30_000);
  const rows = useMemo(() => crewOf(live), [live, v]); // eslint-disable-line react-hooks/exhaustive-deps -- `v` versions the store crewOf reads
  // WORKING MEANS WORKING. An agent can be live without working (queued, waiting): crewOf
  // files it under idle, and counting "everything not needs-you" said "1 working" with nothing
  // running. Only the working and in-panel zones are work.
  const atWork = (r: CrewRow) => r.zone === "working" || r.zone === "in_panel";
  const working = rows.filter(atWork);
  const needs = attention ? attention.count : rows.filter((r) => r.zone === "needs_you").length;
  const workingCount = working.length;
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const [announced, setAnnounced] = useState("");
  const said = useRef<Set<string>>(new Set());

  // The tab title's `(n)` is the shell's (lib/tab-title.ts): one owner, re-applied after Next
  // rewrites the title on a navigation or refresh.
  // Said once per new item (by id), not on every poll.
  useEffect(() => {
    if (attention) {
      const fresh = attention.items.find((i) => !said.current.has(i.id));
      said.current = new Set([...said.current].filter((id) => attention.items.some((i) => i.id === id)));
      for (const i of attention.items) said.current.add(i.id);
      if (fresh) setAnnounced(`Needs you: ${fresh.title}`);
      return;
    }
    const waiting = rows.filter((r) => r.zone === "needs_you");
    const fresh = waiting.find((r) => !said.current.has(r.roleKey));
    said.current = new Set(waiting.map((r) => r.roleKey));
    if (fresh) setAnnounced(`${fresh.name} needs you`);
  }, [rows, attention]);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    const onDown = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onDown);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("mousedown", onDown); };
  }, [open]);

  const bell = variant === "bell";
  if (!bell && !workingCount && !needs) return <span className="ag-sr" aria-live="polite">{announced}</span>;
  const faceKeys = [...new Set([...(attention?.items ?? []).map((i) => i.roleKey).filter((k): k is string => !!k),
                                ...rows.map((r) => r.roleKey)])];
  const states = new Map(rows.map((r) => [r.roleKey, r.state]));
  for (const i of attention?.items ?? []) if (i.roleKey) states.set(i.roleKey, "needs_you");
  const crewRows = attention ? working : rows;
  return (
    <div className="ag-pill-wrap" ref={box} data-paused={consoleLive ? undefined : "true"}>
      {bell ? (
        <button type="button" className="bx-bell" data-testid="topbar-bell" data-needs={needs > 0 || undefined}
                data-working={workingCount > 0 || undefined}
                title="Approvals waiting on you" aria-label={`Approvals waiting on you: ${bellLabel(workingCount, needs)}`}
                aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen((x) => !x)}>
          <Bell size={17} weight={needs ? "fill" : "regular"} aria-hidden />
          {needs ? <b className="bx-bell-n" data-testid="topbar-bell-count">{needs > 99 ? "99+" : needs}</b> : null}
          {workingCount ? <i className="bx-bell-working" aria-hidden /> : null}
        </button>
      ) : (
      <button type="button" className="ag-pill" data-testid="ag-presence-pill" data-needs={needs > 0 || undefined}
              // THE COUNT, ALWAYS (re-check R3): where the words do not fit, CSS draws this.
              data-n={needs || workingCount}
              title="Agents working now" aria-expanded={open} aria-haspopup="dialog"
              onClick={() => setOpen((x) => !x)}>
        {faceKeys.length ? <AgFaceStack roleKeys={faceKeys} size={20} max={4} states={states} decorative /> : null}
        <span>{attention ? pillText(workingCount, needs) : pillWords(rows)}</span>
      </button>
      )}
      <span className="ag-sr" aria-live="polite">{announced}</span>
      {open ? (
        <div className="ag-crew" role="dialog" aria-label="Crew" data-testid="ag-crew">
          <header className="ag-crew-head">
            <span>{bell ? (pillText(workingCount, needs) || "Crew") : "Crew"}</span>
            <button type="button" className="ag-link" onClick={() => { setOpen(false); openAgents(); }}
                    data-testid="ag-crew-roster">Agents ↗</button>
          </header>
          {attention && attention.items.length ? (
            <>
              <span className="ag-crew-sub" data-testid="ag-crew-needs-head">Needs you</span>
              <ul className="ag-crew-list" data-testid="ag-crew-needs">
                {attention.items.slice(0, 5).map((i) => (
                  <li key={i.id} className="ag-crew-need" data-testid={`ag-crew-need-${i.id}`}>
                    {i.roleKey
                      ? <AgFace roleKey={i.roleKey} size={32} state="needs_you" decorative />
                      : <RunFace roleKey={null} variant="boss" size={32} state="needs_you" />}
                    <span className="ag-crew-text">
                      <span className="ag-crew-line ag-trunc" title={i.title} data-user-text>{i.title}</span>
                      {ago(i.at, now) ? <span className="ag-crew-age" title={`Waiting since ${localTime(i.at)}`}>{ago(i.at, now)}</span> : null}
                    </span>
                    <button type="button" className="ag-btn" data-testid={`ag-crew-review-${i.id}`}
                            onClick={() => { setOpen(false); navigate(reviewPatch(i)); }}>Review</button>
                  </li>
                ))}
              </ul>
              {attention.count > Math.min(5, attention.items.length) ? (
                <button type="button" className="ag-link ag-crew-more" data-testid="ag-crew-needs-more"
                        title="See them all on Live"
                        onClick={() => { setOpen(false); navigate({ area: "workforce", view: null, role: null, run: null, focus: null }); }}>
                  +{attention.count - Math.min(5, attention.items.length)} more
                </button>
              ) : null}
            </>
          ) : null}
          {bell && !needs && !crewRows.length ? (
            <p className="ag-crew-empty" data-testid="topbar-bell-empty">Nothing needs you, and no agent is working right now.</p>
          ) : null}
          {crewRows.length ? (
            <>
              {attention ? <span className="ag-crew-sub">Working</span> : null}
              <ul className="ag-crew-list">
                {crewRows.map((r) => (
                  <li key={r.roleKey}>
                    <button type="button" className="ag-crew-row" data-zone={r.zone}
                            data-testid={`ag-crew-${r.roleKey}`} title={`Talk to ${r.name}`}
                            onClick={() => { setOpen(false); askAgent(r.roleKey, identityOf(r.roleKey).name, { from: "presence" }); }}>
                      <AgFace roleKey={r.roleKey} size={32} state={r.state} decorative />
                      <span className="ag-crew-text">
                        <span className="ag-crew-name">{r.name}</span>
                        <span className="ag-crew-line" data-user-text>{r.line}</span>
                      </span>
                      <AgState state={r.state} compact />
                    </button>
                  </li>
                ))}
              </ul>
            </>
          ) : null}
          {bell ? (
            <button type="button" className="ag-link ag-crew-more" data-testid="topbar-bell-approvals"
                    title="Every approval waiting, across this project"
                    onClick={() => { setOpen(false); navigate({ area: "decisions", view: null, role: null, run: null, focus: null }); }}>
              Open Approvals ›
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
