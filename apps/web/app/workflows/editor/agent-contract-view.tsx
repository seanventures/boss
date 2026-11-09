"use client";

/**
 * THE AGENT READ/WRITE VIEW (design §4.7.2): one stage, in three bands , 
 *
 *   Knowledge Graph (read only)   the kinds its contract reads          grey lines
 *   the stage and who runs it     + outside systems it calls over MCP   dotted lines
 *   Context Graph (read & write)  the kinds it writes, required marked  blue lines
 *
 * Click a node to highlight its line. "Show the last run" overlays what the last run really
 * read and wrote (the graph's own record, `view=context_trace`): a declared kind it never
 * touched is marked "declared, not done", a kind it touched without declaring "done, not
 * declared". Nothing is drawn that the contract or the run does not say.
 */
import React, { useMemo, useState } from "react";

import { GatedButton } from "../../../components/gated";

import {
  DESIGN_LABEL, kindKey, kindLabel, STATUS_LABEL, type ContractKind, type DesignNote, type DesignStatus,
  type KindMeta, type PipelineStage, type StageContract, type StageDesignState,
} from "../../../lib/pipeline-model";
import type { DesignAction, TracedKind } from "../../../lib/workflow-api";
import { AgentStepsMap, bandTitle } from "./agent-steps-map";

type Side = "read" | "write";
type Overlay = "declared" | "done" | "missing" | "undeclared";

type GraphNode = {
  key: string; side: Side; kind: string; label: string; sub: string;
  required: boolean; overlay: Overlay; traced?: TracedKind; declared?: ContractKind;
};
type CallNode = { key: string; label: string; sub: string; made: number | null; missing: boolean };

const W = 920;
const NODE_W = 140;
const NODE_H = 46;
const GAP = 12;
const PER_ROW = 6;
const STEP_W = 260;
const STEP_H = 70;
const CALL_W = 210;
const CALL_H = 40;

function nodeSub(n: { side: Side; required: boolean; overlay: Overlay; traced?: TracedKind; declared?: ContractKind }): string {
  if (n.overlay === "missing") return n.required ? "required, not done" : "declared, not done";
  if (n.overlay === "undeclared") return `done, not declared, ${n.traced?.count ?? 0}`;
  if (n.overlay === "done") return `${n.side === "read" ? "read" : "wrote"} ${n.traced?.count ?? 0}`;
  const parts: string[] = [];
  // A required write is marked ● on its label; its decision type says more than "required".
  if (n.declared?.decision_type) parts.push(n.declared.decision_type);
  else if (n.required) parts.push("required");
  if (n.declared?.via) parts.push(`via ${kindLabel(n.declared.via).toLowerCase()}`);
  return parts.join(", ");
}

function graphNodes(side: Side, declared: ContractKind[], traced: TracedKind[] | null): GraphNode[] {
  const out: GraphNode[] = [];
  const done = new Map((traced ?? []).map((t) => [kindKey(t.kind), t]));
  const seen = new Set<string>();
  for (const d of declared) {
    const key = kindKey(d.kind);
    if (seen.has(key)) continue;
    seen.add(key);
    const t = done.get(key);
    const overlay: Overlay = traced ? (t ? "done" : "missing") : "declared";
    const node = { key, side, kind: d.kind, label: kindLabel(d.kind), required: !!d.required, overlay,
                   traced: t, declared: d, sub: "" };
    node.sub = nodeSub(node);
    out.push(node);
  }
  for (const t of traced ?? []) {
    const key = kindKey(t.kind);
    if (seen.has(key)) continue;
    seen.add(key);
    const node = { key, side, kind: t.kind, label: kindLabel(t.kind), required: false,
                   overlay: "undeclared" as Overlay, traced: t, sub: "" };
    node.sub = nodeSub(node);
    out.push(node);
  }
  return out;
}

/** Positions for a band's nodes: centred rows of up to PER_ROW. */
function rows(count: number, top: number): { x: number; y: number }[] {
  const at: { x: number; y: number }[] = [];
  for (let i = 0; i < count; i += 1) {
    const row = Math.floor(i / PER_ROW);
    const inRow = Math.min(PER_ROW, count - row * PER_ROW);
    const width = inRow * NODE_W + (inRow - 1) * GAP;
    const left = (W - width) / 2;
    at.push({ x: left + (i % PER_ROW) * (NODE_W + GAP), y: top + row * (NODE_H + GAP) });
  }
  return at;
}

function bandHeight(count: number): number {
  return Math.max(1, Math.ceil(count / PER_ROW)) * (NODE_H + GAP) - GAP;
}

/** IDs read in a row, each hoverable for its words: "MON-1 severity never goes down, MON-2 …". */
function Notes({ notes, testid }: { notes: DesignNote[]; testid: string }) {
  return (
    <p className="pl-notes" data-testid={testid}>
      {notes.map((n, i) => (
        <React.Fragment key={`${n.id}-${i}`}>
          {i > 0 ? <span className="pl-notes-sep" aria-hidden>, </span> : null}
          <span className="pl-note-item" title={n.id ? `${n.id}: ${n.text}` : n.text}>
            {n.id ? <b className="pl-note-id">{n.id}</b> : null}{n.id && n.text ? " " : ""}{n.text}
          </span>
        </React.Fragment>
      ))}
    </p>
  );
}

type Card = { title: string; tone: "ink" | "graph" | "muted"; body: React.ReactNode; testid?: string };

/** The design cards under the read/write view, in this order: what changed, how
 *  it starts and ends, who reviews it, which graph versions it relies on, its policy, decisions
 *  and open questions. A card shows only when the contract says something for it. */
export function DesignCards({ contract, design }: { contract: StageContract | null; design?: StageDesignState }) {
  if (!contract) return null;
  const cards = contract.design;
  const out: Card[] = [];
  const revision = design?.underRevision ?? cards?.underRevision;
  if (revision) out.push({ title: "Under revision", tone: "muted", testid: "card-under-revision", body: <p>{revision}</p> });
  if (contract.trigger) out.push({ title: "Trigger", tone: "ink", body: <p>{contract.trigger}</p> });
  if (contract.handoff) out.push({ title: "Handoff", tone: "ink", body: <p>{contract.handoff}</p> });
  if (contract.human_review) out.push({ title: "Human review", tone: "ink", body: <p>{contract.human_review}</p> });
  const applied = cards?.appliedIn;
  if (applied && (applied.items.length || applied.kg || applied.cg)) {
    const versions = [applied.kg ? `KG ${applied.kg}` : "", applied.cg ? `CG ${applied.cg}` : ""].filter(Boolean);
    const title = versions.length === 2 ? `Applied in ${versions.join(" / ")}`
      : applied.cg ? `Applied in Context Graph ${applied.cg}` : applied.kg ? `Applied in Knowledge Graph ${applied.kg}` : "Applied in";
    out.push({ title, tone: "graph", testid: "card-applied-in",
               body: applied.items.length ? <p>{applied.items.join("; ")}</p> : <p className="pl-faint">No schema changes named.</p> });
  }
  // The design's own notes (with their IDs) are the card when it has them; the contract's plain
  // list says the same things again, so it shows only when the design names none.
  if (cards?.policy.length || contract.policy?.length) {
    out.push({ title: "Policy", tone: "muted", testid: "card-policy", body: (
      <>
        {cards?.policy.length ? <Notes notes={cards.policy} testid="policy-notes" /> : null}
        {contract.policy?.length && !cards?.policy.length ? (
          <ul className="pl-kv">{contract.policy.map(([k, v]) => <li key={k}><b>{k}</b> {v}</li>)}</ul>
        ) : null}
      </>
    ) });
  }
  if (cards?.openQuestions.length || contract.open_questions?.length) {
    out.push({ title: "Open questions", tone: "muted", testid: "card-open-questions", body: (
      <>
        {cards?.openQuestions.length ? <Notes notes={cards.openQuestions} testid="open-question-notes" /> : null}
        {contract.open_questions?.length && !cards?.openQuestions.length ? <ul>{contract.open_questions.map((d) => <li key={d}>{d}</li>)}</ul> : null}
      </>
    ) });
  }
  if (cards?.decisions.length || contract.decisions?.length) {
    out.push({ title: "Decisions", tone: "ink", testid: "card-decisions", body: (
      <>
        {cards?.decisions.length ? <Notes notes={cards.decisions} testid="decision-notes" /> : null}
        {contract.decisions?.length && !cards?.decisions.length ? <ul>{contract.decisions.map((d) => <li key={d}>{d}</li>)}</ul> : null}
      </>
    ) });
  }
  if (contract.llm) {
    out.push({ title: "Model", tone: "muted", body: <p>{contract.llm === "none" ? "None: no model is used" : contract.llm}</p> });
  }
  if (!out.length) return null;
  return (
    <div className="pl-cards" data-testid="contract-cards">
      {out.map((c) => (
        <div key={c.title} className="pl-design-card" data-tone={c.tone} data-testid={c.testid}>
          <div className="pl-design-card-title">{c.title}</div>
          <div className="pl-design-card-body">{c.body}</div>
        </div>
      ))}
    </div>
  );
}

export function DesignPill({ status, testid = "design-status" }: { status: DesignStatus; testid?: string }) {
  return <span className="pl-design" data-design={status} data-testid={testid}>{DESIGN_LABEL[status]}</span>;
}

/** The design's status, its reference, and the review buttons a person may press now. */
function DesignBar({ design, onAction, busy, error }: {
  design: StageDesignState; onAction?: (action: DesignAction) => void; busy?: boolean; error?: string | null;
}) {
  const locked = design.locked && design.status !== "locked" ? design.locked : null;
  return (
    <div className="pl-design-bar" data-testid="design-bar">
      <DesignPill status={design.status} />
      {design.reference ? <span className="pl-design-ref" data-testid="design-reference">{design.reference}</span> : null}
      {locked?.reference ? (
        <span className="pl-faint" data-testid="design-locked-version">{`${locked.reference} stays locked while this is reviewed`}</span>
      ) : design.status === "locked" && design.locked?.by ? (
        <span className="pl-faint">{`Locked by ${design.locked.by}`}</span>
      ) : null}
      {onAction ? (
        <span className="pl-design-actions">
          {design.can.send_for_review ? (
            <GatedButton permission="workflow:create" className="pl-btn" data-testid="design-send-for-review" disabled={busy}
                         title="Ask a reviewer to check this agent's design" onClick={() => onAction("review")}>Send for review</GatedButton>
          ) : null}
          {design.can.lock ? (
            <GatedButton permission="workflow:publish" className="pl-btn pl-btn-primary" data-testid="design-lock" disabled={busy}
                         title="Lock this design: its contract cannot change until it is revised. Asks you to confirm it is you."
                         onClick={() => onAction("lock")}>Lock design</GatedButton>
          ) : null}
          {design.can.revise ? (
            <GatedButton permission="workflow:create" className="pl-btn" data-testid="design-revise" disabled={busy}
                         title="Open the next version of this design for review; the locked one stays on record"
                         onClick={() => onAction("revise")}>Revise</GatedButton>
          ) : null}
        </span>
      ) : null}
      {error ? <span className="pl-design-error" role="alert" data-testid="design-error">{error}</span> : null}
    </div>
  );
}

export function AgentContractView({ stage, hasRun, onClose, graph, onDesignAction, designBusy, designError }: {
  stage: PipelineStage;
  /** Whether the workflow has a last run at all. */
  hasRun: boolean;
  onClose?: () => void;
  /** The project's graph versions and kinds, for the band titles and domain colours. */
  graph?: { kgVersion: number | null; cgVersion: number | null; kinds: Record<string, KindMeta>;
            contextKinds: Set<string> };
  /** Send for review / Lock design / Revise; absent when the viewer cannot change the design. */
  onDesignAction?: (action: DesignAction) => void;
  designBusy?: boolean;
  designError?: string | null;
}) {
  const [showRun, setShowRun] = useState(false);
  const [highlighted, setHighlighted] = useState<string | null>(null);
  const contract = stage.contract;
  const steps = contract?.steps?.length ? contract.steps : null;
  const reached =!!stage.trace && !["pending", "not_run"].includes(stage.status);
  const overlay = showRun && reached && stage.trace ? stage.trace : null;
  const runTip = !hasRun ? "This workflow has not run yet"
    : !reached ? "This stage was not reached on the last run" : "Overlay what the last run read and wrote";

  const reads = useMemo(() => graphNodes("read", contract?.reads ?? [], overlay ? overlay.reads : null),
    [contract, overlay]);
  const writes = useMemo(() => graphNodes("write", contract?.writes ?? [], overlay ? overlay.writes : null),
    [contract, overlay]);
  const calls: CallNode[] = (contract?.calls ?? []).map((c) => {
    const tool = c.tool || c.mcp;
    const made = overlay
      ? (overlay.calls.find((x) => x.tool === tool && (!x.server || !c.mcp || x.server === c.mcp))?.count ?? null)
      : null;
    // A step that records its calls and has none for this one did not make it; a step with no
    // record at all (an agent that took no turns we can read) cannot be said to have skipped it.
    const missing = !!overlay && !made && !!overlay.callsRecorded;
    const system = c.mcp ? kindLabel(c.mcp).replace(/^Dq\b/, "DQ") : "Outside system";
    return {
      key: `${c.mcp}-${c.tool}`, label: system.length <= 4 ? system.toUpperCase() : system,
      sub: !overlay ? humanTool(tool)
        : made ? `${humanTool(tool)}, called ${made}`
        : `${humanTool(tool)}, ${missing ? "declared, not done" : "not recorded"}`,
      made, missing,
    };
  });

  // Geometry.
  const kgTop = 34;
  const kgH = bandHeight(reads.length);
  const midTop = kgTop + kgH + 64;
  const callsH = calls.length ? calls.length * (CALL_H + 10) - 10 : 0;
  const midH = Math.max(STEP_H, callsH);
  const stepX = calls.length ? 180 : (W - STEP_W) / 2;
  const stepY = midTop + (midH - STEP_H) / 2;
  const callX = W - CALL_W - 40;
  const cgLabelY = midTop + midH + 44;
  const cgTop = cgLabelY + 18;
  const cgH = bandHeight(writes.length);
  const H = cgTop + cgH + 16;
  const readAt = rows(reads.length, kgTop);
  const writeAt = rows(writes.length, cgTop);

  const focus = (key: string) => setHighlighted((h) => (h === key ? null : key));
  const dim = (key: string) => highlighted !== null && highlighted !== key && highlighted !== "step";
  const isHl = (key: string) => highlighted === key || highlighted === "step";
  const attach = (i: number, count: number) => stepX + ((i + 1) * STEP_W) / (count + 1);

  const keyProps = (key: string, label: string) => ({
    role: "button" as const, tabIndex: 0, "aria-label": label, "aria-pressed": highlighted === key,
    onClick: () => focus(key),
    onKeyDown: (e: React.KeyboardEvent) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); focus(key); } },
  });

  const selectedNode = [...reads, ...writes].find((n) => `${n.side}-${n.key}` === highlighted);

  return (
    <div data-area="agent-contract-view" className="pl-acv">
      <div className="pl-acv-head">
        <div>
          <div className="pl-acv-kicker">
            {stage.number ? `Stage ${stage.number}` : stage.placement === "shared" ? "Shared service" : stage.placement === "side" ? (stage.sideLabel ?? "Side path") : "Stage"}
            {steps ? ", agent design" : ", read/write view"}
          </div>
          <h3 className="pl-acv-title">{stage.number ? `${stage.number}. ${stage.title}` : stage.title}</h3>
          <div className="pl-acv-desc">{stage.description}</div>
          {contract?.design?.example ? (
            <div className="pl-acv-example" data-testid="design-example">{`Example: ${contract.design.example}`}</div>
          ) : null}
          <DesignBar design={stage.design} onAction={onDesignAction} busy={designBusy} error={designError} />
        </div>
        <div className="pl-acv-actions">
          <button type="button" className="pl-toggle" data-testid="show-last-run"
                  aria-pressed={showRun && reached} disabled={!hasRun || !reached}
                  title={runTip} data-tip={runTip}
                  onClick={() => setShowRun((v) => !v)}>
            <span className="pl-toggle-dot" aria-hidden />Show the last run
          </button>
          {onClose ? (
            <button type="button" className="pl-close" aria-label="Close the read/write view" onClick={onClose}>×</button>
          ) : null}
        </div>
      </div>

      {!contract ? (
        <p className="pl-note" data-testid="no-contract">
          This stage declares no contract yet, so nothing it reads or writes is checked.
          {reached ? " Show the last run to see what it actually touched." : ""}
        </p>
      ) : null}

      {steps && contract ? (
        <AgentStepsMap stage={stage} steps={steps} kinds={graph?.kinds ?? {}}
                       contextKinds={graph?.contextKinds ?? new Set()} kgVersion={graph?.kgVersion ?? null}
                       cgVersion={graph?.cgVersion ?? null}
                       overlay={overlay ? { reads: overlay.reads, writes: overlay.writes, calls: overlay.calls,
                                            callsRecorded: overlay.callsRecorded } : null} />
      ) : (<>
      <div className="pl-legend" aria-hidden>
        <span><i className="pl-key pl-key-read" />Reads</span>
        <span><i className="pl-key pl-key-write" />Writes</span>
        <span><i className="pl-key pl-key-call" />Calls (MCP)</span>
        <span>● Required write</span>
        {overlay ? (
          <>
            <span><i className="pl-dot" data-overlay="done" />Done as declared</span>
            <span><i className="pl-dot" data-overlay="missing" />Declared, not done</span>
            <span><i className="pl-dot" data-overlay="undeclared" />Done, not declared</span>
          </>
        ) : null}
      </div>

      <svg className="pl-map" viewBox={`0 0 ${W} ${H}`} role="group" data-testid="contract-map"
           aria-label={`What ${stage.title} reads and writes`} data-overlay-on={overlay ? "true" : "false"}>
        {/* Bands */}
        <rect className="pl-band pl-band-kg" x={0} y={0} width={W} height={kgTop + kgH + 16} rx={10} />
        <text className="pl-band-label" x={14} y={22}>{bandTitle("Knowledge Graph", graph?.kgVersion ?? null, "read only")}</text>
        <rect className="pl-band pl-band-cg" x={0} y={cgLabelY - 26} width={W} height={H - (cgLabelY - 26)} rx={10} />
        <text className="pl-band-label" x={14} y={cgLabelY - 4}>{bandTitle("Context Graph", graph?.cgVersion ?? null, "read and write")}</text>

        {/* Lines first, so nodes sit on top. */}
        {reads.map((n, i) => {
          const p = readAt[i];
          const x1 = p.x + NODE_W / 2; const y1 = p.y + NODE_H;
          const x2 = attach(i, reads.length); const y2 = stepY;
          const k = `read-${n.key}`;
          return (
            <path key={k} data-testid={`edge-${k}`} className="pl-edge pl-edge-read" data-overlay={n.overlay}
                  data-highlighted={isHl(k) ? "true" : "false"} data-dim={dim(k) ? "true" : undefined}
                  d={`M ${x1} ${y1} C ${x1} ${(y1 + y2) / 2}, ${x2} ${(y1 + y2) / 2}, ${x2} ${y2}`} />
          );
        })}
        {writes.map((n, i) => {
          const p = writeAt[i];
          const x1 = attach(i, writes.length); const y1 = stepY + STEP_H;
          const x2 = p.x + NODE_W / 2; const y2 = p.y;
          const k = `write-${n.key}`;
          return (
            <path key={k} data-testid={`edge-${k}`} className="pl-edge pl-edge-write" data-overlay={n.overlay}
                  data-highlighted={isHl(k) ? "true" : "false"} data-dim={dim(k) ? "true" : undefined}
                  markerEnd="url(#pl-arrow-write)"
                  d={`M ${x1} ${y1} C ${x1} ${(y1 + y2) / 2}, ${x2} ${(y1 + y2) / 2}, ${x2} ${y2}`} />
          );
        })}
        {calls.map((c, i) => {
          const y = midTop + (midH - callsH) / 2 + i * (CALL_H + 10) + CALL_H / 2;
          const k = `call-${c.key}`;
          return (
            <path key={k} data-testid={`edge-${k}`} className="pl-edge pl-edge-call"
                  data-highlighted={isHl(k) ? "true" : "false"} data-dim={dim(k) ? "true" : undefined}
                  d={`M ${stepX + STEP_W} ${stepY + STEP_H / 2} C ${stepX + STEP_W + 60} ${stepY + STEP_H / 2}, ${callX - 60} ${y}, ${callX} ${y}`} />
          );
        })}
        <defs>
          <marker id="pl-arrow-write" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
            <path d="M 0 0 L 10 5 L 0 10 z" className="pl-arrowhead" />
          </marker>
        </defs>

        {/* Knowledge Graph band */}
        <g data-testid="kg-band">
          {!reads.length ? (
            <text className="pl-empty-band" x={W / 2} y={kgTop + 26} textAnchor="middle">
              {contract ? "Reads nothing from the Knowledge Graph" : "No reads declared"}
            </text>
          ) : null}
          {reads.map((n, i) => (
            <g key={n.key} data-testid={`node-read-${n.key}`} data-overlay={n.overlay}
               data-highlighted={highlighted === `read-${n.key}` ? "true" : "false"}
               className="pl-node pl-node-read" data-dim={dim(`read-${n.key}`) ? "true" : undefined}
               {...keyProps(`read-${n.key}`, `${n.label}, read${n.sub ? `, ${n.sub}` : ""}`)}>
              <rect x={readAt[i].x} y={readAt[i].y} width={NODE_W} height={NODE_H} rx={8} />
              <text x={readAt[i].x + NODE_W / 2} y={readAt[i].y + (n.sub ? 19 : 28)} textAnchor="middle" className="pl-node-label">{n.label}</text>
              {n.sub ? <text x={readAt[i].x + NODE_W / 2} y={readAt[i].y + 35} textAnchor="middle" className="pl-node-sub">{n.sub}</text> : null}
            </g>
          ))}
        </g>

        {/* The stage */}
        <g data-testid="step-node" className="pl-node pl-node-step" {...keyProps("step", `${stage.title}: highlight every line`)}
           data-highlighted={highlighted === "step" ? "true" : "false"}>
          <rect x={stepX} y={stepY} width={STEP_W} height={STEP_H} rx={12} />
          <text x={stepX + STEP_W / 2} y={stepY + 28} textAnchor="middle" className="pl-step-title">{clip(stage.title, 30)}</text>
          <text x={stepX + STEP_W / 2} y={stepY + 50} textAnchor="middle" className="pl-step-sub">
            {clip(stage.runsAs.label, 36)}{overlay ? `, ${STATUS_LABEL[stage.status]}` : ""}
          </text>
        </g>

        {/* Outside systems via MCP */}
        <g data-testid="mcp-band">
          {calls.map((c, i) => {
            const y = midTop + (midH - callsH) / 2 + i * (CALL_H + 10);
            return (
              <g key={c.key} data-testid={`node-call-${c.key}`} className="pl-node pl-node-call"
                 data-made={c.made ? "true" : undefined}
                 data-overlay={c.missing ? "missing" : undefined}
                 data-highlighted={highlighted === `call-${c.key}` ? "true" : "false"}
                 data-dim={dim(`call-${c.key}`) ? "true" : undefined}
                 {...keyProps(`call-${c.key}`, `Calls ${c.label}: ${c.sub}`)}>
                <rect x={callX} y={y} width={CALL_W} height={CALL_H} rx={8} />
                <text x={callX + 12} y={y + 17} className="pl-node-label">{c.label}</text>
                <text x={callX + 12} y={y + 32} className="pl-node-sub">{clip(c.sub, 36)}</text>
              </g>
            );
          })}
        </g>

        {/* Context Graph band */}
        <g data-testid="context-band">
          {!writes.length ? (
            <text className="pl-empty-band" x={W / 2} y={cgTop + 26} textAnchor="middle">
              {contract ? "Writes nothing to the Context Graph" : "No writes declared"}
            </text>
          ) : null}
          {writes.map((n, i) => (
            <g key={n.key} data-testid={`node-write-${n.key}`} data-overlay={n.overlay}
               data-required={n.required ? "true" : undefined}
               data-highlighted={highlighted === `write-${n.key}` ? "true" : "false"}
               className="pl-node pl-node-write" data-dim={dim(`write-${n.key}`) ? "true" : undefined}
               {...keyProps(`write-${n.key}`, `${n.label}, written${n.sub ? `, ${n.sub}` : ""}`)}>
              <rect x={writeAt[i].x} y={writeAt[i].y} width={NODE_W} height={NODE_H} rx={8} />
              <text x={writeAt[i].x + NODE_W / 2} y={writeAt[i].y + (n.sub ? 19 : 28)} textAnchor="middle" className="pl-node-label">
                {n.required ? "● " : ""}{n.label}
              </text>
              {n.sub ? <text x={writeAt[i].x + NODE_W / 2} y={writeAt[i].y + 35} textAnchor="middle" className="pl-node-sub">{clip(n.sub, 24)}</text> : null}
            </g>
          ))}
        </g>
      </svg>

      {selectedNode ? (
        <div className="pl-node-detail" data-testid="node-detail" role="status">
          <b>{selectedNode.label}</b>
          {selectedNode.side === "read" ? ", Knowledge Graph read" : ", Context Graph write"}
          {selectedNode.required ? ", required" : ""}
          {selectedNode.traced ? (
            <>
              {`, the last run ${selectedNode.side === "read" ? "read" : "wrote"} ${selectedNode.traced.count}`}
              {selectedNode.traced.decision_types.length ? ` (${selectedNode.traced.decision_types.join(", ")})` : ""}
              {selectedNode.traced.examples.length ? `: ${selectedNode.traced.examples.join(", ")}` : ""}
            </>
          ) : selectedNode.overlay === "missing" ? ", the last run did not do this" : null}
        </div>
      ) : null}
      </>)}

      <DesignCards contract={contract} design={stage.design} />
    </div>
  );
}

function clip(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/** "get_incident" → "get incident". */
function humanTool(tool: string): string {
  return String(tool ?? "").replace(/[_-]+/g, " ").trim();
}
