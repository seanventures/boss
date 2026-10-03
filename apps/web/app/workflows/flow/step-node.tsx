"use client";

/**
 * One step, drawn the same way in the editor, on a running workflow and on a library card.
 *
 * Three rows and no sentences: what it is (icon and title), who does it (the agent), and either
 * what it is set up with (design) or where it has got to (run). Every explanation is a
 * `data-tip`, so the node stays a glance and the reason is one hover away.
 *
 * The title is never the raw step id when a label exists: `stepView` resolves the step's own
 * title, then its custom-step or built-in label.
 */
import { Check, HandPalm, HardDrives, Lock, LockSimple, Minus, Plug, Sparkle, User, Warning, X } from "@phosphor-icons/react";
import React from "react";

import type { StepView } from "../../../lib/workflow-editor-types";
import { agentMeta } from "../../agent-chip";
import { NodeIcon } from "./type-icons";

export type RunPhase = "done" | "moving" | "needs_you" | "failed" | "future" | "skipped";

/** The names behind the badge counts, when the caller knows them. Only feeds tooltips. */
export type NodeDetail = { risk?: number; skills?: string[]; mcp?: string[]; duration?: string };

/** An agent's face: initials for a named agent, a glyph for Auto, You and A person. Sized by
 *  CSS where it sits (a node, a summary chip), or by `size` when a caller needs another. */
export function AgentAvatar({ agent, size }: { agent: StepView["agent"]; size?: number }) {
  const style = size ? { width: size, height: size } : undefined;
  if (agent.kind === "named") {
    const meta = agentMeta(agent.role_key ?? agent.label, agent.label);
    return <span className={`mr-avatar tone-${meta.tone} wf-avatar`} style={style} aria-hidden>{meta.initials}</span>;
  }
  const Icon = agent.kind === "auto" ? Sparkle : agent.kind === "person" ? HandPalm : User;
  return <span className="wf-avatar wf-avatar-glyph" data-kind={agent.kind} style={style} aria-hidden>
    <Icon size={11} weight="bold" />
  </span>;
}

/** How a run phase is said to a screen reader, and marked on the icon so it is never colour alone. */
const PHASE_WORDS: Record<RunPhase, string> = {
  done: "Done", moving: "In progress", needs_you: "Needs you", failed: "Failed", future: "Not started",
  skipped: "Skipped",
};
const PHASE_MARK: Partial<Record<RunPhase, React.ComponentType<{ size?: number; weight?: "bold" | "fill" }>>> = {
  done: Check, needs_you: HandPalm, failed: X, skipped: Minus,
};

/** Flow blocks the runtime steps through itself: no agent runs them, so no agent row. */
const BLOCK_TYPES = new Set<StepView["type"]>(["parallel", "condition", "join", "loop"]);

function plural(n: number, one: string, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

export function StepNode(p: {
  view: StepView; mode: "design" | "run" | "dot"; selected?: boolean; problem?: string;
  run?: { phase: RunPhase; meta: string; tip?: string };
  onSelect?(): void;
  /** Optional names for the badge tooltips. */
  detail?: NodeDetail;
}): React.JSX.Element {
  const { view, mode, selected, problem, run, onSelect, detail } = p;

  if (mode === "dot") {
    return (
      <span className="wf-dot-node" data-type={view.type} data-tip={view.title} role="img"
            aria-label={view.title} data-testid="wf-dot-node">
        <NodeIcon icon={view.icon} type={view.type} size={11} />
      </span>
    );
  }

  const b = view.badges;
  const unwired = b.unwired.length;
  const phase = run?.phase ?? "future";
  const Mark = mode === "run" ? PHASE_MARK[phase] : undefined;
  return (
    <button type="button" className="wf-node" data-mode={mode} data-type={view.type}
            data-phase={mode === "run" ? phase : undefined}
            data-selected={selected ? "true" : undefined}
            data-problem={problem ? "true" : undefined}
            data-step-id={view.id} data-testid="wf-node"
            aria-pressed={mode === "design" ? !!selected : undefined}
            aria-current={mode === "run" && run?.phase === "moving" ? "step" : undefined}
            onClick={onSelect}>
      <span className="wf-node-head">
        <span className="wf-node-icon" data-type={view.type}>
          <NodeIcon icon={view.icon} type={view.type} />
          {/* A run's outcome is marked on the icon too: "this one failed" must never be read
              from a colour alone, or from a glyph that only says what kind of step it is. */}
          {Mark ? <span className="wf-node-mark" data-phase={phase} data-testid="wf-node-mark"><Mark size={9} weight="bold" /></span> : null}
        </span>
        <span className="wf-node-title">{view.title}</span>
        {mode === "run" ? <span className="wf-sr">{PHASE_WORDS[phase]}</span> : null}
      </span>
      {BLOCK_TYPES.has(view.type) ? null : view.type === "wait" ? (
        <span className="wf-node-agent wf-node-wait" data-testid="wf-node-wait">{detail?.duration ?? ""}</span>
      ) : (
        <span className="wf-node-agent" data-kind={view.agent.kind} data-tip={view.agent.tip || undefined}
              data-testid="wf-node-agent">
          <AgentAvatar agent={view.agent} />
          <span className="wf-node-agent-name">{view.agent.label}</span>
        </span>
      )}
      {mode === "design" ? (
        <span className="wf-node-badges">
          {b.approval ? (
            <span className="wf-badge" data-kind={b.forced ? "forced" : "approval"} data-testid="wf-badge-approval"
                  data-tip={b.forced
                    ? `${detail?.risk != null ? `Risk ${detail.risk}` : "Risk"} — always needs a person.`
                    : "Needs approval before it continues."}
                  aria-label={b.forced ? "Always needs a person" : "Needs approval"}>
              {b.forced ? <LockSimple size={12} weight="fill" aria-hidden /> : <Lock size={12} weight="bold" aria-hidden />}
            </span>
          ) : null}
          {b.inputs > 0 ? (
            <span className="wf-badge" data-kind={unwired ? "unwired" : "inputs"} data-testid="wf-badge-inputs"
                  data-tip={unwired
                    ? `${plural(b.inputs, "input")} · ${unwired} not connected`
                    : plural(b.inputs, "input")}
                  aria-label={unwired ? `${b.inputs} inputs, ${unwired} not connected` : `${b.inputs} inputs`}>
              <Plug size={12} weight="bold" aria-hidden /><span className="wf-badge-n">{b.inputs}</span>
            </span>
          ) : null}
          {b.skills > 0 ? (
            <span className="wf-badge" data-kind="skills" data-testid="wf-badge-skills"
                  data-tip={detail?.skills?.length ? `Limited to: ${detail.skills.join(", ")}` : `Limited to ${plural(b.skills, "skill")}`}
                  aria-label={`${b.skills} skills`}>
              <Sparkle size={12} weight="bold" aria-hidden /><span className="wf-badge-n">{b.skills}</span>
            </span>
          ) : null}
          {b.mcp > 0 ? (
            <span className="wf-badge" data-kind="mcp" data-testid="wf-badge-mcp"
                  data-tip={detail?.mcp?.length ? detail.mcp.join(", ") : plural(b.mcp, "MCP server")}
                  aria-label={`${b.mcp} MCP`}>
              <HardDrives size={12} weight="bold" aria-hidden /><span className="wf-badge-n">{b.mcp}</span>
            </span>
          ) : null}
          {b.lacks.length ? (
            <span className="wf-badge" data-kind="lacks" data-testid="wf-badge-lacks"
                  data-tip={`${view.agent.label} lacks ${b.lacks.join(", ")}`}
                  aria-label={`${view.agent.label} lacks ${b.lacks.join(", ")}`}>
              <Warning size={12} weight="fill" aria-hidden />
            </span>
          ) : null}
        </span>
      ) : null}
      {mode === "run" ? (
        <span className="wf-node-status" data-phase={phase} data-testid="wf-node-status"
              data-tip={run?.tip || undefined}>
          {run?.meta ?? ""}
        </span>
      ) : null}
      {problem ? (
        <span className="wf-node-problem" data-tip={problem} data-testid="wf-node-problem"
              role="img" aria-label={problem} />
      ) : null}
    </button>
  );
}
