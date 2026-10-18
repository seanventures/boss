"use client";

/**
 * THE RUN'S AUTONOMY, IN ONE LINE (docs/product/BOSS_FIXES_ITSELF.md §12): "3 problems · BOSS
 * solved 2 · you solved 1". A problem is one failure episode of a step; how it was resolved is
 * decided when it closes — by BOSS's agents end to end (approving counts as governance, not work),
 * by a person, by an outside coding agent, or still open. Nothing is drawn for a run with no
 * problems: a clean run needs no line.
 */
import React from "react";

import { usePolledRead } from "../../lib/log-stream";
import { pct, polledSelfFix, type RunAutonomyView } from "../../lib/self-fix";

export const RUN_AUTONOMY_POLL_MS = 30_000;

export const AUTONOMY_TIP = "A problem is one failure of a step. BOSS solved it: its agents fixed it end to end "
  + "(your approval counts as governance, not work). You: a person answered or fixed it. Outside agent: "
  + "a coding agent outside BOSS changed BOSS. Open: not resolved yet.";

export function runAutonomyLine(v: RunAutonomyView): string {
  if (v.sentence) return v.sentence;
  const c = v.counts;
  const parts = [`${c.problems} problem${c.problems === 1 ? "" : "s"}`];
  if (c.by_boss) parts.push(`BOSS solved ${c.by_boss}`);
  if (c.by_person) parts.push(`you solved ${c.by_person}`);
  if (c.by_outside_agent) parts.push(`outside agent ${c.by_outside_agent}`);
  if (c.open) parts.push(`${c.open} open`);
  return parts.join(" · ");
}

export function RunAutonomy({ projectId, runId, live }: { projectId: string; runId: string; live: boolean }) {
  const read = usePolledRead<RunAutonomyView>({
    key: `${projectId}:${runId}`, pollMs: live ? RUN_AUTONOMY_POLL_MS : null, paused: !live,
    read: polledSelfFix<RunAutonomyView>("run_autonomy", { project_id: projectId, run_id: runId }),
  });
  const v = read.data;
  if (!v || !v.counts || !v.counts.problems) return null;
  return (
    <span className="wf-tag sfx-run-autonomy" data-testid="dw-run-autonomy"
          title={`${AUTONOMY_TIP} BOSS solved ${pct(v.rate)} of this run's problems.`}>
      {runAutonomyLine(v)}
    </span>
  );
}
