"use client";

import React from "react";

type Stage = {
  id: string;
  kind: string;
  lane?: string;
  body?: string;
  agent?: string;
  status?: string;
  reference?: string;
  max_iterations?: number;
};

type PipelineSpec = {
  key: string;
  version: number;
  steps: Stage[];
};

const KIND_LABEL: Record<string, string> = {
  for_each: "For each lane",
  bounded_loop: "Bounded loop",
  gate: "Human gate",
  custom_step: "Agent step",
};

function StatusPill({ status }: { status?: string }) {
  const s = status ?? "unknown";
  const color =
    s === "done" ? "#2f8f4e" : s === "running" ? "#b5820f" : s === "pending" ? "#8a3ab3" : "#5b6570";
  return (
    <span
      style={{
        fontSize: 11,
        fontWeight: 600,
        padding: "2px 8px",
        borderRadius: 999,
        color,
        border: `1px solid ${color}55`,
        background: `${color}18`,
        textTransform: "uppercase",
        letterSpacing: "0.04em",
      }}
    >
      {s}
    </span>
  );
}

function ReferenceMark({ reference }: { reference?: string }) {
  if (!reference) return null;
  return (
    <span
      data-reference={reference}
      title="Open the run record for this stage"
      aria-label="Open run record"
      style={{ cursor: "pointer", color: "#5b6570", fontSize: 12 }}
    >
      ⧉
    </span>
  );
}

function StageNode({
  stage,
  onSelect,
}: {
  stage: Stage;
  onSelect?: (stage: Stage) => void;
}) {
  const extraTestIds: string[] = [];
  if (stage.kind === "bounded_loop") extraTestIds.push(`loop-${stage.id}`);
  if (stage.kind === "gate") extraTestIds.push(`gate-${stage.id}`);

  const statusLabel = stage.status ?? "unknown";
  const ariaLabel = [
    KIND_LABEL[stage.kind] ?? stage.kind,
    `status ${statusLabel}`,
    stage.agent ? `agent ${stage.agent}` : null,
  ]
    .filter(Boolean)
    .join(", ");

  return (
    <div style={{ display: "grid", gap: 4 }}>
      <button
        type="button"
        data-testid={`stage-${stage.id}`}
        aria-label={ariaLabel}
        onClick={() => onSelect?.(stage)}
        style={{
          textAlign: "left",
          border: "1px solid #d8dce1",
          borderRadius: 10,
          padding: "10px 12px",
          background: "#fff",
          cursor: "pointer",
          display: "grid",
          gap: 4,
        }}
      >
        <div style={{ fontWeight: 600, fontSize: 13 }}>{KIND_LABEL[stage.kind] ?? stage.kind}</div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <StatusPill status={stage.status} />
          {stage.agent ? <span style={{ fontSize: 12, color: "#5b6570" }}>{stage.agent}</span> : null}
          {stage.kind === "bounded_loop" && stage.max_iterations ? (
            <span style={{ fontSize: 12, color: "#5b6570" }}>up to {stage.max_iterations}×</span>
          ) : null}
          <ReferenceMark reference={stage.reference} />
        </div>
      </button>
      {extraTestIds.map((id) => (
        <div key={id} data-testid={id} style={{ display: "none" }} />
      ))}
    </div>
  );
}

export function PipelineView({
  spec,
  onSelectStage,
}: {
  spec: PipelineSpec;
  onSelectStage?: (stage: Stage) => void;
}) {
  const steps = spec?.steps ?? [];
  const lanes = new Map<string, Stage[]>();
  const unlaned: Stage[] = [];
  for (const step of steps) {
    if (step.lane) {
      const bucket = lanes.get(step.lane) ?? [];
      bucket.push(step);
      lanes.set(step.lane, bucket);
    } else {
      unlaned.push(step);
    }
  }

  if (steps.length === 0) {
    return (
      <div data-area="pipeline-view" style={{ display: "grid", gap: 20 }}>
        <section>
          <div className="ct-block-title">{spec?.key ?? "Workflow"} · stage diagram</div>
          <p data-testid="pipeline-empty" role="status" style={{ color: "#5b6570", fontSize: 13, marginTop: 8 }}>
            This pipeline has no stages yet.
          </p>
        </section>
      </div>
    );
  }

  return (
    <div data-area="pipeline-view" style={{ display: "grid", gap: 20 }}>
      <section>
        <div className="ct-block-title">{spec?.key ?? "Workflow"} · stage diagram</div>
        <div style={{ display: "grid", gap: 12, marginTop: 8 }}>
          {unlaned.map((stage) => (
            <StageNode key={stage.id} stage={stage} onSelect={onSelectStage} />
          ))}
          {Array.from(lanes.entries()).map(([lane, stages]) => (
            <div
              key={lane}
              data-testid={`lane-${lane}`}
              style={{
                border: "1px dashed #b9c0cb",
                borderRadius: 10,
                padding: 12,
                display: "grid",
                gap: 10,
              }}
            >
              <div style={{ fontSize: 12, color: "#5b6570", fontWeight: 600 }}>Lane: {lane}</div>
              {stages.map((stage) => (
                <StageNode key={stage.id} stage={stage} onSelect={onSelectStage} />
              ))}
            </div>
          ))}
        </div>
      </section>

      <section>
        <div className="ct-block-title">Stages</div>
        <table style={{ width: "100%", borderCollapse: "collapse", marginTop: 8 }}>
          <thead>
            <tr style={{ textAlign: "left", fontSize: 12, color: "#5b6570" }}>
              <th style={{ padding: "6px 8px" }}>Stage</th>
              <th style={{ padding: "6px 8px" }}>What it does</th>
              <th style={{ padding: "6px 8px" }}>Status</th>
              <th style={{ padding: "6px 8px" }}>Reference</th>
            </tr>
          </thead>
          <tbody>
            {steps.map((stage) => (
              <tr
                key={stage.id}
                data-testid={`stage-row-${stage.id}`}
                style={{ borderTop: "1px solid #eceff2" }}
              >
                <td style={{ padding: "6px 8px", fontWeight: 600 }}>
                  {KIND_LABEL[stage.kind] ?? stage.kind}
                </td>
                <td style={{ padding: "6px 8px", color: "#5b6570" }}>{stage.agent ?? "—"}</td>
                <td style={{ padding: "6px 8px" }}>
                  <StatusPill status={stage.status} />
                </td>
                <td style={{ padding: "6px 8px" }}>
                  <ReferenceMark reference={stage.reference} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
