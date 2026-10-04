"use client";

/**
 * A build's agents, from its signed work (spec §3.7).
 *
 * The stage ribbon shows one face per stage taken from the build's stage acts, not from a
 * stage→role table, so an engineer the operator assigned is the face on Implement. The PR footer
 * says how many agents made it, who is accountable, and opens the run's Credits.
 *
 * A build that was not started by a workflow run (no `origin.workflow_run_id`), or an install
 * whose API has no chain yet, draws exactly what it drew before: no faces, no footer.
 */
import React, { useMemo, useState } from "react";

import type { Act, Chain } from "../../lib/agent-world-types";
import { ActFace } from "./act-card";
import { agentsIn } from "./chain-model";
import { useRunChain } from "./run-acts";
import { CreditsDialog } from "./run-credits";

/** The stage an act ran: its `stage` link, else a known stage name inside its label. */
export function stageOf(act: Act, known: string[]): string | null {
  if (act.links.stage) return act.links.stage;
  const words = act.label.toLowerCase().replace(/[^a-z0-9_ ]+/g, " ").split(/\s+/);
  const joined = ` ${words.join(" ")} `;
  for (const k of known) {
    if (words.includes(k) || joined.includes(` ${k.replace(/_/g, " ")} `)) return k;
  }
  return null;
}

/** The latest stage act per phase, for a ribbon keyed by phase. */
export function stageActsByPhase(acts: Act[], phaseStages: Record<string, string[]>): Record<string, Act> {
  const known = [...new Set(Object.values(phaseStages).flat())];
  const out: Record<string, Act> = {};
  for (const a of acts) {
    if (a.kind !== "stage") continue;
    const stage = stageOf(a, known);
    if (!stage) continue;
    const phase = Object.keys(phaseStages).find((p) => phaseStages[p].includes(stage));
    if (phase) out[phase] = a;
  }
  return out;
}

/** The build's chain and its own stage acts. */
export function useBuildWork(projectId: string, workflowRunId: string | null, codeTaskRunId: string, live: boolean): {
  chain: Chain | null; stages: Act[];
} {
  const { chain } = useRunChain(projectId, workflowRunId, live);
  const stages = useMemo(
    () => (chain?.acts ?? []).filter((a) => a.kind === "stage" && (!a.links.code_task_run_id || a.links.code_task_run_id === codeTaskRunId)),
    [chain, codeTaskRunId]);
  return { chain, stages };
}

/** Faces for the stage ribbon, keyed by phase. */
export function useStageFaces(stages: Act[], phaseStages: Record<string, string[]>, viewer = ""): Record<string, React.ReactNode> {
  return useMemo(() => {
    const by = stageActsByPhase(stages, phaseStages);
    return Object.fromEntries(Object.entries(by).map(([phase, act]) => [
      phase, <ActFace key={act.actId} act={act} viewer={viewer} size={20} />,
    ]));
  }, [stages, phaseStages, viewer]);
}

/** `Made by 5 agents · accountable: <you> · Credits ↗` under the pull request. */
export function PrMadeBy({ chain, runId, projectId, viewer = "", title }: {
  chain: Chain | null; runId: string | null; projectId: string; viewer?: string; title: string;
}) {
  const [open, setOpen] = useState(false);
  if (!chain || !runId) return null;
  const n = agentsIn(chain).length;
  if (!n) return null;
  const who = chain.accountable && chain.accountable === viewer ? "you" : chain.accountable || "—";
  return (
    <span className="ag-made-by" data-testid="ag-pr-made-by">
      Made by {n} {n === 1 ? "agent" : "agents"} · accountable: {who} ·{" "}
      <button type="button" className="ag-link-btn" data-testid="ag-pr-credits" onClick={() => setOpen(true)}>
        Credits ↗
      </button>
      {open ? (
        <CreditsDialog chain={chain} runId={runId} projectId={projectId} viewer={viewer} title={title}
                       onClose={() => setOpen(false)} />
      ) : null}
    </span>
  );
}
