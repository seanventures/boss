"use client";

// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope -- see app/previews-ui.tsx.
import React from "react";

import type { OrgChart, OrgNode } from "../../lib/v2/workforce";

/**
 * Org Chart — who actually passes work to whom.
 *
 * OBSERVED, NOT DECLARED
 * ----------------------
 * The artifact draws a reporting line: a CEO, managers, reports. BOSS has no parent field on a
 * role, so any tree drawn here would be an organisation somebody imagined.
 *
 * This is built from counted handoffs instead. It is measured rather than asserted, it shows
 * how the work actually flows, and the page says which it is — a derived shape presented as a
 * declared structure is a lie that looks like a diagram.
 */

export function OrgScreen({ chart }: { chart: OrgChart }) {
  if (!chart.apiReachable) {
    return (
      <section className="v2-screen" data-testid="v2-org-unreachable">
        <h1 className="v2-screen-title">Org Chart</h1>
        <p className="v2-empty">
          Could not reach the control plane
          {chart.blockedReason ? ` (${chart.blockedReason})` : ""}. An empty chart would read as
          a company with nobody in it.
        </p>
      </section>
    );
  }

  const connected = chart.nodes.filter((n) => n.connected);
  const isolated = chart.nodes.filter((n) => !n.connected);

  return (
    <section className="v2-screen" data-testid="v2-org">
      <header className="v2-screen-head">
        <h1 className="v2-screen-title">Org Chart</h1>
        {/* The provenance, on the page and not only in a comment. */}
        <p className="v2-screen-sub" data-testid="v2-org-provenance">
          {chart.note || `Derived from ${chart.derived_from}.`}
        </p>
      </header>

      {chart.nodes.length === 0 ? (
        <p className="v2-empty" data-testid="v2-org-none">
          No agent roles are defined for this company yet.
        </p>
      ) : (
        <>
          <h2 className="v2-ws-h">Who work flows through</h2>
          {connected.length === 0 ? (
            <p className="v2-empty" data-testid="v2-org-no-flow">
              No work has passed between agents in the last {chart.window_hours} hours, so there
              is no observed structure to draw yet.
            </p>
          ) : (
            <ul className="v2-rows" data-testid="v2-org-connected">
              {connected.map((n) => (
                <OrgRow key={n.id} n={n} edges={chart.edges} />
              ))}
            </ul>
          )}

          {/* Isolated agents are invisible on a tree and worth seeing: each is either idle by
              design or forgotten, and both are decisions someone should make on purpose. */}
          {isolated.length > 0 ? (
            <>
              <h2 className="v2-ws-h">Working alone</h2>
              <p className="v2-org-isolated-note">
                No work passed to or from these in {chart.window_hours}h — idle by design, or
                forgotten.
              </p>
              <ul className="v2-rows" data-testid="v2-org-isolated">
                {isolated.map((n) => (
                  <li className="v2-row" key={n.id} data-testid={`v2-org-isolated-${n.id}`}>
                    <span className="v2-row-main">
                      <span className="v2-row-title">
                        {n.name}
                        <span className="v2-row-meta-dim"> · {n.role}</span>
                      </span>
                      <span className="v2-row-meta v2-mono">{n.task}</span>
                    </span>
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </>
      )}
    </section>
  );
}

function OrgRow({ n, edges }: { n: OrgNode; edges: OrgChart["edges"] }) {
  const receives = edges.filter((e) => e.target === n.id);
  const sends = edges.filter((e) => e.source === n.id);
  return (
    <li className="v2-row v2-org-row" data-testid={`v2-org-node-${n.id}`}>
      <span className="v2-status v2-row-status" data-status={n.status}>
        <span className="v2-dot" data-status={n.status} aria-hidden />
        {n.status}
      </span>

      <span className="v2-row-main">
        <span className="v2-row-title">
          {n.name}
          <span className="v2-row-meta-dim"> · {n.role}</span>
        </span>

        <span className="v2-row-meta v2-mono">
          <span className="v2-row-meta-dim">receives from</span>{" "}
          {receives.length ? receives.map((e) => `${e.source} (${e.times})`).join(", ") : "—"}
        </span>
        <span className="v2-row-meta v2-mono">
          <span className="v2-row-meta-dim">passes to</span>{" "}
          {sends.length ? sends.map((e) => `${e.target} (${e.times})`).join(", ") : "—"}
        </span>
      </span>
    </li>
  );
}
