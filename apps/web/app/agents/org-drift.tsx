"use client";

/**
 * Lines › Plan vs work (org-view spec §4.5): the plan against 30 days of handoffs, whatever the
 * window chip says. Two lists — lines nobody works along, and pairs that work together with no
 * line — and one action, `Plan it`, which places the pair through Lines' own save (so a refusal
 * shows the API's sentence where Lines always shows it).
 *
 * WHO REPORTS TO WHOM is proposed, never imposed (operator decision 2026-10-04): the team's
 * manager in the plan by default, else whoever sends the other more work — and the select
 * changes it before anything is saved.
 */
import React, { useState } from "react";

import type { OrgDrift, OrgUnplannedPair } from "../../lib/agents-org-board";
import { nameOf } from "./org-shared";

export function PlanVsWork({ drift, busy, onPlan, declared = {} }: {
  drift: OrgDrift; busy: boolean; onPlan: (child: string, parent: string) => void;
  /** The plan as it stands: `Plan it` says which line it replaces before it saves (walk D14). */
  declared?: Record<string, { reports_to?: string | null } | undefined>;
}) {
  if (!drift.plannedUnused.length && !drift.unplannedPairs.length) return null;
  return (
    <section className="org-drift" data-testid="org-drift" aria-labelledby="org-drift-title">
      <header className="org-drift-head">
        <span id="org-drift-title" className="org-drift-title" title="Compared over 30 days of handoffs">Plan vs work</span>
      </header>
      {drift.plannedUnused.length ? (
        <div className="org-drift-group" data-testid="org-drift-unused">
          <span className="org-drift-label">Planned, not used: {drift.plannedUnused.length}</span>
          <ul className="org-drift-list">
            {drift.plannedUnused.map((p) => (
              <li key={`${p.agent}>${p.reportsTo}`}>
                <span data-user-text>{nameOf(p.agent)}</span> reports to <span data-user-text>{nameOf(p.reportsTo)}</span>
                <span className="org-drift-sub">no handoffs in 30d</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {drift.unplannedPairs.length ? (
        <div className="org-drift-group" data-testid="org-drift-unplanned">
          <span className="org-drift-label">Works together, not planned: {drift.unplannedPairs.length}</span>
          <ul className="org-drift-list">
            {drift.unplannedPairs.map((p) => <PairRow key={`${p.a}|${p.b}`} pair={p} busy={busy} onPlan={onPlan}
                                                       declared={declared} />)}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

function PairRow({ pair, busy, onPlan, declared }: { pair: OrgUnplannedPair; busy: boolean;
                                                      onPlan: (child: string, parent: string) => void;
                                                      declared: Record<string, { reports_to?: string | null } | undefined> }) {
  const [parent, setParent] = useState(pair.parent);
  const child = parent === pair.a ? pair.b : pair.a;
  const current = declared[child]?.reports_to;
  return (
    <li data-testid={`org-drift-pair-${pair.a}-${pair.b}`}>
      <span data-user-text>{nameOf(pair.a)}</span> ↔ <span data-user-text>{nameOf(pair.b)}</span>
      <span>×{pair.handoffs}</span>
      <span className="org-drift-sub">not on the plan</span>
      <label className="org-drift-pick" title={pair.parentIs === "manager" && parent === pair.parent
        ? "The team's manager in the plan" : "Who the other reports to"}>
        <span className="ag-sr">Reports to</span>
        <select value={parent} onChange={(e) => setParent(e.target.value)} data-testid={`org-drift-parent-${pair.a}-${pair.b}`}>
          {[pair.a, pair.b].map((k) => <option key={k} value={k}>{nameOf(k)} leads</option>)}
        </select>
      </label>
      {current && current !== parent ? (
        <span className="org-drift-replaces" data-testid={`org-drift-replaces-${pair.a}-${pair.b}`}
              title="Saving moves this line in the plan">
          Replaces <span data-user-text>{nameOf(child)}</span> → <span data-user-text>{nameOf(current)}</span>
        </span>
      ) : null}
      <button type="button" className="org-node-act" disabled={busy} data-testid={`org-drift-plan-${pair.a}-${pair.b}`}
              title={`${nameOf(child)} reports to ${nameOf(parent)}`} onClick={() => onPlan(child, parent)}>
        Plan it
      </button>
    </li>
  );
}
