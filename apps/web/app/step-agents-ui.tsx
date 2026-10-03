"use client";

/**
 * Who ran a workflow step — the run view's agent chip and its "Ran as" line. (Choosing the agent
 * and an agent step's scope is the editor's inspector: app/workflows/editor/inspector/.)
 *
 * WHY THESE LIVE TOGETHER. They are faces of one rule (engine/boss_engine/services/
 * step_agents.py): a step runs as the agent it names, else the agent holding what it needs,
 * else BOSS's pick among the agents that could do it, else it is refused. The picker used to
 * offer a hard-coded list of 29 role keys — five of which no project has — while an agent made
 * on the Agents page never appeared; and when BOSS picked the agent, nothing on the run said
 * which one ran or why. The engine records `ran_as` on every step now; this draws it.
 */
import React from "react";

/** WHICH AGENT TO SHOW BESIDE A STEP in the run view, and what the chip should claim.
 *
 *  It fell back to the capability's default captain, so a built-in step that ran as whoever
 *  started the run showed "engineering_captain — answers for this step" directly above "Ran as
 *  ceo — ran as whoever started the run": two answers to one question, the first an agent that
 *  did not run it and that most projects do not have. Now: the agent the workflow named; else,
 *  once it has run, the agent that RAN it, or no chip when no agent did (the Ran-as line says
 *  who); else, before it runs, the default captain — labelled as someone to ask, not someone
 *  who ran it. */
export const NOT_AN_AGENT = ["run_actor", "workflow_runner", "legacy_workflow_runner",
                             "decision_model"];

export function stepAgentChip(named: string | null | undefined, output: unknown,
                              defaultCaptain: string | null | undefined):
    { role: string; note: string } | null {
  const name = String(named ?? "").trim();
  if (name) return { role: name, note: "named by this workflow" };
  const ran = (output && typeof output === "object"
    ? (output as { ran_as?: RanAsRecord }).ran_as : undefined);
  if (ran) {
    const agent = String(ran.agent ?? "").trim();
    return agent && !NOT_AN_AGENT.includes(ran.how ?? "")
      ? { role: agent, note: "ran this step" } : null;
  }
  const fallback = String(defaultCaptain ?? "").trim();
  return fallback ? { role: fallback, note: "can answer questions about this step" } : null;
}

/** How the agent came to be the one, in the operator's words. */
export const HOW_CHOSEN: Record<string, string> = {
  named: "named on the step",
  model: "chosen by BOSS",
  word_overlap: "chosen by matching the instruction to skills",
  only_holder: "the only agent that could",
  only_candidate: "the only agent holding any skill",
  run_actor: "ran as whoever started the run",
  workflow_runner: "ran as the workflow itself",
  legacy_workflow_runner: "ran as the workflow, as this version was published to",
  decision_model: "answered by the decision model",
};

export type RanAsRecord = {
  agent?: string;
  how?: string;
  why?: string;
  skills_offered?: string[];
  skills_used?: string[];
  mcp_offered?: string[];
  tool?: string | null;
};

/** The run view's "Ran as <agent> — how and why, skills offered vs used" line. */
export function RanAs({ output, stepId }: { output: unknown; stepId: string }) {
  const record = (output && typeof output === "object"
    ? (output as { ran_as?: RanAsRecord }).ran_as : undefined) ?? undefined;
  if (!record || !record.agent) return null;
  const how = HOW_CHOSEN[record.how ?? ""] ?? record.how ?? "";
  const offered = record.skills_offered ?? [];
  const used = record.skills_used ?? [];
  const mcp = record.mcp_offered ?? [];
  return (
    <div className="dw-ran-as detail" data-testid={`dw-ran-as-${stepId}`}>
      <span>
        Ran as <strong>{record.agent}</strong>
        {how ? ` — ${how}` : ""}
        {record.why && record.why !== how ? `: ${record.why}` : ""}
      </span>
      {offered.length || mcp.length ? (
        <span className="dw-ran-as-skills">
          {" · "}offered {[...offered, ...mcp.map((m) => `MCP ${m}`)].join(", ")}
          {" · "}used {used.length ? used.join(", ") : "none"}
        </span>
      ) : null}
    </div>
  );
}
