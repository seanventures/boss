import React from "react";

import { formatDateTime } from "../lib/datetime";
import type { ImprovementProgramItem } from "../lib/self-improvement";
import { createImprovementProgramAction } from "./actions";
import { SubmitButton } from "./submit-button";

const PROMOTE_BADGE =
  "Promote is an enterprise SIM step — Community records trials only.";

export function ImprovementProgramsPanel({
  programs,
  programsApiReachable,
  programsBlockedReason,
  projectId,
  actor,
}: {
  programs: ImprovementProgramItem[];
  programsApiReachable: boolean;
  programsBlockedReason?: string;
  projectId: string;
  actor: string;
}) {
  return (
    <>
      <h3>Improvement programs</h3>
      <p className="detail">
        A ledger of improvement trials kept by BOSS. Promoting one into a self-improvement
        proposal needs the Enterprise edition.
      </p>

      <form
        action={createImprovementProgramAction}
        className="composer compact"
        data-testid="improvement-program-create-form"
      >
        <input type="hidden" name="project_id" value={projectId} />
        <input type="hidden" name="actor" value={actor} />
        <label className="field">
          <span>Name</span>
          <input
            name="name"
            className="input"
            placeholder="routing-baseline"
            required
          />
        </label>
        <label className="field">
          <span>Objective</span>
          <textarea
            name="objective"
            rows={3}
            className="textarea"
            placeholder="Pin a QA baseline and record comparable trials."
            required
          />
        </label>
        <label className="field">
          <span>Benchmark</span>
          <input
            name="benchmark_id"
            className="input"
            defaultValue="bugbox"
            placeholder="bugbox"
          />
        </label>
        <label className="field">
          <span>Baseline evaluation</span>
          <input
            name="baseline_evaluation_id"
            className="input"
            placeholder="qa_eval_…"
            required
          />
        </label>
        <SubmitButton className="button secondary" type="submit">
          Create improvement program
        </SubmitButton>
      </form>

      {!programsApiReachable ? (
        <p className="detail">
          {programsBlockedReason ?? "Improvement programs API is unavailable."}
        </p>
      ) : programs.length === 0 ? (
        <p className="detail">
          No improvement programs recorded yet. Programs pin QA baselines and
          deterministic trial policy without requiring enterprise SIM routes.
        </p>
      ) : (
        <div className="feed-list">
          {programs.map((program) => (
            <div className="feed-row" key={program.id}>
              <div>
                <h4>{program.name}</h4>
                <p className="detail">
                  {program.status}
                  {program.updatedAt
                    ? ` / ${formatDateTime(program.updatedAt)}`
                    : ""}
                </p>
                <p className="detail">{program.objective}</p>
                {program.baselineEvaluationId ? (
                  <p className="detail">
                    Baseline: {program.baselineEvaluationId}
                  </p>
                ) : null}
              </div>
              <code className="mono">{program.id}</code>
            </div>
          ))}
        </div>
      )}

      <div
        className="button-stack"
        data-testid="improvement-program-promote-gate"
        style={{ marginTop: 12 }}
      >
        <span
          data-testid="improvement-program-promote-badge"
          style={{
            display: "inline-block",
            fontSize: 10.5,
            color: "#d29922",
            border: "1px solid #d29922",
            padding: "1px 8px",
            borderRadius: 999,
            marginBottom: 8,
          }}
        >
          {PROMOTE_BADGE}
        </span>
        <div className="button-stack">
          <button
            type="button"
            className="button secondary"
            disabled
            data-testid="improvement-program-promote"
          >
            Promote
          </button>
          <button
            type="button"
            className="button secondary"
            disabled
            data-testid="improvement-program-adopt-baseline"
          >
            Adopt baseline
          </button>
        </div>
      </div>
    </>
  );
}
