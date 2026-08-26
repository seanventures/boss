"use client";

// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope -- see app/previews-ui.tsx.
import React from "react";

import type { V2Agent, V2Handoff, Workforce } from "../../lib/v2/workforce";

/**
 * Hive — the agents as a graph, and the work moving between them.
 *
 * WHY A CIRCLE AND NOT THE ARTIFACT'S FIXED LAYOUT
 * ------------------------------------------------
 * The artifact positions thirteen named agents by hand. BOSS has however many roles the project
 * defines, and a hardcoded layout would either drop the extras or leave holes. Positions are
 * computed from the agent count so the picture is right for any company.
 *
 * EDGES ARE MEASURED
 * ------------------
 * Every line is a handoff that actually happened, with a count. Nothing here is a declared
 * relationship — see console_v2_workforce.org_chart for why that distinction is load-bearing.
 */

const SIZE = 560;
const CENTRE = SIZE / 2;
const RADIUS = 210;

type Placed = V2Agent & { x: number; y: number };

function place(agents: V2Agent[]): Placed[] {
  return agents.map((a, i) => {
    // Start at the top and go clockwise; deterministic, so the graph does not reshuffle on
    // every render and make the operator re-find an agent they were looking at.
    const angle = (i / Math.max(agents.length, 1)) * Math.PI * 2 - Math.PI / 2;
    return {
      ...a,
      x: Math.round(CENTRE + RADIUS * Math.cos(angle)),
      y: Math.round(CENTRE + RADIUS * Math.sin(angle)),
    };
  });
}

export function HiveScreen({ workforce }: { workforce: Workforce }) {
  if (!workforce.apiReachable) {
    return (
      <section className="v2-screen" data-testid="v2-hive-unreachable">
        <h1 className="v2-screen-title">Hive</h1>
        <p className="v2-empty">
          Could not reach the control plane
          {workforce.blockedReason ? ` (${workforce.blockedReason})` : ""}. An empty graph would
          read as a company with no agents.
        </p>
      </section>
    );
  }

  const nodes = place(workforce.agents);
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const busiest = Math.max(1, ...workforce.handoffs.map((h) => h.times));

  return (
    <section className="v2-screen" data-testid="v2-hive">
      <header className="v2-screen-head">
        <h1 className="v2-screen-title">Hive</h1>
        <p className="v2-screen-sub">
          {workforce.agents.length} agents · {workforce.handoffs.length} handoff routes observed
          in {workforce.window_hours}h
        </p>
      </header>

      {workforce.agents.length === 0 ? (
        <p className="v2-empty" data-testid="v2-hive-none">
          No agent roles are defined for this company yet.
        </p>
      ) : (
        <>
          <svg
            className="v2-hive-svg"
            viewBox={`0 0 ${SIZE} ${SIZE}`}
            role="img"
            aria-label="Agents and the handoffs between them"
            data-testid="v2-hive-graph"
          >
            {/* Handoffs first, so nodes sit on top of their own lines. */}
            {workforce.handoffs.map((h) => {
              const from = byId.get(h.source);
              const to = byId.get(h.target);
              if (!from || !to) return null;
              return (
                <line
                  key={`${h.source}->${h.target}`}
                  x1={from.x}
                  y1={from.y}
                  x2={to.x}
                  y2={to.y}
                  className="v2-hive-edge"
                  // Thickness is the count, so the busy routes are visible at a glance rather
                  // than every line looking equally important.
                  strokeWidth={1 + (h.times / busiest) * 3}
                  data-testid={`v2-hive-edge-${h.source}-${h.target}`}
                >
                  <title>{`${h.source} → ${h.target}: ${h.times} handoff${h.times === 1 ? "" : "s"}${h.last_action ? ` (last: ${h.last_action})` : ""}`}</title>
                </line>
              );
            })}

            {nodes.map((n) => (
              <g key={n.id} data-testid={`v2-hive-node-${n.id}`} data-status={n.status}>
                <circle cx={n.x} cy={n.y} r={20} className="v2-hive-node" data-status={n.status}>
                  <title>{`${n.name} — ${n.status}: ${n.task}`}</title>
                </circle>
                <text x={n.x} y={n.y + 36} className="v2-hive-label" textAnchor="middle">
                  {n.name.length > 14 ? `${n.name.slice(0, 13)}…` : n.name}
                </text>
              </g>
            ))}
          </svg>

          <h2 className="v2-ws-h">Handoffs</h2>
          {workforce.handoffs.length === 0 ? (
            <p className="v2-empty" data-testid="v2-hive-no-handoffs">
              No work has passed between agents in the last {workforce.window_hours} hours. That
              is either a quiet window or a pipeline that is not running.
            </p>
          ) : (
            <ul className="v2-rows" data-testid="v2-hive-handoffs">
              {workforce.handoffs.slice(0, 20).map((h) => (
                <HandoffRow key={`${h.source}->${h.target}`} h={h} />
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}

function HandoffRow({ h }: { h: V2Handoff }) {
  return (
    <li className="v2-row" data-testid={`v2-handoff-${h.source}-${h.target}`}>
      <span className="v2-row-main">
        <span className="v2-row-title v2-mono">
          {h.source} <span className="v2-row-meta-dim">→</span> {h.target}
        </span>
        <span className="v2-row-meta v2-mono">
          <b>{h.times}</b> {h.times === 1 ? "handoff" : "handoffs"}
          {h.last_action ? (
            <>
              {"  "}
              <span className="v2-row-meta-dim">last</span> {h.last_action}
            </>
          ) : null}
          {h.last_at ? (
            <>
              {"  "}
              <span className="v2-row-meta-dim">{h.last_at.slice(0, 16).replace("T", " ")}</span>
            </>
          ) : null}
        </span>
      </span>
    </li>
  );
}
