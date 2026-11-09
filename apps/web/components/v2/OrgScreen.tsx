"use client";

// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope, see app/previews-ui.tsx.
import React, { useState } from "react";

import type { OrgChart, OrgNode } from "../../lib/v2/workforce";
import { AgentChat } from "./AgentChat";

/**
 * Org Chart, a tree of who actually hands work to whom.
 *
 * OBSERVED, NOT DECLARED, AND THE TREE IS STILL REAL
 * ---------------------------------------------------
 * BOSS has no parent field on a role, so a hierarchy drawn from imagination would be an
 * organisation that does not exist. An earlier version therefore refused to draw a tree at all
 * and listed "receives from / passes to" per agent, which was honest and unreadable.
 *
 * The handoff graph IS directed, so a tree is derivable rather than invented: a root is an
 * agent nobody hands to, children are who it hands to, depth is distance from the root. That is
 * a real relationship, measured, and arguably the more useful one, it shows how work flows
 * rather than who someone once said reports to whom. The page says which it is.
 *
 * Each agent appears ONCE, at its shallowest depth. Work fans out and rejoins, so an agent can
 * be reached by several paths; drawing it several times would inflate the org.
 */

type Level = { depth: number; nodes: OrgNode[] };

function levels(tree: OrgNode[]): Level[] {
  const byDepth = new Map<number, OrgNode[]>();
  for (const n of tree) {
    const list = byDepth.get(n.depth ?? 0) ?? [];
    list.push(n);
    byDepth.set(n.depth ?? 0, list);
  }
  return [...byDepth.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([depth, nodes]) => ({ depth, nodes }));
}

export function OrgScreen({
  chart,
  projectId,
  actor = "executive_console",
}: {
  chart: OrgChart;
  projectId?: string;
  actor?: string;
}) {
  const [chatWith, setChatWith] = useState<OrgNode | null>(null);

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

  const tree = chart.tree ?? [];
  const rows = levels(tree);
  const isolated = chart.nodes.filter((n) => !n.connected);

  return (
    <section className="v2-screen" data-testid="v2-org">
      <header className="v2-screen-head">
        <h1 className="v2-screen-title">Org Chart</h1>
        <p className="v2-screen-sub">
          {chart.nodes.length} agents, click any node to chat
        </p>
        {/* The provenance, on the page and not only in a comment. A derived shape presented as
            a declared structure is a lie that looks like a diagram. */}
        <p className="v2-org-provenance" data-testid="v2-org-provenance">
          {chart.note || `Derived from ${chart.derived_from}.`}
          {chart.root_inferred ? (
            <span data-testid="v2-org-root-inferred">
              {" "}
              Every agent here is handed work by someone, so the busiest was taken as the top.
            </span>
          ) : null}
        </p>
      </header>

      {tree.length === 0 ? (
        <p className="v2-empty" data-testid="v2-org-no-flow">
          No work has passed between agents in the last {chart.window_hours} hours, so there is
          no observed structure to draw yet.
        </p>
      ) : (
        <div className="v2-tree" data-testid="v2-org-tree">
          {rows.map((row) => (
            <div
              className="v2-tree-level"
              key={row.depth}
              data-depth={row.depth}
              data-testid={`v2-org-level-${row.depth}`}
            >
              {row.nodes.map((n) => (
                <OrgCard
                  key={n.id}
                  n={n}
                  root={row.depth === 0}
                  onChat={() => setChatWith(n)}
                />
              ))}
            </div>
          ))}
        </div>
      )}

      {/* Isolated agents are invisible on a tree, so they get their own section: each is either
          idle by design or forgotten, and both are decisions someone should make on purpose. */}
      {isolated.length > 0 ? (
        <>
          <h2 className="v2-ws-h">Working alone</h2>
          <p className="v2-org-isolated-note">
            No work passed to or from these in {chart.window_hours}h, idle by design, or
            forgotten.
          </p>
          <ul className="v2-rows" data-testid="v2-org-isolated">
            {isolated.map((n) => (
              <li className="v2-row" key={n.id} data-testid={`v2-org-isolated-${n.id}`}>
                <span className="v2-row-main">
                  <span className="v2-row-title">
                    {n.name}
                    <span className="v2-row-meta-dim">, {n.role}</span>
                  </span>
                  <span className="v2-row-meta">{n.task}</span>
                </span>
              </li>
            ))}
          </ul>
        </>
      ) : null}

      {chatWith && projectId ? (
        <AgentChat
          agent={chatWith}
          projectId={projectId}
          actor={actor}
          onClose={() => setChatWith(null)}
        />
      ) : null}
    </section>
  );
}

function OrgCard({ n, root, onChat }: { n: OrgNode; root: boolean; onChat: () => void }) {
  return (
    <button
      type="button"
      className="v2-org-card"
      data-root={root}
      data-status={n.status}
      onClick={onChat}
      aria-label={`Chat with ${n.name}`}
      data-testid={`v2-org-node-${n.id}`}
    >
      <span className="v2-org-card-head">
        <span className="v2-dot" data-status={n.status} aria-hidden />
        <span className="v2-org-card-name">{n.name}</span>
      </span>

      <span className="v2-org-card-role v2-mono">
        {n.role}
        {/* The model it ACTUALLY used. Absent when nothing ran, a configured model that never
            executed is a claim, not a fact. */}
        {n.model ? <span className="v2-row-meta-dim">, {n.model}</span> : null}
      </span>

      {/* What it is doing, so a click lands on a conversation about real work. Only below the
          root, as the reference has it, the top of the tree is an anchor, not a status. */}
      {!root && n.task ? <span className="v2-org-card-task">{n.task}</span> : null}

      {!n.registered ? (
        <span className="v2-ungoverned-tag v2-mono" data-testid={`v2-org-ungoverned-${n.id}`}>
          no registered role
        </span>
      ) : null}
    </button>
  );
}
