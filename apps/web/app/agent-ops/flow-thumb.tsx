"use client";

/**
 * A WORK ITEM AS A SMALL DIAGRAM (design §11.0 rule 4): the list of work is a list of these,
 * never a table of text. The strip is the API's own (≤ 6 nodes: the first signal, the owner,
 * the last decision, Needs you / the outcome); each node is its shape and colour, an agent is its
 * face, and a two-word caption sits under each.
 */
import React from "react";

import { AgentAvatar } from "../agent-avatar";
import { clampWords, type StripNode } from "../../lib/ambient";
import { TYPE_WORDS } from "./flow-canvas";

function Shape({ n, size }: { n: StripNode; size: number }) {
  if (n.type === "agent") return <AgentAvatar agent={n.label} size={size} aiBadge={false} />;
  if (n.type === "person") return <AgentAvatar variant="person" size={size} />;
  const glyph = n.type === "signal" ? "↯" : n.type === "needs_you" ? "!" : n.type === "outcome"
    ? (n.badges.includes("failed") ? "✕" : n.badges.includes("dismissed") ? "–" : "✓") : n.type === "decision" ? "" : "▶";
  return <span className="aa-thumb-shape" aria-hidden data-type={n.type}
               data-tone={n.type === "outcome" ? (n.badges.includes("failed") ? "bad" : "ok") : undefined}>{glyph}</span>;
}

export function FlowThumb({ strip, live = false, size = 22 }: { strip: StripNode[]; live?: boolean; size?: number }) {
  const said = strip.map((n) => `${TYPE_WORDS[n.type] ?? n.type}: ${n.label}`).join(" → ");
  return (
    <span className="aa-thumb" role="img" aria-label={said} title={said} data-testid="aa-thumb" data-live={live || undefined}>
      {strip.map((n, i) => (
        <React.Fragment key={`${n.type}-${i}`}>
          {i ? <span className="aa-thumb-wire" aria-hidden /> : null}
          <span className="aa-thumb-node" data-type={n.type} data-last={i === strip.length - 1 || undefined}>
            <Shape n={n} size={size} />
            <span className="aa-thumb-cap">{clampWords(n.label, 2)}</span>
          </span>
        </React.Fragment>
      ))}
    </span>
  );
}
