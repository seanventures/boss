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
 * lib/ambient-layout.ts.
 *
 * ZOOM TO FIT (operator, 2026-11-07: "in the video it was full screen, this one is not"): the
 * frame takes the room the page gives it — its full width and, on a desktop, the viewport's
 * height below it — and the chart is scaled to fit both, centred, up to FIT.max (a node never
 * grows past a sensible size) and down to FIT.min (still legible; past that the frame scrolls,
 * never the page). It re-fits whenever the frame or the window changes size (a drawer opening
 * beside it, a resize). Labels wrap to three lines and carry their full words as a tooltip.
 */
import React, { useEffect, useId, useMemo, useRef, useState } from "react";

import { AgentAvatar } from "../agent-avatar";
import { type FlowEdge, type FlowNode, partyOfNode, roleOfParty } from "../../lib/ambient";
import { activeNodeId, layoutFlow, type PlacedNode } from "../../lib/ambient-layout";

export const TYPE_WORDS: Record<string, string> = {
  signal: "Signal", agent: "Agent", person: "Person", policy: "Policy", squad: "Squad", boss: "BOSS",
  action: "Proposed action", decision: "Decision", needs_you: "Needs you", run: "Work started", outcome: "Outcome",
  learning: "Applied learning",
};
const ICON: Record<string, string> = { signal: "↯", policy: "§", squad: "⚑", boss: "◆", action: "▶", run: "⚙", learning: "✦" };

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
  const face = ["agent", "person", "boss"].includes(node.type) ? <NodeFace node={node} /> : null;
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

/** How far the chart may zoom: never smaller than legible, never a node larger than ~1.6×. */
export const FIT = { min: 0.6, max: 1.6 } as const;
/** Below the frame: the legend row and the page's bottom margin. */
const BELOW = 64;
/** The frame fills the viewport's height only on a desktop-sized frame; a phone hugs the chart. */
const FILL_MIN_W = 720;
const FILL_MIN_H = 320;

/** The zoom that fits a `lw`×`lh` chart in a `w`×`h` box (h null: width only), clamped to FIT. */
export function fitScale(lw: number, lh: number, w: number, h: number | null): number {
  if (!w || !lw || !lh) return 1;
  const s = Math.min((w - 2) / lw, h ? (h - 2) / lh : Number.POSITIVE_INFINITY);
  return Math.max(FIT.min, Math.min(FIT.max, s));
}

type Room = { w: number; h: number | null; base: number };
/** The laptop root (layout-scale.css): the size the chart's px layout is drawn for. */
const BASE_ROOT_PX = 19;
function rootPx(): number {
  if (typeof window === "undefined" || typeof getComputedStyle === "undefined") return BASE_ROOT_PX;
  const v = parseFloat(getComputedStyle(document.documentElement).fontSize);
  return Number.isFinite(v) && v > 0 ? v : BASE_ROOT_PX;
}

/** The frame's width and, on a desktop, the height left in the viewport below its top. */
function measure(el: HTMLElement, compact: boolean): Room {
  const w = el.clientWidth;
  const base = rootPx();
  if (compact || typeof window === "undefined" || w < FILL_MIN_W) return { w, h: null, base };
  const top = el.getBoundingClientRect().top + (window.scrollY || 0);
  const h = Math.round(window.innerHeight - top - BELOW);
  return { w, h: h >= FILL_MIN_H ? h : null, base };
}

export function FlowCanvas({ nodes, edges, lifecycle, selected, onSelect, onAnswer, compact = false }: {
  nodes: FlowNode[]; edges: FlowEdge[]; lifecycle: string; selected?: string | null;
  onSelect: (target: { node?: FlowNode; edge?: FlowEdge }) => void; onAnswer?: () => void; compact?: boolean;
}) {
  const uid = useId().replace(/:/g, "");
  const layout = useMemo(() => layoutFlow(nodes, edges), [nodes, edges]);
  const active = activeNodeId(layout, lifecycle);
  // ZOOM TO FIT the frame's width and the viewport's height below it; re-fit on every resize.
  const frame = useRef<HTMLDivElement>(null);
  const [room, setRoom] = useState<Room | null>(null);
  useEffect(() => {
    const el = frame.current;
    if (!el) return undefined;
    const fit = () => setRoom((cur) => {
      const next = measure(el, compact);
      return cur && cur.w === next.w && cur.h === next.h && cur.base === next.base ? cur : next;
    });
    fit();
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(fit);
    ro?.observe(el);
    window.addEventListener("resize", fit);
    return () => { ro?.disconnect(); window.removeEventListener("resize", fit); };
  }, [compact, nodes.length]);
  const scale = room?.w ? fitScale(layout.width, layout.height, room.w, room.h) : 1;
  const drawnH = layout.height * scale;
  // The frame fills the height it was given (the chart centred in it), never less than the chart.
  const frameH = room?.h ? Math.max(room.h, Math.ceil(drawnH) + 2) : undefined;
  if (!nodes.length) return <div className="aa-canvas-empty" data-testid="aa-canvas-empty">Nothing drawn yet</div>;
  return (
    <div className="aa-canvas-frame" data-testid="aa-canvas" data-compact={compact || undefined} ref={frame} data-scale={scale.toFixed(2)}
         data-fill={frameH ? "" : undefined} style={frameH ? { height: frameH } : undefined}>
      <div className="aa-canvas-fit" style={{ width: layout.width * scale, height: drawnH }}>
      {/* The chart's own type base: the laptop root, whatever the screen's root (its words are
          em, its boxes px — they stay in proportion; the whole chart grows by zoom-to-fit). */}
      <div className="aa-canvas" style={{ width: layout.width, height: layout.height, transform: scale !== 1 ? `scale(${scale})` : undefined,
                                          fontSize: room && room.base !== BASE_ROOT_PX ? `${(BASE_ROOT_PX / room.base).toFixed(4)}rem` : undefined }}>
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
                  aria-label={`${TYPE_WORDS[n.type] ?? n.type}: ${n.label}`}
                  title={[n.label, n.detail ?? `${TYPE_WORDS[n.type] ?? n.type} · open details`].filter(Boolean).join(" — ")}
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
    </div>
  );
}
