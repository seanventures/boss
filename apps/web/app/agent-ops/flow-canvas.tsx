"use client";

/**
 * ONE WORK ITEM AS A FLOW CHART (design §11.0 / §11.3; spec §7.1). Left to right: the signal,
 * the agents (their real faces) and how they handed it on, what they proposed, how they voted,
 * the decision (a diamond), what ran, and how it ended. Words are labels only (≤ 4 per node,
 * ≤ 3 per arrow); badges carry type, level, risk, dissent. A dissenting vote is its own red,
 * dashed arrow that splits off the agreeing ones. "Needs you" is an amber node with its one
 * button. Click any node — or a dissenting arrow — for the drawer; nothing else is text.
 *
 * Nodes are HTML (focusable buttons with faces); arrows are one SVG beneath them, placed by
 * lib/ambient-layout.ts. Wide charts scroll inside their own frame, never the page.
 */
import React, { useId, useMemo } from "react";

import { AgentAvatar } from "../agent-avatar";
import { type FlowEdge, type FlowNode, partyOfNode, roleOfParty } from "../../lib/ambient";
import { activeNodeId, layoutFlow, type PlacedNode } from "../../lib/ambient-layout";

export const TYPE_WORDS: Record<string, string> = {
  signal: "Signal", agent: "Agent", person: "Person", policy: "Policy", squad: "Squad", boss: "BOSS",
  action: "Proposed action", decision: "Decision", needs_you: "Needs you", run: "Work started", outcome: "Outcome",
};
const ICON: Record<string, string> = { signal: "🔔", policy: "§", squad: "⚑", boss: "◆", action: "▶", run: "⚙" };

function outcomeTone(n: FlowNode): "ok" | "bad" | "quiet" {
  if (n.badges.includes("failed")) return "bad";
  if (n.badges.includes("dismissed")) return "quiet";
  return "ok";
}

/** The face for a party node: an agent's own, a person's, or the system's. */
export function NodeFace({ node, size = 28 }: { node: FlowNode; size?: number }) {
  const who = partyOfNode(node);
  const role = roleOfParty(who);
  if (node.type === "agent" && role) return <AgentAvatar agent={role} size={size} aiBadge={false} />;
  if (node.type === "person") return <AgentAvatar variant="person" size={size} />;
  if (node.type === "boss") return <AgentAvatar variant="boss" size={size} />;
  return null;
}

function NodeBody({ node, active }: { node: PlacedNode; active: boolean }) {
  const face = <NodeFace node={node} />;
  const chips = node.badges.filter((b) => b && b !== node.type).slice(0, 2);
  return (
    <>
      {node.type === "decision" ? (
        <svg className="aa-diamond" viewBox={`0 0 ${node.w} ${node.h}`} preserveAspectRatio="none" aria-hidden>
          <polygon points={`${node.w / 2},1 ${node.w - 1},${node.h / 2} ${node.w / 2},${node.h - 1} 1,${node.h / 2}`} />
        </svg>
      ) : null}
      <span className="aa-node-main">
        {face ? <span className="aa-node-face">{face}</span>
          : node.type === "outcome" ? <span className="aa-node-icon" data-tone={outcomeTone(node)} aria-hidden>{outcomeTone(node) === "bad" ? "✕" : outcomeTone(node) === "quiet" ? "–" : "✓"}</span>
          : node.type === "needs_you" ? <span className="aa-node-icon" aria-hidden>!</span>
          : ICON[node.type] ? <span className="aa-node-icon" aria-hidden>{ICON[node.type]}</span> : null}
        <span className="aa-node-label" data-testid="aa-node-label">{node.label}</span>
      </span>
      {node.type === "needs_you" ? null : chips.length ? (
        <span className="aa-node-chips">
          {chips.map((c) => <span key={c} className="aa-chip-xs" data-chip={c}>{c}</span>)}
        </span>
      ) : null}
      {active ? <span className="aa-pulse" aria-hidden data-testid="aa-active" /> : null}
    </>
  );
}

export function FlowCanvas({ nodes, edges, lifecycle, selected, onSelect, onAnswer, compact = false }: {
  nodes: FlowNode[]; edges: FlowEdge[]; lifecycle: string; selected?: string | null;
  onSelect: (target: { node?: FlowNode; edge?: FlowEdge }) => void; onAnswer?: () => void; compact?: boolean;
}) {
  const uid = useId().replace(/:/g, "");
  const layout = useMemo(() => layoutFlow(nodes, edges), [nodes, edges]);
  const active = activeNodeId(layout, lifecycle);
  if (!nodes.length) return <div className="aa-canvas-empty" data-testid="aa-canvas-empty">Nothing drawn yet</div>;
  return (
    <div className="aa-canvas-frame" data-testid="aa-canvas" data-compact={compact || undefined}>
      <div className="aa-canvas" style={{ width: layout.width, height: layout.height }}>
        <svg className="aa-edges" width={layout.width} height={layout.height} aria-hidden>
          <defs>
            <marker id={`${uid}-a`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0 0 L10 5 L0 10 z" className="aa-arrowhead" />
            </marker>
            <marker id={`${uid}-d`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0 0 L10 5 L0 10 z" className="aa-arrowhead dissent" />
            </marker>
          </defs>
          {layout.edges.map((e) => (
            <g key={e.key} className="aa-edge" data-kind={e.kind} data-dissent={e.dissent || undefined} data-back={e.back || undefined}
               data-testid="aa-edge">
              <path d={e.d} className="aa-edge-line" markerEnd={`url(#${uid}-${e.dissent ? "d" : "a"})`} />
            </g>
          ))}
        </svg>
        {/* Arrow labels are HTML so a dissent can be pressed to read why. */}
        {layout.edges.filter((e) => e.label && !["raised", "resulted", "settles", "decided", "acted", "asks"].includes(e.kind) || e.dissent).map((e) => {
          const text = `${e.dissent ? "✕ " : ""}${e.label}${e.count > 1 ? ` ×${e.count}` : ""}`;
          const style = { left: e.lx, top: e.ly };
          return e.ref || e.dissent ? (
            <button key={`l-${e.key}`} type="button" className="aa-edge-label" style={style} data-dissent={e.dissent || undefined}
                    data-testid="aa-edge-label" title={e.dissent ? "Dissent — see why" : "See the message"}
                    onClick={() => onSelect({ edge: e })}>{text}</button>
          ) : (
            <span key={`l-${e.key}`} className="aa-edge-label" style={style} data-testid="aa-edge-label">{text}</span>
          );
        })}
        {layout.nodes.map((n) => (
          <button key={n.id} type="button" className="aa-node" data-type={n.type} data-testid="aa-node" data-node-id={n.id}
                  data-selected={selected === n.id || undefined} data-active={active === n.id || undefined}
                  data-tone={n.type === "outcome" ? outcomeTone(n) : undefined}
                  style={{ left: n.x, top: n.y, width: n.w, height: n.h }}
                  aria-label={`${TYPE_WORDS[n.type] ?? n.type}: ${n.label}`} title={`${TYPE_WORDS[n.type] ?? n.type} · open details`}
                  onClick={() => onSelect({ node: n })}>
            <NodeBody node={n} active={active === n.id} />
          </button>
        ))}
        {/* THE ONE BUTTON on Needs you (§11.0 rule 5): a sibling, never a button inside a button. */}
        {onAnswer ? layout.nodes.filter((n) => n.type === "needs_you").map((n) => (
          <button key={`ans-${n.id}`} type="button" className="aa-node-answer" data-testid="aa-needs-you-answer"
                  style={{ left: n.x + n.w / 2, top: n.y + n.h }} onClick={onAnswer}>Answer</button>
        )) : null}
      </div>
    </div>
  );
}
