"use client";

/**
 * THE PIPELINE VIEW (Workflow editor › Pipeline, design §4.7): the workflow as the numbered
 * stage diagram a consultant draws. Each stage says who runs it, what it reads and writes, and
 * how it went on the last run; a loop is drawn as the frame of the stages it repeats, a lane as
 * the stage it runs once per item. Click a stage for its read/write view.
 *
 * Every word comes from the spec, the palette and the last run (lib/pipeline-model.ts): no ids,
 * no sample data.
 */
import React from "react";

import {
  contractSummary, kindLabel, STATUS_LABEL, type PipelineStage, type StageStatus,
} from "../../lib/pipeline-model";
import { localWith } from "../../lib/local-time";
import type { RunContext } from "../../lib/workflow-api";
import { AgentContractView } from "./editor/agent-contract-view";

/** A connected outside agent the captain switcher can offer (KG P6b console). */
export type OutsideAgentOption = { id: string; display_name: string; endpoint_host: string };

type Assign = (stageId: string, choice: { kind: "agent" | "outside_agent"; value: string }) => void;

export function StatusPill({ status }: { status: StageStatus }) {
  return (
    <span className="pl-status" data-status={status} data-testid="stage-status">
      {STATUS_LABEL[status]}
    </span>
  );
}

function RunsAsChip({ stage }: { stage: PipelineStage }) {
  return (
    <span className="pl-runs-as" data-tone={stage.runsAs.tone} title={stage.runsAs.tip}
          data-testid={`stage-runs-as-${stage.id}`}>
      {stage.runsAs.label}
    </span>
  );
}

/** KG P6b: an outside agent's endpoint host — never its auth secret. Its chip already says
 *  "Outside agent · <name>", and its contract reads like every other card's. */
function OutsideAgentHost({ stage }: { stage: PipelineStage }) {
  if (!stage.endpointHost) return null;
  return (
    <span data-testid="outside-agent-host" className="pl-faint pl-host"
          title="Runs outside BOSS, over HTTP, under a per-run token">{stage.endpointHost}</span>
  );
}

/** A stage's captain can be switched to a connected outside agent (KG P6b). */
function CaptainSwitcher({ stage, outsideAgents, onAssign }: {
  stage: PipelineStage; outsideAgents: OutsideAgentOption[]; onAssign: Assign;
}) {
  const current = stage.captainKind === "outside_agent" ? `outside:${stage.agent ?? ""}` : "agent";
  return (
    <select
      className="pl-captain"
      data-testid={`stage-captain-select-${stage.id}`}
      aria-label={`Who runs ${stage.title}`}
      value={current}
      onChange={(e) => {
        e.stopPropagation();
        const value = e.target.value;
        if (value.startsWith("outside:")) onAssign(stage.id, { kind: "outside_agent", value: value.slice("outside:".length) });
        else onAssign(stage.id, { kind: "agent", value: stage.agent ?? "" });
      }}
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => e.stopPropagation()}
    >
      <option value="agent">{stage.captainKind === "outside_agent" ? "BOSS agent" : stage.runsAs.label}</option>
      {outsideAgents.map((o) => (
        <option key={o.id} value={`outside:${o.id}`}>{o.display_name} (outside)</option>
      ))}
    </select>
  );
}

function StageCard({ stage, selected, onSelect, outsideAgents, onAssignCaptain }: {
  stage: PipelineStage; selected: boolean; onSelect: (id: string) => void;
  outsideAgents: OutsideAgentOption[]; onAssignCaptain?: Assign;
}) {
  const summary = contractSummary(stage.contract);
  // Who runs it can be switched on the stage in hand — one control on screen, not one per card.
  const switchable = selected && !!onAssignCaptain && outsideAgents.length > 0
    && !["decision", "for_each", "bounded_loop", "parallel", "condition", "join"].includes(stage.kind);
  return (
    <div
      role="button"
      tabIndex={0}
      className="pl-card"
      data-testid={`stage-${stage.id}`}
      data-selected={selected ? "true" : undefined}
      data-tone={stage.runsAs.tone}
      aria-pressed={selected}
      aria-label={`Stage ${stage.number ?? ""} ${stage.title}, ${stage.runsAs.label}, ${STATUS_LABEL[stage.status]}`}
      onClick={() => onSelect(stage.id)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(stage.id); }
      }}
    >
      <div className="pl-card-top">
        <span className="pl-num" aria-hidden>{stage.number}</span>
        <StatusPill status={stage.status} />
      </div>
      <div className="pl-card-title">{stage.title}</div>
      <RunsAsChip stage={stage} />
      {stage.captainKind === "outside_agent" ? <OutsideAgentHost stage={stage} /> : null}
      <div className="pl-card-contract" data-testid={`stage-contract-${stage.id}`}
           data-empty={summary ? undefined : "true"}
           title={stage.contract ? contractTip(stage.contract) : undefined}>
        {summary ?? "No contract declared"}
      </div>
      {switchable ? <CaptainSwitcher stage={stage} outsideAgents={outsideAgents} onAssign={onAssignCaptain!} /> : null}
    </div>
  );
}

/** The kinds behind "reads 3 · writes 2", for the hover. */
function contractTip(c: NonNullable<PipelineStage["contract"]>): string {
  return [
    c.reads.length ? `Reads ${c.reads.map((r) => kindLabel(r.kind)).join(", ")}` : "",
    c.writes.length ? `Writes ${c.writes.map((w) => kindLabel(w.kind)).join(", ")}` : "",
    c.calls.length ? `Calls ${c.calls.map((x) => x.tool || x.mcp).join(", ")}` : "",
  ].filter(Boolean).join(" · ");
}

function Arrow() {
  return <span className="pl-arrow" aria-hidden>→</span>;
}

/** A loop or lane frame: its own words, then the stages it holds. */
function Frame({ frame, members, children }: {
  frame: PipelineStage; members: PipelineStage[]; children: React.ReactNode;
}) {
  const loop = frame.kind === "bounded_loop";
  return (
    <div className="pl-frame" data-kind={frame.kind}
         data-testid={loop ? `loop-${frame.id}` : `lane-${frame.id}`}
         aria-label={`${frame.runsAs.label}: ${frame.frameNote ?? ""}`}>
      <div className="pl-frame-head">
        <span className="pl-frame-icon" aria-hidden>{loop ? "↻" : "⇉"}</span>
        <span className="pl-frame-label">{frame.runsAs.label}</span>
        <StatusPill status={frame.status} />
      </div>
      {frame.frameNote ? <div className="pl-frame-note">{frame.frameNote}</div> : null}
      <div className="pl-frame-body" data-lanes={loop ? undefined : "true"}>
        {children}
        {loop && members.length ? (
          <svg className="pl-loop-back" viewBox="0 0 100 20" preserveAspectRatio="none" aria-hidden
               data-testid={`loop-back-${frame.id}`}>
            <path d="M 96 2 L 96 12 Q 96 18 90 18 L 10 18 Q 4 18 4 12 L 4 4" />
            <path d="M 1 7 L 4 2 L 7 7" />
          </svg>
        ) : null}
      </div>
    </div>
  );
}

function formatWhen(iso: string | null): string {
  return localWith(iso, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

const RUN_WORD: Record<string, string> = {
  completed: "Completed", succeeded: "Completed", waiting: "Waiting for a person", running: "Running",
  queued: "Queued", failed: "Failed", cancelled: "Cancelled", paused: "Paused", blocked: "Blocked",
};

export function PipelineView({
  name, stages, run, outsideAgents = [], onAssignCaptain, selectedId, onSelectStage,
}: {
  /** The workflow's own name. */
  name: string;
  stages: PipelineStage[];
  /** The last run's trace; null before any run. */
  run?: RunContext | null;
  /** Connected outside agents the captain switcher can offer (KG P6b console). */
  outsideAgents?: OutsideAgentOption[];
  onAssignCaptain?: Assign;
  selectedId?: string | null;
  onSelectStage?: (id: string | null) => void;
}) {
  const [ownSelection, setOwnSelection] = React.useState<string | null>(null);
  const selected = selectedId !== undefined ? selectedId : ownSelection;
  const select = (id: string | null) => {
    if (onSelectStage) onSelectStage(id);
    if (selectedId === undefined) setOwnSelection(id);
  };
  const toggle = (id: string) => select(selected === id ? null : id);
  const detailRef = React.useRef<HTMLDivElement>(null);
  React.useEffect(() => {
    if (selected) detailRef.current?.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
  }, [selected]);

  if (stages.length === 0) {
    return (
      <div data-area="pipeline-view" className="pl-view">
        <div className="pl-head"><h2 className="pl-title">{name}</h2></div>
        <p data-testid="pipeline-empty" role="status" className="pl-empty">
          This pipeline has no stages yet. Add steps on the Visual tab and they appear here, numbered.
        </p>
      </div>
    );
  }

  const byId = new Map(stages.map((s) => [s.id, s]));
  const top = stages.filter((s) => !s.group);
  const selectedStage = selected ? byId.get(selected) : undefined;
  const hasRun = !!run;

  const card = (s: PipelineStage) => (
    <StageCard key={s.id} stage={s} selected={selected === s.id} onSelect={toggle}
               outsideAgents={outsideAgents} onAssignCaptain={onAssignCaptain} />
  );

  return (
    <div data-area="pipeline-view" className="pl-view">
      <div className="pl-head">
        <h2 className="pl-title">{name}</h2>
        <span className="pl-run-line" data-testid="pipeline-last-run">
          {run
            ? `Last run · ${RUN_WORD[run.status] ?? STATUS_LABEL.pending}${run.started_at ? ` · started ${formatWhen(run.started_at)}` : ""}`
            : "Not run yet: statuses appear after the first run"}
        </span>
      </div>

      <section className="pl-diagram" aria-label="Stage diagram" data-testid="pipeline-diagram">
        {top.map((s, i) => {
          const members = (s.members ?? []).map((id) => byId.get(id)).filter((m): m is PipelineStage => !!m);
          return (
            <React.Fragment key={s.id}>
              {i > 0 ? <Arrow /> : null}
              {s.members ? (
                <Frame frame={s} members={members}>
                  {members.map((m, j) => (
                    <React.Fragment key={m.id}>
                      {j > 0 ? <Arrow /> : null}
                      {card(m)}
                    </React.Fragment>
                  ))}
                  {!members.length ? <span className="pl-faint">No stage inside yet</span> : null}
                </Frame>
              ) : card(s)}
            </React.Fragment>
          );
        })}
      </section>
      <p className="pl-hint">Click a stage to see what it reads from the Knowledge Graph, what it writes to the Context Graph, and which outside systems it calls.</p>

      {selectedStage ? (
        <div ref={detailRef} className="pl-detail" data-testid="pipeline-stage-detail">
          <AgentContractView key={selectedStage.id} stage={selectedStage} hasRun={hasRun} onClose={() => select(null)} />
        </div>
      ) : null}

      <section aria-label="Stages">
        <div className="pl-section-title">Stages</div>
        <div className="pl-table-wrap">
          <table className="pl-table" data-testid="pipeline-stage-table">
            <thead>
              <tr><th>#</th><th>Stage</th><th>What it does</th><th>Runs as</th><th>Status</th></tr>
            </thead>
            <tbody>
              {stages.map((s) => (
                <tr key={s.id} data-testid={`stage-row-${s.id}`} data-member={s.group ? "true" : undefined}
                    data-selected={selected === s.id ? "true" : undefined}
                    onClick={() => { if (!s.members) toggle(s.id); }}
                    className={s.members ? "pl-row-frame" : "pl-row"}>
                  <td className="pl-cell-num">{s.number ?? (s.kind === "bounded_loop" ? "↻" : s.kind === "for_each" ? "⇉" : "")}</td>
                  <td className="pl-cell-stage">{s.members ? s.runsAs.label : s.title}</td>
                  <td className="pl-cell-what">{s.description}</td>
                  <td><RunsAsChip stage={s} /></td>
                  <td><StatusPill status={s.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
