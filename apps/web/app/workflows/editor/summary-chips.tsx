"use client";

/**
 * The workflow at a glance, on one line: steps, agents, approvals, time, start-form inputs,
 * settings and issues. Icon and number only; the detail is each chip's tooltip.
 *
 * Read from the draft (and the last check's summary), never from the published version, so the
 * line always describes what Save would publish.
 */
import { CheckCircle, CircleDashed, Clock, Gear, Lock, Stack, Textbox, WarningCircle } from "@phosphor-icons/react";
import React, { useMemo } from "react";

import type { EditorState, StepView } from "../../../lib/workflow-editor-types";
import { workflowInputFields } from "../../../lib/workflow-inputs";
import { AgentAvatar } from "../flow/step-node";

const MAX_AVATARS = 3;

/** "~45m", "~2h", "~1d". */
export function formatMinutes(total: number | null | undefined): string {
  if (!total || total <= 0) return "";
  if (total < 60) return `~${Math.round(total)}m`;
  if (total < 60 * 24) return `~${Math.round(total / 60)}h`;
  return `~${Math.round(total / (60 * 24))}d`;
}

function count(n: number, one: string) {
  return `${n} ${n === 1 ? one : `${one}s`}`;
}

export function SummaryChips({ state, views, typicalMinutes, onSettings, onIssues }: {
  state: EditorState;
  views: Map<string, StepView>;
  /** The draft's own estimate, used until a check has given one. */
  typicalMinutes?: number | null;
  onSettings(): void;
  onIssues(): void;
}) {
  const steps = state.spec.steps;
  const agents = useMemo(() => {
    const seen = new Map<string, StepView["agent"]>();
    for (const v of views.values()) {
      if (v.agent.kind === "named" && v.agent.role_key && !seen.has(v.agent.role_key)) seen.set(v.agent.role_key, v.agent);
    }
    for (const key of state.summary?.agents ?? []) {
      if (!seen.has(key)) seen.set(key, { kind: "named", role_key: key,
        label: key.startsWith("outside:") ? "Outside agent" : key.replace(/_/g, " "), tip: "" });
    }
    return [...seen.values()];
  }, [views, state.summary]);
  const approvals = steps.map((s) => views.get(s.id)).filter((v): v is StepView => !!v && v.badges.approval);
  const fields = workflowInputFields(state.spec.input_schema);
  const minutes = state.summary?.typical_minutes ?? typicalMinutes ?? null;
  const issues = state.problems.length;
  // READY ONLY FOR THE DRAFT ON SCREEN. A check answers for the spec it was sent; after any
  // edit that answer is about an older draft, and a green Ready would vouch for steps nobody
  // has checked. The auto-check re-checks the new draft and this turns green (or red) again.
  const fresh = Boolean(state.summary) && state.checkedSpec === state.spec;

  return (
    <div className="wf-summary" role="group" aria-label="Summary" data-testid="wf-summary">
      <span className="wf-chip" data-tip="Steps in this workflow" data-testid="wf-chip-steps">
        <Stack size={14} weight="bold" aria-hidden />{count(steps.length, "step")}
      </span>
      {agents.length ? (
        <span className="wf-chip" data-tip={agents.map((a) => a.label).join(", ")} data-testid="wf-chip-agents">
          <span className="wf-avatars" aria-hidden>
            {agents.slice(0, MAX_AVATARS).map((a) => <AgentAvatar key={a.role_key} agent={a} />)}
          </span>
          {count(agents.length, "agent")}
        </span>
      ) : null}
      {approvals.length ? (
        <span className="wf-chip" data-tip={approvals.map((v) => v.title).join(", ")} data-testid="wf-chip-approvals">
          <Lock size={14} weight="bold" aria-hidden />{count(approvals.length, "approval")}
        </span>
      ) : null}
      {formatMinutes(minutes) ? (
        <span className="wf-chip" data-tip="Typical time, from past runs" data-testid="wf-chip-time">
          <Clock size={14} weight="bold" aria-hidden />{formatMinutes(minutes)}
        </span>
      ) : null}
      {fields.length ? (
        <span className="wf-chip" data-tip="Asked for when someone starts a run" data-testid="wf-chip-inputs">
          <Textbox size={14} weight="bold" aria-hidden />{count(fields.length, "input")}
        </span>
      ) : null}
      <button type="button" className="wf-chip wf-chip-btn" onClick={onSettings} data-testid="wf-chip-settings"
              data-tip="Name, start form and schedule">
        <Gear size={14} weight="bold" aria-hidden />Settings
      </button>
      {issues ? (
        <button type="button" className="wf-chip wf-chip-btn" data-tone="red" onClick={onIssues}
                data-tip={state.problems[0]?.message} data-testid="wf-chip-issues">
          <WarningCircle size={14} weight="fill" aria-hidden />{count(issues, "issue")}
        </button>
      ) : fresh ? (
        <span className="wf-chip" data-tone="green" data-tip="Every check passes" data-testid="wf-chip-ready">
          <CheckCircle size={14} weight="fill" aria-hidden />Ready
        </span>
      ) : steps.length ? (
        <span className="wf-chip" data-tip="Changed since the last check. Checking…"
              data-testid="wf-chip-unchecked">
          <CircleDashed size={14} weight="bold" aria-hidden />Not checked
        </span>
      ) : null}
    </div>
  );
}
