"use client";

/**
 * The top-bar presence pill, and the crew view behind it (spec §3.3, §0.2 C).
 *
 * `3 working · 1 needs you` with up to four live faces; hidden when nothing is live, so an idle
 * install has no pill shouting zero. Pressed, it opens the CREW: who is on what right now, one
 * row each, needs-you first — and pressing an agent there opens a 1:1 talk with it, which is the
 * shortest path from "something is happening" to "tell me about it". `Roster ↗` goes to Agents.
 *
 * Needs you is said once to a screen reader (aria-live polite) and as a `(n)` prefix on the tab
 * title, so a background tab still tells the operator they are being waited on.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";

import { askAgent, identityOf, isLive, zoneOf } from "../../lib/agent-world";
import { useLivePresence, type LiveMap } from "../../lib/agents-live";
import { AgFace, AgFaceStack, AgState } from "./agent-face";
import { presenceLine } from "./roster-zones";

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

/** `3 working · 1 needs you`. Either half is left out when it is zero. */
export function pillWords(rows: CrewRow[]): string {
  const needs = rows.filter((r) => r.zone === "needs_you").length;
  const working = rows.length - needs;
  return [working ? `${working} working` : "", needs ? `${needs} needs you` : ""].filter(Boolean).join(" · ");
}

function openRoster(): void {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set("area", "workforce");
    url.searchParams.delete("view");
    url.searchParams.delete("role");
    window.history.pushState(null, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  } catch { /* non-browser */ }
}

const TITLE_PREFIX = /^\(\d+\) /;

export function PresencePill({ projectId, live: given }: { projectId: string; live?: LiveMap }) {
  const polled = useLivePresence(given ? "" : projectId);
  const live = given ?? polled;
  const rows = useMemo(() => crewOf(live), [live]);
  const needs = rows.filter((r) => r.zone === "needs_you").length;
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const [announced, setAnnounced] = useState("");
  const said = useRef<Set<string>>(new Set());

  // The tab title carries `(n)` while somebody is waiting on the operator.
  useEffect(() => {
    if (typeof document === "undefined") return;
    const base = document.title.replace(TITLE_PREFIX, "");
    document.title = needs ? `(${needs}) ${base}` : base;
  }, [needs]);
  useEffect(() => () => {
    if (typeof document !== "undefined") document.title = document.title.replace(TITLE_PREFIX, "");
  }, []);
  // Said once per new wait, not on every poll.
  useEffect(() => {
    const waiting = rows.filter((r) => r.zone === "needs_you");
    const fresh = waiting.find((r) => !said.current.has(r.roleKey));
    said.current = new Set(waiting.map((r) => r.roleKey));
    if (fresh) setAnnounced(`${fresh.name} needs you`);
  }, [rows]);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    const onDown = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onDown);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("mousedown", onDown); };
  }, [open]);

  if (!rows.length) return null;
  const states = new Map(rows.map((r) => [r.roleKey, r.state]));
  return (
    <div className="ag-pill-wrap" ref={box}>
      <button type="button" className="ag-pill" data-testid="ag-presence-pill" data-needs={needs > 0 || undefined}
              title="Agents working now" aria-expanded={open} aria-haspopup="dialog"
              onClick={() => setOpen((v) => !v)}>
        <AgFaceStack roleKeys={rows.map((r) => r.roleKey)} size={20} max={4} states={states} decorative />
        <span>{pillWords(rows)}</span>
      </button>
      <span className="ag-sr" aria-live="polite">{announced}</span>
      {open ? (
        <div className="ag-crew" role="dialog" aria-label="Crew" data-testid="ag-crew">
          <header className="ag-crew-head">
            <span>Crew</span>
            <button type="button" className="ag-link" onClick={() => { setOpen(false); openRoster(); }}
                    data-testid="ag-crew-roster">Roster ↗</button>
          </header>
          <ul className="ag-crew-list">
            {rows.map((r) => (
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
        </div>
      ) : null}
    </div>
  );
}
