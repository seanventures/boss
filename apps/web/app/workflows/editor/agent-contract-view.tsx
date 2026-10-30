"use client";

import React, { useState } from "react";

type ContractNode = { id: string; label: string };

type StepContract = {
  reads: ContractNode[];
  writes: ContractNode[];
  calls: ContractNode[];
};

type Step = {
  id: string;
  agent?: string;
  contract: StepContract;
};

type RealRun = {
  runId: string;
  reads: ContractNode[];
  writes: ContractNode[];
};

type Kind = "read" | "write" | "call";

type Overlay = "contract-only" | "real-only" | undefined;

function dedupe(nodes: ContractNode[]): ContractNode[] {
  const seen = new Map<string, ContractNode>();
  for (const node of nodes) seen.set(node.id, node);
  return Array.from(seen.values());
}

function ContractLine({
  kind,
  node,
  overlay,
  highlighted,
  onClick,
}: {
  kind: Kind;
  node: ContractNode;
  overlay: Overlay;
  highlighted: boolean;
  onClick: () => void;
}) {
  const color = kind === "read" ? "#8a9099" : kind === "write" ? "#3a6fd8" : "#9b59b6";
  const style: React.CSSProperties = {
    cursor: "pointer",
    display: "flex",
    alignItems: "center",
    gap: 8,
    padding: "4px 8px",
    borderRadius: 6,
    border: `1px ${kind === "call" ? "dotted" : "solid"} ${color}`,
    background: highlighted ? `${color}22` : "transparent",
    opacity: overlay === "contract-only" ? 0.5 : 1,
    width: "100%",
    textAlign: "left",
    font: "inherit",
    color: "inherit",
  };
  const suffix =
    overlay === "contract-only" ? ", never happened" : overlay === "real-only" ? ", undeclared in contract" : "";
  return (
    <button
      type="button"
      data-testid={`line-${kind}-${node.id}`}
      data-highlighted={highlighted ? "true" : "false"}
      data-overlay={overlay}
      aria-pressed={highlighted}
      aria-label={`${kind} ${node.label}${suffix}`}
      onClick={onClick}
      style={style}
    >
      <span>{node.label}</span>
      {overlay === "contract-only" ? (
        <span style={{ fontSize: 11, color: "#a6433a" }}>never happened</span>
      ) : null}
      {overlay === "real-only" ? (
        <span style={{ fontSize: 11, color: "#1f8a5f" }}>undeclared in contract</span>
      ) : null}
    </button>
  );
}

export function AgentContractView({
  step,
  realRun,
}: {
  step: Step;
  realRun?: RealRun;
}) {
  const [highlighted, setHighlighted] = useState<string | null>(null);

  const contractReads = step.contract.reads ?? [];
  const contractWrites = step.contract.writes ?? [];
  const calls = step.contract.calls ?? [];

  const reads = realRun
    ? dedupe([...contractReads, ...realRun.reads])
    : contractReads;
  const writes = realRun
    ? dedupe([...contractWrites, ...realRun.writes])
    : contractWrites;

  const overlayFor = (kind: "read" | "write", id: string): Overlay => {
    if (!realRun) return undefined;
    const declared = (kind === "read" ? contractReads : contractWrites).some((n) => n.id === id);
    const real = (kind === "read" ? realRun.reads : realRun.writes).some((n) => n.id === id);
    if (declared && !real) return "contract-only";
    if (!declared && real) return "real-only";
    return undefined;
  };

  const select = (kind: Kind, id: string) => {
    setHighlighted(`${kind}-${id}`);
  };

  return (
    <div data-area="agent-contract-view" style={{ display: "grid", gap: 16 }}>
      {realRun ? (
        <div style={{ fontSize: 12, color: "#5b6570" }}>
          Showing the last real run overlaid against the contract — faded lines never fired, marked
          lines weren't declared.
        </div>
      ) : null}

      <section data-testid="kg-band" style={{ display: "grid", gap: 8 }}>
        <div className="ct-block-title">Knowledge Graph (read only)</div>
        {reads.length === 0 ? (
          <div style={{ fontSize: 12, color: "#5b6570" }}>No reads declared for this step.</div>
        ) : (
          reads.map((node) => (
            <ContractLine
              key={node.id}
              kind="read"
              node={node}
              overlay={overlayFor("read", node.id)}
              highlighted={highlighted === `read-${node.id}`}
              onClick={() => select("read", node.id)}
            />
          ))
        )}
      </section>

      <section style={{ display: "grid", gap: 8 }}>
        <div className="ct-block-title">Agent step</div>
        <div style={{ fontSize: 13, fontWeight: 600 }}>{step.agent ?? "—"}</div>
      </section>

      <section data-testid="context-band" style={{ display: "grid", gap: 8 }}>
        <div className="ct-block-title">Context Graph</div>
        {writes.length === 0 ? (
          <div style={{ fontSize: 12, color: "#5b6570" }}>No writes declared for this step.</div>
        ) : (
          writes.map((node) => (
            <ContractLine
              key={node.id}
              kind="write"
              node={node}
              overlay={overlayFor("write", node.id)}
              highlighted={highlighted === `write-${node.id}`}
              onClick={() => select("write", node.id)}
            />
          ))
        )}
      </section>

      <section data-testid="mcp-band" style={{ display: "grid", gap: 8 }}>
        <div className="ct-block-title">External MCP systems</div>
        {calls.length === 0 ? (
          <div style={{ fontSize: 12, color: "#5b6570" }}>No external calls declared for this step.</div>
        ) : (
          calls.map((node) => (
            <ContractLine
              key={node.id}
              kind="call"
              node={node}
              overlay={undefined}
              highlighted={highlighted === `call-${node.id}`}
              onClick={() => select("call", node.id)}
            />
          ))
        )}
      </section>
    </div>
  );
}
