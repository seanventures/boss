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
  /** KG P6b: a contract stage's captain can be a connected outside agent instead of an
   *  in-process one. Set only when `agent` names one. */
  captain_kind?: "agent" | "outside_agent";
  /** The outside agent's endpoint, host only — never its auth secret. */
  endpoint_host?: string;
  /** What an `external_agent` step must read before it runs and must write once it has
   *  (the shape `/v1/delivery-workflows` checks a captain's binding against). */
  contract?: { reads?: { kind: string }[]; writes?: { kind: string; required?: boolean }[] };
};

type PipelineSpec = {
  key: string;
  version: number;
  steps: Stage[];
};

/** What one stage actually read and wrote on its last run (KG P6b: the operator's "after a run,
 *  show me what it touched" ask) — keyed by stage id, from the run's own trace. */
export type StageTrace = Record<string, { reads?: string[]; writes?: string[] }>;

function contractLine(contract?: Stage["contract"]): string | null {
  if (!contract) return null;
  const reads = (contract.reads ?? []).map((r) => r.kind);
  const writes = (contract.writes ?? []).map((w) => w.kind);
  if (!reads.length && !writes.length) return null;
  const left = reads.length ? reads.join(", ") : "nothing";
  const right = writes.length ? writes.join(", ") : "nothing";
  return `reads ${left} · writes ${right}`;
}

/** A connected outside agent the captain switcher can offer (KG P6b console). */
export type OutsideAgentOption = { id: string; display_name: string; endpoint_host: string };

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

/** KG P6b: `Outside agent`, next to its endpoint host and its contract — never its auth
 *  secret. The contract is what the operator asked to see alongside the badge: what this
 *  stage must read before it runs and must write once it has. */
function OutsideAgentBadge({ host, contract }: { host?: string; contract?: Stage["contract"] }) {
  const line = contractLine(contract);
  return (
    <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
      <span
        data-testid="outside-agent-badge"
        title="Runs outside BOSS, over HTTP, under a per-run token"
        style={{
          fontSize: 10.5, fontWeight: 700, padding: "1px 7px", borderRadius: 999,
          color: "#8a3ab3", border: "1px solid #8a3ab355", background: "#8a3ab318",
          textTransform: "uppercase", letterSpacing: "0.03em",
        }}
      >
        Outside agent
      </span>
      {host ? <span data-testid="outside-agent-host" style={{ fontSize: 12, color: "#5b6570" }}>{host}</span> : null}
      {line ? <span data-testid="outside-agent-contract" style={{ fontSize: 11, color: "#8a8f98" }}>{line}</span> : null}
    </span>
  );
}

/** A contract stage's captain can be switched to a connected outside agent (KG P6b): the
 *  current captain plus every reachable outside agent, each offered by name. */
function CaptainSwitcher({
  stage,
  outsideAgents,
  onAssign,
}: {
  stage: Stage;
  outsideAgents: OutsideAgentOption[];
  onAssign: (stageId: string, choice: { kind: "agent" | "outside_agent"; value: string }) => void;
}) {
  const current = stage.captain_kind === "outside_agent" ? `outside:${stage.agent ?? ""}` : "agent";
  return (
    <select
      data-testid={`stage-captain-select-${stage.id}`}
      aria-label={`Captain for ${stage.id}`}
      value={current}
      onChange={(e) => {
        e.stopPropagation();
        const value = e.target.value;
        if (value.startsWith("outside:")) {
          onAssign(stage.id, { kind: "outside_agent", value: value.slice("outside:".length) });
        } else {
          onAssign(stage.id, { kind: "agent", value: stage.agent ?? "" });
        }
      }}
      onClick={(e) => e.stopPropagation()}
      style={{ fontSize: 12, border: "1px solid #d8dce1", borderRadius: 6, padding: "2px 4px" }}
    >
      <option value="agent">{stage.agent ?? "Auto"}</option>
      {outsideAgents.map((o) => (
        <option key={o.id} value={`outside:${o.id}`}>{o.display_name} (outside)</option>
      ))}
    </select>
  );
}

function StageNode({
  stage,
  onSelect,
  outsideAgents,
  onAssignCaptain,
}: {
  stage: Stage;
  onSelect?: (stage: Stage) => void;
  outsideAgents?: OutsideAgentOption[];
  onAssignCaptain?: (stageId: string, choice: { kind: "agent" | "outside_agent"; value: string }) => void;
}) {
  const extraTestIds: string[] = [];
  if (stage.kind === "bounded_loop") extraTestIds.push(`loop-${stage.id}`);
  if (stage.kind === "gate") extraTestIds.push(`gate-${stage.id}`);

  const statusLabel = stage.status ?? "unknown";
  const isOutside = stage.captain_kind === "outside_agent";
  const ariaLabel = [
    KIND_LABEL[stage.kind] ?? stage.kind,
    `status ${statusLabel}`,
    stage.agent ? `agent ${stage.agent}` : null,
    isOutside ? "outside agent" : null,
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
          {isOutside ? <OutsideAgentBadge host={stage.endpoint_host} contract={stage.contract} /> : null}
          {stage.kind === "bounded_loop" && stage.max_iterations ? (
            <span style={{ fontSize: 12, color: "#5b6570" }}>up to {stage.max_iterations}×</span>
          ) : null}
          <ReferenceMark reference={stage.reference} />
        </div>
        {onAssignCaptain && (outsideAgents?.length ?? 0) > 0 ? (
          <CaptainSwitcher stage={stage} outsideAgents={outsideAgents ?? []} onAssign={onAssignCaptain} />
        ) : null}
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
  outsideAgents,
  onAssignCaptain,
  trace,
}: {
  spec: PipelineSpec;
  onSelectStage?: (stage: Stage) => void;
  /** Connected outside agents the captain switcher can offer (KG P6b console). */
  outsideAgents?: OutsideAgentOption[];
  /** A contract stage's captain switched to an in-process agent or a connected outside one. */
  onAssignCaptain?: (stageId: string, choice: { kind: "agent" | "outside_agent"; value: string }) => void;
  /** What each stage actually read and wrote on its last run, from the run's own trace — shown
   *  once a stage has run (KG P6b: the operator's "after a run" ask). Absent before any run. */
  trace?: StageTrace;
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
            <StageNode key={stage.id} stage={stage} onSelect={onSelectStage}
                       outsideAgents={outsideAgents} onAssignCaptain={onAssignCaptain} />
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
                <StageNode key={stage.id} stage={stage} onSelect={onSelectStage}
                           outsideAgents={outsideAgents} onAssignCaptain={onAssignCaptain} />
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

      {trace && Object.keys(trace).length > 0 ? (
        <section>
          <div className="ct-block-title">Reads &amp; writes (last run)</div>
          <table data-testid="pipeline-trace-table" style={{ width: "100%", borderCollapse: "collapse", marginTop: 8 }}>
            <thead>
              <tr style={{ textAlign: "left", fontSize: 12, color: "#5b6570" }}>
                <th style={{ padding: "6px 8px" }}>Stage</th>
                <th style={{ padding: "6px 8px" }}>Read</th>
                <th style={{ padding: "6px 8px" }}>Wrote</th>
              </tr>
            </thead>
            <tbody>
              {steps.filter((stage) => trace[stage.id]).map((stage) => {
                const entry = trace[stage.id];
                return (
                  <tr key={stage.id} data-testid={`stage-trace-row-${stage.id}`} style={{ borderTop: "1px solid #eceff2" }}>
                    <td style={{ padding: "6px 8px", fontWeight: 600 }}>{KIND_LABEL[stage.kind] ?? stage.kind}</td>
                    <td style={{ padding: "6px 8px", color: "#5b6570" }}>{(entry.reads ?? []).join(", ") || "—"}</td>
                    <td style={{ padding: "6px 8px", color: "#5b6570" }}>{(entry.writes ?? []).join(", ") || "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
      ) : null}
    </div>
  );
}
