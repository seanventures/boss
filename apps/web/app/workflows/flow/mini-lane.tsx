"use client";

/**
 * A library card's whole flow in one line: a 16px icon per step joined by hairlines.
 *
 * It never wraps. Past MAX icons the rest is a `+n`, and every icon's name is its tooltip, so
 * the card says the shape of the workflow without a word of text.
 */
import React from "react";

import type { WorkflowSummary } from "../../../lib/workflow-editor-types";
import { useTips } from "./tip-layer";
import { NodeIcon } from "./type-icons";

export const MINI_LANE_MAX = 14;

export function MiniLane({ steps }: { steps: WorkflowSummary["steps"] }): React.JSX.Element {
  useTips();
  const shown = steps.slice(0, MINI_LANE_MAX);
  const rest = steps.length - shown.length;
  return (
    <ol className="wf-mini" aria-label={`${steps.length} steps`} data-testid="wf-mini-lane">
      {shown.map((s) => (
        <li key={s.id} className="wf-mini-step" data-tip={s.title} aria-label={s.title} tabIndex={-1}
            data-gate={s.approval === "manual" ? "true" : undefined}>
          <NodeIcon icon={s.capability} type={s.capability ? "builtin" : s.captain ? "agent" : "other"} size={10} />
        </li>
      ))}
      {rest > 0 ? (
        <li className="wf-mini-more" data-tip={steps.slice(MINI_LANE_MAX).map((s) => s.title).join(", ")}>
          +{rest}
        </li>
      ) : null}
    </ol>
  );
}
