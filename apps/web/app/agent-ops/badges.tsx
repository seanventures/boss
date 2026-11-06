"use client";

/** An item's facts as badges and colours, never sentences (§11.0 rule 2): problem type ·
 *  outcome · autonomy level · cost, plus dissent and severity when they apply. */
import React from "react";

import { LEVEL_WORDS, money, outcomeWords, type ItemBadges } from "../../lib/ambient";

export function ItemBadgeRow({ b, compact = false }: { b: ItemBadges; compact?: boolean }) {
  const out = outcomeWords(b.outcome);
  return (
    <span className="aa-badges" data-testid="aa-badges">
      <span className="aa-badge" data-tone="type" data-testid="aa-badge-type" title="Problem type">{b.problem_type || b.task_class}</span>
      <span className="aa-badge" data-tone={out.tone} data-testid="aa-badge-outcome" title="Outcome">{out.label}</span>
      {b.autonomy_level ? (
        <span className="aa-badge" data-tone="level" data-testid="aa-badge-level"
              title={`Autonomy ${b.autonomy_level}: ${LEVEL_WORDS[b.autonomy_level] ?? ""}`}>{b.autonomy_level}{compact ? "" : ` · ${LEVEL_WORDS[b.autonomy_level] ?? ""}`}</span>
      ) : null}
      <span className="aa-badge" data-tone="cost" data-testid="aa-badge-cost" title="What the agents spent on it">{money(b.cost_usd)}</span>
      {b.dissent ? <span className="aa-badge" data-tone="red" data-testid="aa-badge-dissent" title="An agent disagreed; open the decision to see why">⑂ dissent</span> : null}
      {!compact && b.severity && b.severity !== "low" ? <span className="aa-badge" data-tone={b.severity === "high" ? "red" : "amber"} title="Severity">{b.severity}</span> : null}
    </span>
  );
}
