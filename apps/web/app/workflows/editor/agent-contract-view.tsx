"use client";

/**
 * THE AGENT READ/WRITE VIEW (design §4.7.2): one stage, in three bands —
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

import {
  kindKey, kindLabel, STATUS_LABEL, type ContractKind, type PipelineStage, type StageContract,
} from "../../../lib/pipeline-model";
import type { TracedKind } from "../../../lib/workflow-api";

type Side = "read" | "write";
type Overlay = "declared" | "done" | "missing" | "undeclared";

type GraphNode = {
  key: string; side: Side; kind: string; label: string; sub: string;
  required: boolean; overlay: Overlay; traced?: TracedKind; declared?: ContractKind;
};
type CallNode = { key: string; label: string; sub: string; made: number | null };

const W = 920;
const NODE_W = 140;
const NODE_H = 46;
const GAP = 12;
const PER_ROW = 6;
const STEP_W = 260;
const STEP_H = 70;
const CALL_W = 190;
const CALL_H = 40;

function nodeSub(n: { side: Side; required: boolean; overlay: Overlay; traced?: TracedKind; declared?: ContractKind }): string {
  if (n.overlay === "missing") return n.required ? "required · not done" : "declared, not done";
  if (n.overlay === "undeclared") return `done, not declared · ${n.traced?.count ?? 0}`;
  if (n.overlay === "done") return `${n.side === "read" ? "read" : "wrote"} ${n.traced?.count ?? 0}`;
  const parts: string[] = [];
  // A required write is marked ● on its label; its decision type says more than "required".
  if (n.declared?.decision_type) parts.push(n.declared.decision_type);
  else if (n.required) parts.push("required");
  if (n.declared?.via) parts.push(`via ${kindLabel(n.declared.via).toLowerCase()}`);
  return parts.join(" · ");
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

function Cards({ contract }: { contract: StageContract | null }) {
  if (!contract) return null;
  const cards: [string, React.ReactNode][] = [];
  if (contract.trigger) cards.push(["Trigger", contract.trigger]);
  if (contract.handoff) cards.push(["Handoff", contract.handoff]);
  if (contract.human_review) cards.push(["Human review", contract.human_review]);
  if (contract.llm) cards.push(["Model", contract.llm === "none" ? "None: no model is used" : contract.llm]);
  if (contract.policy?.length) {
    cards.push(["Policy", <ul key="p">{contract.policy.map(([k, v]) => <li key={k}><b>{k}</b> {v}</li>)}</ul>]);
  }
  if (contract.decisions?.length) {
    cards.push(["Decisions", <ul key="d">{contract.decisions.map((d) => <li key={d}>{d}</li>)}</ul>]);
  }
  if (contract.open_questions?.length) {
    cards.push(["Open questions", <ul key="q">{contract.open_questions.map((d) => <li key={d}>{d}</li>)}</ul>]);
  }
  if (!cards.length) return null;
  return (
    <div className="pl-cards" data-testid="contract-cards">
      {cards.map(([title, body]) => (
        <div key={title} className="pl-design-card">
          <div className="pl-design-card-title">{title}</div>
          <div className="pl-design-card-body">{body}</div>
        </div>
      ))}
    </div>
  );
}

export function AgentContractView({ stage, hasRun, onClose }: {
  stage: PipelineStage;
  /** Whether the workflow has a last run at all. */
  hasRun: boolean;
  onClose?: () => void;
}) {
  const [showRun, setShowRun] = useState(false);
  const [highlighted, setHighlighted] = useState<string | null>(null);
  const contract = stage.contract;
  const reached = !!stage.trace && !["pending", "not_run"].includes(stage.status);
  const overlay = showRun && reached && stage.trace ? stage.trace : null;
  const runTip = !hasRun ? "This workflow has not run yet"
    : !reached ? "This stage was not reached on the last run" : "Overlay what the last run read and wrote";

  const reads = useMemo(() => graphNodes("read", contract?.reads ?? [], overlay ? overlay.reads : null),
    [contract, overlay]);
  const writes = useMemo(() => graphNodes("write", contract?.writes ?? [], overlay ? overlay.writes : null),
    [contract, overlay]);
  const calls: CallNode[] = (contract?.calls ?? []).map((c) => {
    const tool = c.tool || c.mcp;
    const made = overlay ? (overlay.calls.find((x) => x.tool === tool)?.count ?? null) : null;
    const system = c.mcp ? kindLabel(c.mcp) : "Outside system";
    return {
      key: `${c.mcp}-${c.tool}`, label: system.length <= 4 ? system.toUpperCase() : system,
      sub: overlay ? (made ? `${humanTool(tool)} · called ${made}` : `${humanTool(tool)} · not recorded`) : humanTool(tool),
      made,
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
          <div className="pl-acv-kicker">Stage {stage.number ?? ""} · read/write view</div>
          <div className="pl-acv-title">{stage.title}</div>
          <div className="pl-acv-desc">{stage.description}</div>
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
        <text className="pl-band-label" x={14} y={22}>Knowledge Graph (read only)</text>
        <rect className="pl-band pl-band-cg" x={0} y={cgLabelY - 26} width={W} height={H - (cgLabelY - 26)} rx={10} />
        <text className="pl-band-label" x={14} y={cgLabelY - 4}>Context Graph (read &amp; write)</text>

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
            {clip(stage.runsAs.label, 36)}{overlay ? ` · ${STATUS_LABEL[stage.status]}` : ""}
          </text>
        </g>

        {/* Outside systems via MCP */}
        <g data-testid="mcp-band">
          {calls.map((c, i) => {
            const y = midTop + (midH - callsH) / 2 + i * (CALL_H + 10);
            return (
              <g key={c.key} data-testid={`node-call-${c.key}`} className="pl-node pl-node-call"
                 data-made={c.made ? "true" : undefined}
                 data-highlighted={highlighted === `call-${c.key}` ? "true" : "false"}
                 data-dim={dim(`call-${c.key}`) ? "true" : undefined}
                 {...keyProps(`call-${c.key}`, `Calls ${c.label}: ${c.sub}`)}>
                <rect x={callX} y={y} width={CALL_W} height={CALL_H} rx={8} />
                <text x={callX + 12} y={y + 17} className="pl-node-label">{c.label}</text>
                <text x={callX + 12} y={y + 32} className="pl-node-sub">{clip(c.sub, 30)}</text>
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
          {selectedNode.side === "read" ? " · Knowledge Graph read" : " · Context Graph write"}
          {selectedNode.required ? " · required" : ""}
          {selectedNode.traced ? (
            <>
              {` · the last run ${selectedNode.side === "read" ? "read" : "wrote"} ${selectedNode.traced.count}`}
              {selectedNode.traced.decision_types.length ? ` (${selectedNode.traced.decision_types.join(", ")})` : ""}
              {selectedNode.traced.examples.length ? `: ${selectedNode.traced.examples.join(", ")}` : ""}
            </>
          ) : selectedNode.overlay === "missing" ? " · the last run did not do this" : null}
        </div>
      ) : null}

      <Cards contract={contract} />
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
