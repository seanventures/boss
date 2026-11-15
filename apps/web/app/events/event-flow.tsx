"use client";

/**
 * AN EVENT AS A ROW OF NODES (the operator's standing rule: visibility is a diagram, not text).
 * Source → Checked → Saved → Rule → For whom → Run → Reply (lib/intake.ts `eventFlow`): one shape
 * per step, a two-word caption, the detail under it, the why in the tooltip. Where it stopped is
 * red; what never happened after it is dashed and faint. `role="img"` with the flow as one
 * sentence for a screen reader. The Slack test (E2) draws Signed → Saved → Replied with it too.
 */
import React from "react";
import {
  ArrowRight, ChatCircleText, Check, Funnel, Hourglass, LinkSimple, ListBullets, Play, Plug, Prohibit, ShieldCheck, Tray, User, X, type Icon,
} from "@phosphor-icons/react";

import { flowSentence, type FlowNode } from "../../lib/intake";

const BY_KEY: Record<string, Icon> = {
  source: Plug, checked: ShieldCheck, signed: ShieldCheck, saved: Tray, rule: ListBullets, whom: User, prompt: LinkSimple,
  run: Play, reply: ChatCircleText, replied: ChatCircleText,
};

function glyph(n: FlowNode): Icon {
  if (n.state === "failed") return n.key === "rule" ? Funnel : n.key === "run" ? Prohibit : X;
  if (n.state === "waiting" || n.state === "active") return Hourglass;
  return BY_KEY[n.key] ?? Check;
}

export function EventFlowView({ nodes, size, testId = "ev-flow" }: { nodes: FlowNode[]; size?: "sm"; testId?: string }) {
  if (!nodes.length) return null;
  const said = flowSentence(nodes);
  return (
    <ol className="ev-flow" role="img" aria-label={`Event flow: ${said}`} title={said} data-size={size} data-testid={testId}>
      {nodes.map((n, i) => {
        const G = glyph(n);
        return (
          <li key={`${n.key}-${i}`} className="ev-flow-node" data-state={n.state} data-key={n.key} title={n.tip} data-testid={`${testId}-${n.key}`}>
            <span className="ev-flow-shape" aria-hidden><G size={size === "sm" ? 13 : 17} weight="bold" /></span>
            <span className="ev-flow-cap">{n.label}</span>
            {n.detail ? <span className="ev-flow-detail">{n.detail}</span> : null}
            {i < nodes.length - 1 ? <span className="ev-flow-wire" aria-hidden><ArrowRight size={12} weight="bold" /></span> : null}
          </li>
        );
      })}
    </ol>
  );
}
