import type {
  WorkflowDefinitionSummary,
  WorkflowMappingSummary,
  WorkflowRunDetail,
  WorkflowRunSummary,
} from "@boss/api-client";
import React from "react";

function formatTime(value?: string): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat("en", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
}

function tone(status: string): string {
  if (["completed", "succeeded", "published", "active"].includes(status)) return "green";
  if (["failed", "deadlettered", "blocked", "cancelled"].includes(status)) return "red";
  if (["waiting", "paused", "compensating"].includes(status)) return "amber";
  return "blue";
}

export function WorkflowRegistryView({
  apiReachable,
  blockedReason,
  definitions,
  mappings,
  runs,
  selectedRun,
}: {
  apiReachable: boolean;
  blockedReason?: string;
  definitions: WorkflowDefinitionSummary[];
  mappings: WorkflowMappingSummary[];
  runs: WorkflowRunSummary[];
  selectedRun?: WorkflowRunDetail;
}) {
  if (!apiReachable) {
    return (
      <article className="stack-card">
        <h3>Workflow runtime unavailable</h3>
        <p className="detail">{blockedReason ?? "The workflow control-plane feed could not be loaded."}</p>
      </article>
    );
  }

  return (
    <div className="stack-grid workflow-registry-grid">
      <article className="stack-card">
        <div className="card-heading-row">
          <div>
            <span className="eyebrow">Registry</span>
            <h3>Reusable workflows</h3>
          </div>
          <span className="status-meta">{definitions.length} definitions</span>
        </div>
        {definitions.length === 0 ? (
          <p className="detail">No workflow definitions have been published for this project.</p>
        ) : (
          <div className="workflow-registry-list">
            {definitions.slice(0, 8).map((definition) => (
              <div className="workflow-registry-row" key={definition.id}>
                <div>
                  <strong>{definition.name}</strong>
                  <p className="detail mono-soft">{definition.workflow_key}</p>
                </div>
                <div className="workflow-registry-meta">
                  <span className={`badge tone-${tone(definition.status)}`}>{definition.status}</span>
                  <span className="detail">{definition.version_count ?? 0} versions</span>
                </div>
              </div>
            ))}
          </div>
        )}
        <div className="workflow-registry-mapping-summary">
          <strong>{mappings.length}</strong>
          <span className="detail">deterministic work-class mappings</span>
        </div>
      </article>

      <article className="stack-card">
        <div className="card-heading-row">
          <div>
            <span className="eyebrow">Runs</span>
            <h3>Durable execution</h3>
          </div>
          <span className="status-meta">{runs.length} recent</span>
        </div>
        {runs.length === 0 ? (
          <p className="detail">No mapped workflows have run yet.</p>
        ) : (
          <div className="workflow-registry-list">
            {runs.slice(0, 8).map((run) => (
              <div className="workflow-registry-row" key={run.id}>
                <div>
                  <strong>{run.workflow_name}</strong>
                  <p className="detail">v{run.version} · {formatTime(run.updated_at)}</p>
                </div>
                <div className="workflow-registry-meta">
                  <span className={`badge tone-${tone(run.status)}`}>{run.status}</span>
                  <span className="detail">{run.current_frontier.length} frontier</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </article>

      <article className="stack-card workflow-run-timeline">
        <div className="card-heading-row">
          <div>
            <span className="eyebrow">Pinned run</span>
            <h3>{selectedRun ? selectedRun.workflow_name : "Run timeline"}</h3>
          </div>
          {selectedRun ? (
            <span className={`badge tone-${tone(selectedRun.status)}`}>{selectedRun.status}</span>
          ) : null}
        </div>
        {!selectedRun ? (
          <p className="detail">Start a workflow to see its immutable version, frontier, steps, and audit timeline.</p>
        ) : (
          <>
            <p className="detail mono-soft">
              version {selectedRun.version} · {selectedRun.workflow_version_id}
            </p>
            <div className="workflow-step-strip" aria-label="Workflow step status">
              {selectedRun.steps.map((step) => (
                <div className="workflow-step-node" key={`${step.id}:${step.iteration}`}>
                  <span className={`workflow-step-dot tone-${tone(step.status)}`} />
                  <span>
                    <strong>{step.step_id}</strong>
                    <small>{step.step_kind} · {step.status}{step.iteration ? ` · loop ${step.iteration}` : ""}</small>
                  </span>
                </div>
              ))}
            </div>
            <div className="workflow-event-timeline">
              {selectedRun.timeline.slice(0, 10).map((event) => (
                <div key={event.id} className="workflow-event-row">
                  <span className="workflow-event-marker" />
                  <span>
                    <strong>{event.event_type.replaceAll("_", " ")}</strong>
                    <small>{event.actor} · {formatTime(event.created_at)}</small>
                  </span>
                </div>
              ))}
            </div>
          </>
        )}
      </article>
    </div>
  );
}
