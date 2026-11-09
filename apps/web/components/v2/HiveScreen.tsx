"use client";

// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope, see app/previews-ui.tsx.
import React from "react";

import type { V2Agent, V2Handoff, Workforce } from "../../lib/v2/workforce";

/**
 * Hive, the agents as a graph, and the work moving between them.
 *
 * THE GEOMETRY IS THE REFERENCE'S, NOT AN APPROXIMATION
 * ----------------------------------------------------
 * An earlier version put every node on one perfect circle with solid straight edges. Beside the
 * real thing it read as a different diagram entirely. The reference uses three rings:
 *
 *   hub    (450, 320) r=30
 *   inner  radius 130, starting at -PI/2
 *   outer  radius 250, starting at -PI/2 + 0.35
 *
 * The 0.35 radian offset on the outer ring is why it looks scattered rather than concentric , 
 * the two rings deliberately do not line up. Nodes carry a halo at r+12, the label sits at
 * y+r+13 and the status at y-r-8, and edges are QUADRATIC curves whose control point is offset
 * 28px perpendicular to the chord. All measured out of the reference's own source rather than
 * eyeballed.
 *
 * WHICH AGENT GOES WHERE IS EARNED, NOT ASSIGNED
 * ----------------------------------------------
 * The reference hardcodes thirteen named agents. BOSS has however many roles a project defines,
 * so the hub is the agent that work actually flows through most, the inner ring is the next
 * five, and the rest go outside. That keeps the picture true for any company and makes position
 * mean something rather than being alphabetical.
 */

const W = 900;
const H = 640;
const CX = 450;
const CY = 320;
const INNER_R = 130;
const OUTER_R = 250;
const OUTER_OFFSET = 0.35;
const HUB_R = 30;
const NODE_R = 24;
const HALO_PAD = 12;
/** How far the control point sits off the chord. Straight lines read as a mesh; these read as
 *  flows. */
const CURVE = 28;

type Placed = V2Agent & { x: number; y: number; r: number; ring: string };

function place(agents: V2Agent[], handoffs: V2Handoff[]): Placed[] {
  // Connectivity, counted. The busiest agent earns the middle.
  const traffic = new Map<string, number>();
  for (const h of handoffs) {
    traffic.set(h.source, (traffic.get(h.source) ?? 0) + h.times);
    traffic.set(h.target, (traffic.get(h.target) ?? 0) + h.times);
  }
  const ranked = [...agents].sort(
    (a, b) => (traffic.get(b.id) ?? 0) - (traffic.get(a.id) ?? 0) || a.name.localeCompare(b.name),
  );

  const hub = ranked.slice(0, 1);
  const inner = ranked.slice(1, 6);
  const outer = ranked.slice(6);

  const out: Placed[] = [];
  for (const a of hub) out.push({ ...a, x: CX, y: CY, r: HUB_R, ring: "hub" });
  inner.forEach((a, i) => {
    const t = -Math.PI / 2 + (i * 2 * Math.PI) / Math.max(inner.length, 1);
    out.push({
      ...a,
      x: Math.round(CX + INNER_R * Math.cos(t)),
      y: Math.round(CY + INNER_R * Math.sin(t)),
      r: NODE_R,
      ring: "inner",
    });
  });
  outer.forEach((a, i) => {
    const t = -Math.PI / 2 + OUTER_OFFSET + (i * 2 * Math.PI) / Math.max(outer.length, 1);
    out.push({
      ...a,
      x: Math.round(CX + OUTER_R * Math.cos(t)),
      y: Math.round(CY + OUTER_R * Math.sin(t)),
      r: NODE_R,
      ring: "outer",
    });
  });
  return out;
}

/** A quadratic whose control point is pushed perpendicular to the chord, exactly as the
 *  reference does it. */
function curve(from: Placed, to: Placed): { d: string; lx: number; ly: number } {
  const mx = (from.x + to.x) / 2;
  const my = (from.y + to.y) / 2;
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const len = Math.hypot(dx, dy) || 1;
  const cx = mx - (dy / len) * CURVE;
  const cy = my + (dx / len) * CURVE;
  return {
    d: `M${from.x} ${from.y} Q${cx.toFixed(0)} ${cy.toFixed(0)} ${to.x} ${to.y}`,
    lx: Math.round(cx),
    ly: Math.round(cy) - 5,
  };
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

  const nodes = place(workforce.agents, workforce.handoffs);
  const byId = new Map(nodes.map((n) => [n.id, n]));

  return (
    <section className="v2-screen" data-testid="v2-hive">
      <header className="v2-screen-head">
        <h1 className="v2-screen-title">Hive</h1>
        <p className="v2-screen-sub">
          {workforce.agents.length} agents, {workforce.handoffs.length} handoff routes observed
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
            viewBox={`0 0 ${W} ${H}`}
            role="img"
            aria-label="Agents and the handoffs between them"
            data-testid="v2-hive-graph"
          >
            {/* The two rings, faint. They are what makes the layout legible as structure
                rather than as scatter. */}
            <circle cx={CX} cy={CY} r={INNER_R} className="v2-hive-orbit" />
            <circle cx={CX} cy={CY} r={OUTER_R} className="v2-hive-orbit" />

            {/* Handoffs, curved and dashed. Thickness is the count, so a busy route reads as
                busy rather than every line looking equally important. */}
            {workforce.handoffs.map((h) => {
              const from = byId.get(h.source);
              const to = byId.get(h.target);
              if (!from || !to) return null;
              const { d, lx, ly } = curve(from, to);
              const busiest = Math.max(1, ...workforce.handoffs.map((x) => x.times));
              return (
                <g key={`${h.source}->${h.target}`}>
                  <path
                    d={d}
                    className="v2-hive-edge"
                    data-status={from.status}
                    strokeWidth={1 + (h.times / busiest) * 1.4}
                    data-testid={`v2-hive-edge-${h.source}-${h.target}`}
                  >
                    <title>{`${h.source} → ${h.target}: ${h.times} handoff${h.times === 1 ? "" : "s"}${h.last_action ? ` (last: ${h.last_action})` : ""}`}</title>
                  </path>
                  {/* The first words of what was handed over, on the curve, so the graph says
                      what moved, not only that something did. */}
                  {h.last_action ? (
                    <text x={lx} y={ly} className="v2-hive-edge-label" textAnchor="middle">
                      {h.last_action.split(/[\s_]+/).slice(0, 3).join(" ")}
                    </text>
                  ) : null}
                </g>
              );
            })}

            {nodes.map((n) => (
              <g key={n.id} data-testid={`v2-hive-node-${n.id}`} data-status={n.status}>
                {/* The halo. Pulses only while the agent is working, so motion means
                    something, a graph where everything pulses says nothing. */}
                <circle
                  cx={n.x}
                  cy={n.y}
                  r={n.r + HALO_PAD}
                  className="v2-hive-halo"
                  data-status={n.status}
                  data-testid={`v2-hive-halo-${n.id}`}
                />
                {/* A RING, not a disc: dark fill, coloured stroke. */}
                <circle
                  cx={n.x}
                  cy={n.y}
                  r={n.r}
                  className="v2-hive-node"
                  data-status={n.status}
                  data-ring={n.ring}
                >
                  <title>{`${n.name}, ${n.status}: ${n.task}`}</title>
                </circle>

                {/* Status above, name below, the reference's own placement. */}
                <text x={n.x} y={n.y - n.r - 8} className="v2-hive-status" textAnchor="middle">
                  {n.status}
                </text>
                <text x={n.x} y={n.y + n.r + 13} className="v2-hive-label" textAnchor="middle">
                  {n.name.length > 16 ? `${n.name.slice(0, 15)}…` : n.name}
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
          <span>
            <b>{h.times}</b> {h.times === 1 ? "handoff" : "handoffs"}
          </span>
          {h.last_action ? (
            <span>
              <span className="v2-row-meta-dim">last</span> {h.last_action}
            </span>
          ) : null}
          {h.last_at ? (
            <span className="v2-row-meta-dim">{h.last_at.slice(0, 16).replace("T", " ")}</span>
          ) : null}
        </span>
      </span>
    </li>
  );
}
