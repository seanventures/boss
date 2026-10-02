"use client";

import React, { useState, useTransition } from "react";

import type { KnowledgeGraphFeed, GraphSummary } from "../lib/knowledge-graph";
import {
  fetchGraphSummary,
  seedGraphFromInfra,
} from "../lib/knowledge-graph";

function Pill({ text, color }: { text: string; color: string }) {
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
        whiteSpace: "nowrap",
      }}
    >
      {text}
    </span>
  );
}

export function KnowledgeGraphPanel({ feed }: { feed: KnowledgeGraphFeed }) {
  const [serviceKey, setServiceKey] = useState("");
  const [summary, setSummary] = useState<GraphSummary | null>(null);
  const [seedNote, setSeedNote] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  const health = feed.health;

  if (!feed.apiReachable) {
    return (
      <section className="ct-block" data-empty-state="knowledge-graph-unreachable">
        <div className="ct-block-title">Knowledge graph</div>
        <p className="empty-state" style={{ padding: "12px 0" }}>
          Control plane unreachable — graph health could not be loaded.
        </p>
      </section>
    );
  }

  const empty = !health || health.node_count === 0;

  return (
    <div style={{ display: "grid", gap: 16 }} data-area="knowledge-graph">
      <section className="ct-block">
        <div className="ct-block-title">Knowledge graph</div>
        <p className="detail" style={{ marginTop: 4, marginBottom: 12 }}>
          Topology + RCA learning substrate. Seed from infra, inspect health, and pull an RCA
          pack (subgraph neighborhood + known causes) for a service.
        </p>

        {health ? (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))",
              gap: 10,
              marginBottom: 12,
            }}
          >
            <Stat
              label="Status"
              value={health.status}
              ok={health.healthy}
            />
            <Stat label="Nodes" value={String(health.node_count)} />
            <Stat label="Edges" value={String(health.edge_count)} />
          </div>
        ) : null}

        {empty ? (
          <div data-empty-state="knowledge-graph-empty">
            <p className="empty-state" style={{ padding: "8px 0" }}>
              Graph is empty for this project. Seed resource nodes from active infra inventory,
              or let reliability investigations write services and causes as they run.
            </p>
            {(health?.hints ?? []).map((h) => (
              <p key={h} className="detail" style={{ marginTop: 0 }}>
                {h}
              </p>
            ))}
          </div>
        ) : (
          <div style={{ marginBottom: 10 }}>
            <div style={{ fontSize: 12, fontWeight: 600, color: "var(--muted)", marginBottom: 6 }}>
              Nodes by kind
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {Object.entries(health?.nodes_by_kind ?? {}).map(([k, n]) => (
                <Pill key={k} text={`${k}: ${n}`} color="#58a6ff" />
              ))}
            </div>
          </div>
        )}

        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
          <button
            type="button"
            disabled={pending}
            data-testid="kg-seed"
            onClick={() =>
              startTransition(async () => {
                const res = await seedGraphFromInfra(feed.projectId);
                if (res?.health) {
                  setSeedNote(
                    `Seeded ${res.seeded?.resources ?? 0} resources · ${res.health.node_count} nodes`,
                  );
                } else {
                  setSeedNote("Seed failed or control plane unreachable.");
                }
              })
            }
            style={btnStyle}
          >
            Seed from infra
          </button>
          {seedNote ? (
            <span style={{ fontSize: 12, color: "var(--muted)" }} data-testid="kg-seed-note">{seedNote}</span>
          ) : null}
        </div>
      </section>

      <section className="ct-block">
        <div className="ct-block-title">RCA pack</div>
        <p className="detail" style={{ marginTop: 4 }}>
          Load subgraph neighborhood + prior confirmed causes for a service node key.
        </p>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 10, alignItems: "center" }}>
          <input
            type="text"
            placeholder="service node_key (e.g. checkout)"
            value={serviceKey}
            onChange={(e) => setServiceKey(e.target.value)}
            style={{
              background: "var(--panel)",
              color: "var(--ink)",
              border: "1px solid var(--line)",
              borderRadius: 8,
              padding: "6px 10px",
              fontSize: 13,
              minWidth: 220,
            }}
          />
          <button
            type="button"
            disabled={pending || !serviceKey.trim()}
            onClick={() =>
              startTransition(async () => {
                const res = await fetchGraphSummary(feed.projectId, {
                  kind: "service",
                  nodeKey: serviceKey.trim(),
                  depth: 2,
                });
                setSummary(res);
              })
            }
            style={btnStyle}
          >
            Load RCA pack
          </button>
        </div>

        {summary?.rca ? (
          <div style={{ marginTop: 14, display: "grid", gap: 10 }}>
            {!summary.rca.found ? (
              <p className="empty-state">No node found for that key.</p>
            ) : (
              <>
                <div style={{ fontSize: 13 }}>
                  <b style={{ color: "var(--ink)" }}>
                    {summary.rca.root?.name || summary.rca.root?.node_key}
                  </b>
                  <span style={{ color: "var(--muted)", marginLeft: 8 }}>
                    {summary.rca.subgraph.node_count} nodes · {summary.rca.subgraph.edge_count}{" "}
                    edges · {summary.rca.known_causes_count} known causes
                  </span>
                </div>
                <div>
                  <div style={{ fontSize: 12, fontWeight: 600, color: "var(--muted)", marginBottom: 6 }}>
                    Neighborhood
                  </div>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                    {summary.rca.subgraph.nodes.map((n) => (
                      <Pill
                        key={n.id}
                        text={`${n.kind}:${n.node_key}`}
                        color={n.kind === "service" ? "#3fb950" : "#58a6ff"}
                      />
                    ))}
                  </div>
                </div>
                {summary.rca.known_causes.length ? (
                  <div>
                    <div style={{ fontSize: 12, fontWeight: 600, color: "var(--muted)", marginBottom: 6 }}>
                      Known causes
                    </div>
                    <div style={{ display: "grid", gap: 6 }}>
                      {summary.rca.known_causes.map((c) => (
                        <div
                          key={c.node_key}
                          style={{
                            padding: "8px 10px",
                            borderRadius: 8,
                            background: "var(--panel-2, rgba(128,128,128,0.04))",
                            fontSize: 13,
                            display: "flex",
                            gap: 8,
                            alignItems: "center",
                          }}
                        >
                          <b style={{ color: "var(--ink)" }}>{c.name || c.node_key}</b>
                          <span style={{ color: "var(--muted)", fontSize: 12 }}>
                            conf {Number(c.confidence ?? 0).toFixed(2)}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                ) : (
                  <p className="detail">No prior confirmed causes for this service.</p>
                )}
              </>
            )}
          </div>
        ) : null}
      </section>

      {feed.nodes.length ? (
        <section className="ct-block">
          <div className="ct-block-title">Recent nodes</div>
          <div style={{ display: "grid", gap: 6, marginTop: 8 }}>
            {feed.nodes.slice(0, 15).map((n) => (
              <div
                key={n.id}
                style={{
                  display: "flex",
                  gap: 8,
                  alignItems: "center",
                  fontSize: 12.5,
                  padding: "6px 8px",
                  borderRadius: 6,
                  background: "var(--panel-2, rgba(128,128,128,0.04))",
                }}
              >
                <Pill text={n.kind} color="#a371f7" />
                <span style={{ color: "var(--ink)", fontFamily: "var(--mono, monospace)" }}>
                  {n.node_key}
                </span>
                <span style={{ color: "var(--muted)" }}>{n.name}</span>
              </div>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}

function Stat({
  label,
  value,
  ok,
}: {
  label: string;
  value: string;
  ok?: boolean;
}) {
  return (
    <div
      style={{
        padding: "10px 12px",
        borderRadius: 10,
        border: "1px solid var(--line)",
        background: "var(--panel-2, rgba(128,128,128,0.03))",
      }}
    >
      <div
        style={{
          fontSize: 11,
          color: "var(--muted)",
          textTransform: "uppercase",
          letterSpacing: "0.04em",
        }}
      >
        {label}
      </div>
      <div
        style={{
          fontSize: 16,
          fontWeight: 700,
          color: ok === false ? "#d29922" : ok ? "#3fb950" : "var(--ink)",
          marginTop: 2,
        }}
      >
        {value}
      </div>
    </div>
  );
}

const btnStyle: React.CSSProperties = {
  background: "transparent",
  color: "var(--ink-soft)",
  border: "1px solid var(--line)",
  borderRadius: 8,
  padding: "6px 12px",
  fontSize: 12.5,
  fontWeight: 600,
  cursor: "pointer",
};
