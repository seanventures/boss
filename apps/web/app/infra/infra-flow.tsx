"use client";

/**
 * THE INCIDENT AS A ROW OF NODES (the operator's standing rule: visibility is a diagram, not
 * text). Detected → Paused 14 runs → Cleaning 3 of 5 → Retention (asks) → Freed → Resumed, drawn
 * from the API's `flow` (StripNodes with a state and a time, docs/infra/INFRA_AGENT_API.md §2)
 * the way FlowThumb draws a work item: one shape per node, a two-word caption, the detail and
 * the time under it, the sentence in the tooltip. The same component serves the Command Center
 * card (S2) and the report (S5). `role="img"` with the whole flow as one sentence.
 */
import React from "react";
import { Archive, ArrowRight, Broom, Check, Pause, Play, Sparkle, UserCircle, Warning, type Icon } from "@phosphor-icons/react";

import { clampWords } from "../../lib/ambient";
import { clock, type FlowNode } from "../../lib/infra-storage";
import { TYPE_WORDS } from "../agent-ops/flow-canvas";

const ICONS: Record<string, Icon> = { Warning, Pause, Broom, Archive, Check, Play, Sparkle, UserCircle };
const BY_KEY: Record<string, Icon> = { detected: Warning, paused: Pause, cleaning: Broom, asking: Archive, freed: Check, resumed: Play, reported: Sparkle };
const BY_TYPE: Record<string, Icon> = { signal: Warning, action: Broom, needs_you: Archive, person: UserCircle, outcome: Check, learning: Sparkle, decision: Check };

function glyphOf(n: FlowNode): Icon {
  if (n.icon && ICONS[n.icon]) return ICONS[n.icon];
  if (BY_KEY[n.key]) return BY_KEY[n.key];
  return BY_TYPE[n.type] ?? Check;
}

export function flowSentence(flow: FlowNode[]): string {
  return flow.map((n) => {
    const at = clock(n.at);
    return `${TYPE_WORDS[n.type] ?? n.type}: ${n.label}${n.detail ? ` ${n.detail}` : ""}${at ? ` at ${at}` : ""}${n.state === "active" ? " (now)" : n.state === "ask" ? " (asks you)" : ""}`;
  }).join(" → ");
}

export function InfraFlow({ flow, size = "card", testId = "infra-flow" }: { flow: FlowNode[]; size?: "card" | "report" | "strip"; testId?: string }) {
  if (!flow.length) return null;
  const said = flowSentence(flow);
  return (
    <ol className="ifl" role="img" aria-label={said} title={said} data-size={size} data-testid={testId}>
      {flow.map((n, i) => {
        const Glyph = glyphOf(n);
        const at = clock(n.at);
        return (
          <li key={`${n.key}-${i}`} className="ifl-node" data-state={n.state} data-type={n.type} data-key={n.key} data-testid={`${testId}-node`}>
            <span className="ifl-shape" aria-hidden>
              <Glyph size={size === "strip" ? 12 : 18} weight="bold" />
            </span>
            <span className="ifl-cap">{clampWords(n.label, 2)}</span>
            {size !== "strip" && n.detail ? <span className="ifl-detail">{n.detail}</span> : null}
            {size !== "strip" && at ? <span className="ifl-at">{at}</span> : null}
            {i < flow.length - 1 ? <span className="ifl-wire" aria-hidden><ArrowRight size={12} weight="bold" /></span> : null}
          </li>
        );
      })}
    </ol>
  );
}
