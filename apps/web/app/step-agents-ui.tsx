"use client";

/**
 * Who runs a workflow step — the builder's picker, an agent step's scope, and the run view's
 * "Ran as" line.
 *
 * WHY THESE LIVE TOGETHER. They are three faces of one rule (engine/boss_engine/services/
 * step_agents.py): a step runs as the agent it names, else the agent holding what it needs,
 * else BOSS's pick among the agents that could do it, else it is refused. The picker used to
 * offer a hard-coded list of 29 role keys — five of which no project has — while an agent made
 * on the Agents page never appeared; and when BOSS picked the agent, nothing on the run said
 * which one ran or why. The engine records `ran_as` on every step now; this draws it.
 */
import React from "react";

export type StepAgent = { role_key: string; name?: string; holds?: string[] };

/** The person who answers a gate. Offered on gates only — a person cannot run a step. */
export const HUMAN = "human";

export function StepAgentPicker({
  value, onChange, roles, holdings, isGate, emptyLabel, testId, ariaLabel,
}: {
  value: string | undefined;
  onChange: (next: string | undefined) => void;
  /** The project's agents (from /v1/delivery-workflows/capabilities), plus `human`. */
  roles: string[];
  holdings: Record<string, string[]>;
  isGate: boolean;
  emptyLabel: string;
  testId: string;
  ariaLabel: string;
}) {
  const agents = roles.filter((role) => role !== HUMAN);
  const stale = stalePick(value, agents, isGate);
  return (
    <select className="input dw-draft-captain" value={value ?? ""}
            onChange={(e) => onChange(e.target.value || undefined)}
            aria-label={ariaLabel} data-testid={testId}>
      <option value="">{emptyLabel}</option>
      {isGate ? <option value={HUMAN}>a person (human)</option> : null}
      {agents.map((role) => {
        const held = (holdings[role] ?? []).length;
        return (
          <option key={role} value={role}>
            {role} · {held} skill{held === 1 ? "" : "s"}
          </option>
        );
      })}
      {stale ? <option value={stale.value}>{stale.label}</option> : null}
    </select>
  );
}

/** A loaded value the picker cannot offer, with the reason, or null.
 *
 *  A NAME THE PROJECT DOES NOT HAVE stays visible rather than silently becoming "nobody": the
 *  save would refuse it, and the operator should see why before pressing Save. `human` on a
 *  step that is not a gate is the same case — it was valid before a person could only answer a
 *  gate, so specs carry it, and the picker showed such a step as empty while Save returned a
 *  422 nothing on the canvas explained. */
export function stalePick(value: string | undefined, agents: string[],
                          isGate: boolean): { value: string; label: string } | null {
  if (!value) return null;
  if (value === HUMAN) {
    return isGate ? null : { value, label: "human — only a gate can wait for a person" };
  }
  return agents.includes(value) ? null
    : { value, label: `${value} — not an agent on this project` };
}

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

/**
 * Which of the agent's skills, and which MCP servers, an agent step may use.
 *
 * Nothing chosen means "everything the agent holds, no MCP" — what an agent step always did.
 * Choosing narrows it. The skills offered are the named agent's holdings, or everything any
 * agent holds when BOSS will pick: a step can scope an agent, never grant it something.
 */
export function AgentStepScope({
  stepId, skills, mcp, available, servers, onChange,
}: {
  stepId: string;
  skills: string[];
  mcp: string[];
  available: string[];
  servers: { id: string; name: string }[];
  onChange: (next: { skills: string[]; mcp: string[] }) => void;
}) {
  const toggle = (list: string[], item: string) =>
    list.includes(item) ? list.filter((x) => x !== item) : [...list, item];
  return (
    <span className="dw-agent-scope" data-testid={`dw-agent-scope-${stepId}`}>
      <span className="detail">
        {skills.length
          ? `limited to ${skills.length} skill${skills.length === 1 ? "" : "s"}`
          : "may use every skill its agent holds"}
        {mcp.length ? ` · reaches only ${mcp.join(", ")}`
          : " · may use every MCP server its agent holds"}
      </span>
      {available.length ? (
        <span className="dw-agent-scope-list" role="group" aria-label={`Skills for ${stepId}`}>
          {available.map((skill) => (
            <label key={skill} className="dw-agent-scope-item">
              <input type="checkbox" checked={skills.includes(skill)}
                     data-testid={`dw-scope-skill-${stepId}-${skill}`}
                     onChange={() => onChange({ skills: toggle(skills, skill), mcp })} />
              {skill}
            </label>
          ))}
        </span>
      ) : null}
      {servers.length ? (
        <span className="dw-agent-scope-list" role="group" aria-label={`MCP servers for ${stepId}`}>
          {servers.map((server) => (
            <label key={server.id} className="dw-agent-scope-item">
              <input type="checkbox" checked={mcp.includes(server.name)}
                     data-testid={`dw-scope-mcp-${stepId}-${server.name}`}
                     onChange={() => onChange({ skills, mcp: toggle(mcp, server.name) })} />
              MCP · {server.name}
            </label>
          ))}
        </span>
      ) : null}
    </span>
  );
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
