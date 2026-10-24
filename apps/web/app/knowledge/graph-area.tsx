"use client";

/**
 * GRAPH — code, rules and what agents learned (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5).
 *
 * Phase 0: the area is registered and mounted, with its tab row (§5.1: Graph · Rules review ·
 * Settings) and a designed state on every tab, so the rail entry never opens an unstyled screen.
 * Stream S4 replaces the bodies with the tree, canvas, detail panel and Rules review, reading
 * lib/kg-view-types.ts. Tokens only (kg.css); the layer colours are kg.css custom properties.
 */
import React, { useState } from "react";

import { AreaLink } from "../area-link";
import { AreaHead, type HeadTab } from "../infra/kit";
import { KG_LAYERS } from "../../lib/kg-view-types";
import "../infra/infra.css";
import "./kg.css";

type GraphTab = "graph" | "rules" | "settings";

const TABS: HeadTab[] = [
  { key: "graph", label: "Graph", tip: "The whole repository, or one symbol, rule or learning and what connects to it" },
  { key: "rules", label: "Rules review", tip: "Business rules agents proposed: approve, edit or reject" },
  { key: "settings", label: "Settings", tip: "Indexing, embeddings and retention" },
];

const LAYER_LINE: Record<(typeof KG_LAYERS)[number], { name: string; what: string }> = {
  code: { name: "Code", what: "indexed from the repository" },
  rules: { name: "Rules", what: "proposed by agents, approved by people" },
  learning: { name: "Learning", what: "written by agents as they work" },
};

/** Faint hop rings behind the card: the Focus view's motif, drawn in tokens. */
function Rings() {
  return (
    <svg className="kg-stage-rings" viewBox="0 0 800 460" preserveAspectRatio="xMidYMid slice" aria-hidden focusable="false">
      <circle cx="400" cy="230" r="120" />
      <circle cx="400" cy="230" r="210" />
      <circle cx="400" cy="230" r="300" />
    </svg>
  );
}

function Layers() {
  return (
    <ul className="kg-layers" aria-label="The three layers">
      {KG_LAYERS.map((l) => (
        <li key={l} className="kg-layer">
          <span className="kg-dot" data-layer={l} aria-hidden />
          <span><span className="kg-layer-name">{LAYER_LINE[l].name}</span> · {LAYER_LINE[l].what}</span>
        </li>
      ))}
    </ul>
  );
}

function Empty({ testId, eyebrow, title, body, children }: {
  testId: string; eyebrow: string; title: string; body: string; children?: React.ReactNode;
}) {
  return (
    <div className="kg-stage" data-testid={testId}>
      <Rings />
      <div className="kg-empty" role="status">
        <span className="kg-empty-eyebrow">{eyebrow}</span>
        <h3 className="kg-empty-title">{title}</h3>
        <p className="kg-empty-body">{body}</p>
        <Layers />
        {children ? <div className="kg-empty-actions">{children}</div> : null}
      </div>
    </div>
  );
}

export function GraphArea({ projectId, actor, orgId }: { projectId: string; actor: string; orgId?: string | null }) {
  const [tab, setTab] = useState<GraphTab>("graph");
  return (
    <div className="kg" data-testid="kg" data-project={projectId} data-actor={actor}>
      <AreaHead area="kg" title="Graph" tabs={TABS} active={tab} onTab={(k) => setTab(k as GraphTab)} />
      {tab === "graph" ? (
        <Empty testId="kg-empty-graph" eyebrow="Graph"
               title="Connect a repository to see its graph"
               body="BOSS indexes the repository into a graph of its areas, packages, classes and calls. Rules and what agents learn while they work are drawn on the same graph.">
          <AreaLink area="setup" orgId={orgId ?? undefined} className="ag-btn ag-btn-primary">Open Repo and models</AreaLink>
        </Empty>
      ) : tab === "rules" ? (
        <Empty testId="kg-empty-rules" eyebrow="Rules review"
               title="No rules yet"
               body="Agents propose business rules from the code, each with the lines that show it. A rule becomes trusted input for every agent only after a person approves it here." />
      ) : (
        <Empty testId="kg-empty-settings" eyebrow="Settings"
               title="Indexing, embeddings and retention"
               body="These settings are in Reliability for now, beside the knowledge explorer.">
          <AreaLink area="reliability" orgId={orgId ?? undefined} className="ag-btn">Open Reliability</AreaLink>
        </Empty>
      )}
    </div>
  );
}
