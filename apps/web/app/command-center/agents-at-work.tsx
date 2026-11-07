"use client";

/**
 * AGENTS AT WORK on Command Center (design §11.3, §11.5): what agents are doing on their own,
 * as three small diagrams — Needs you · Running · Resolved today — each the newest item's flow
 * strip, each a link into Agents at work. Hidden until this install has ambient work at all.
 *
 * NEEDS YOU STAYS ONE QUEUE: an ambient item waiting on a person is a row in the existing Needs
 * you card (`AmbientNeedRows`), with its strip and an Open button; a row the decisions store
 * already listed (same decision id) is not listed twice.
 */
import { WhyLine } from "../../components/stuck-work";
import React from "react";

import { useConsoleLive } from "../../lib/console-live";
import { usePolledRead } from "../../lib/log-stream";
import { flowHref, listPath, parseList, polled, roleOfParty, type ListItem, type ListView } from "../../lib/ambient";
import { AgentAvatar } from "../agent-avatar";
import { FlowThumb } from "../agent-ops/flow-thumb";
import "../agent-ops/agent-ops.css";

export const AAW_POLL_MS = 20_000;

export function useAmbientToday(projectId: string): ListView | null {
  const live = useConsoleLive();
  const r = usePolledRead({ key: listPath(projectId, "all", 50), read: polled(listPath(projectId, "all", 50), parseList),
                            pollMs: AAW_POLL_MS, paused: !live, enabled: !!projectId });
  return r.data;
}

const sameDay = (iso: string | null, now: Date) => !!iso && new Date(iso).toDateString() === now.toDateString();

export function todayBuckets(view: ListView | null, now = new Date()) {
  const items = view?.items ?? [];
  const needs = items.filter((i) => i.badges.outcome === "needs_you");
  const running = items.filter((i) => i.badges.outcome === "running");
  const resolved = items.filter((i) => i.badges.outcome === "resolved_by_agents" && sameDay(i.resolved_at ?? i.updated_at, now));
  return {
    needs: { n: view?.counts.needs_you ?? needs.length, top: needs[0] ?? null },
    running: { n: view?.counts.running ?? running.length, top: running[0] ?? null },
    resolved: { n: resolved.length, top: resolved[0] ?? null },
  };
}

function Cell({ k, n, label, top, href }: { k: string; n: number; label: string; top: ListItem | null; href: string }) {
  return (
    <a className="aa-cc-cell" data-k={k} href={href} data-testid={`aaw-${k}`} title={top ? `${label}: ${top.title}` : label}>
      <span className="aa-cc-n">{n}<small>{label}</small></span>
      {top ? <FlowThumb strip={top.strip} live={k !== "resolved"} size={18} /> : <span className="aa-sub">—</span>}
      {top?.why && k !== "resolved" ? <WhyLine why={top.why} testId={`aaw-${k}-why`} compact /> : null}
    </a>
  );
}

export function AgentsAtWorkCard({ view, orgId }: { view: ListView | null; orgId?: string | null }) {
  if (!view || !Object.values(view.counts).some(Boolean)) return null;
  const b = todayBuckets(view);
  const base = (tab: string | null, item?: string) => {
    const u = new URLSearchParams();
    if (orgId) u.set("org", orgId);
    u.set("area", "agent_ops");
    if (tab) u.set("aat", tab);
    if (item) u.set("item", item);
    return `/?${u.toString()}`;
  };
  return (
    <section className="cc-card cc-sec aa-cc" aria-labelledby="cc-aaw-h" data-testid="cc-agents-at-work">
      <header className="cc-card-h">
        <h2 id="cc-aaw-h">Agents at work</h2>
        <span className="cc-spacer" />
        <a className="cc-link" href={base(null)}>See all ↗</a>
      </header>
      <div className="aa-cc-row">
        <Cell k="needs_you" n={b.needs.n} label="need you" top={b.needs.top} href={base(null, b.needs.top?.id)} />
        <Cell k="running" n={b.running.n} label="running" top={b.running.top} href={base("running", b.running.top?.id)} />
        <Cell k="resolved" n={b.resolved.n} label="resolved today" top={b.resolved.top} href={base("resolved_by_agents", b.resolved.top?.id)} />
      </div>
    </section>
  );
}

/** Ambient items waiting on a person, as rows of the one Needs-you queue. */
export function AmbientNeedRows({ view, listedDecisionIds }: { view: ListView | null; listedDecisionIds: Set<string> }) {
  const rows = (view?.items ?? []).filter((i) => i.badges.outcome === "needs_you" && !(i.needs_you_decision_id && listedDecisionIds.has(i.needs_you_decision_id)));
  if (!rows.length) return null;
  return (
    <>
      {rows.map((i) => {
        const role = roleOfParty(i.lease_owner);
        return (
          <div className="cc-need" key={i.id} data-testid="cc-need" data-kind="ambient">
            <div className="cc-need-row">
              {role ? <span className="cc-need-face"><AgentAvatar agent={role} size={36} aiBadge={false} state="needs_you" /></span>
                : <span className="cc-need-icon" aria-hidden>!</span>}
              <div className="cc-need-text">
                <b data-user-text title={i.title}>{i.title}</b>
                {i.why ? <WhyLine why={i.why} testId={`cc-need-why-${i.id}`} compact />
                  : <span>{i.badges.problem_type} · agents want your answer</span>}
              </div>
              <a className="cc-btn sm primary" href={flowHref({ item: i.id })} data-testid="cc-need-ambient-open">Open</a>
            </div>
            <div className="aa-need-strip"><FlowThumb strip={i.strip} live size={16} /></div>
          </div>
        );
      })}
    </>
  );
}
