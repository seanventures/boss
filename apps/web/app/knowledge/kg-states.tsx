"use client";

/**
 * EVERY STATE OF THE GRAPH, DESIGNED (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.8): loading, no
 * repository, indexing, an empty layer, an error, a stale rollup, a large neighbourhood, locked
 * (the add-on), and an archived project. None is a bare spinner or an unstyled line; each says
 * what is true and offers the next step. tests/kg-states.test.tsx renders each one.
 */
import React, { useEffect, useState } from "react";

import { AreaLink } from "../area-link";
import { LockedPanel } from "../../components/locked-panel";
import { GatedButton } from "../../components/gated";
import { KG_LAYERS } from "../../lib/kg-view-types";
import { INDEX_NOW, type KgRepository } from "../../lib/kg-view";
import { scanReader } from "../../lib/kg-scan";

const LAYER_LINE: Record<(typeof KG_LAYERS)[number], { name: string; what: string }> = {
  code: { name: "Code", what: "indexed from the repository" },
  rules: { name: "Rules", what: "proposed by agents, approved by people" },
  learning: { name: "Learning", what: "written by agents as they work" },
  domain: { name: "Domain", what: "regulations and documents, from your ontology pack" },
  context: { name: "Context", what: "what agents did and decided: cases, decisions, fixes" },
};

export function Layers() {
  return (
    <ul className="kg-layers" aria-label="The layers">
      {KG_LAYERS.map((l) => (
        <li key={l} className="kg-layer">
          <span className="kg-dot" data-layer={l} aria-hidden />
          <span><span className="kg-layer-name">{LAYER_LINE[l].name}</span>. {LAYER_LINE[l].what}</span>
        </li>
      ))}
    </ul>
  );
}

export function StageCard({ testId, title, body, children, layers = false, tone }: {
  testId: string; title: string; body: React.ReactNode; children?: React.ReactNode; layers?: boolean;
  tone?: "red" | "amber";
}) {
  return (
    <div className="kg-stage" data-testid={testId} data-tone={tone}>
      <div className="kg-empty" role="status">
        <h3 className="kg-empty-title">{title}</h3>
        <div className="kg-empty-body">{body}</div>
        {layers ? <Layers /> : null}
        {children ? <div className="kg-empty-actions">{children}</div> : null}
      </div>
    </div>
  );
}

/** Loading: skeleton tree rows and a shimmering canvas; past 1.5 s it says what it is reading. */
export function KgLoading({ nodes }: { nodes?: number | null }) {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setSlow(true), 1500);
    return () => clearTimeout(t);
  }, []);
  return (
    <div className="kg-work kg-work-loading" data-testid="kg-loading" aria-busy="true">
      <div className="kg-col-tree">
        <div className="kg-skel-rows" aria-hidden>
          {Array.from({ length: 14 }, (_, i) => <span key={i} className="kg-skel-row" style={{ width: `${48 + ((i * 37) % 46)}%` }} />)}
        </div>
      </div>
      <div className="kg-col-canvas">
        <div className="kg-shimmer" aria-hidden />
        <p className="kg-loading-note" role="status" data-testid="kg-loading-note">
          {slow ? `Reading ${nodes ? `${nodes.toLocaleString("en-US")} nodes` : "the graph"}…` : <span className="ag-sr">Loading the graph</span>}
        </p>
      </div>
      <div className="kg-col-panel">
        <div className="kg-skel-rows" aria-hidden>
          {Array.from({ length: 6 }, (_, i) => <span key={i} className="kg-skel-row" style={{ width: `${40 + ((i * 23) % 50)}%` }} />)}
        </div>
      </div>
    </div>
  );
}

/** The canvas while a view is read: a shimmer, and past 1.5 s what is being read. */
export function CanvasLoading({ nodes }: { nodes?: number | null }) {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setSlow(true), 1500);
    return () => clearTimeout(t);
  }, []);
  return (
    <div className="kg-canvas-loading" data-testid="kg-canvas-loading" aria-busy="true">
      <div className="kg-shimmer" aria-hidden />
      <p className="kg-loading-note" role="status" data-testid="kg-loading-note">
        {slow ? `Reading ${nodes ? `${nodes.toLocaleString("en-US")} nodes` : "the graph"}…` : <span className="ag-sr">Loading the graph</span>}
      </p>
    </div>
  );
}

/** Empty: no repository registered. */
export function KgNoRepo({ orgId }: { orgId?: string | null }) {
  return (
    <StageCard testId="kg-empty-graph" layers
               title="Connect a repository to see its graph"
               body="BOSS indexes the repository into a graph of its areas, packages, classes and calls. Rules and what agents learn while they work are drawn on the same graph.">
      <AreaLink area="setup" orgId={orgId ?? undefined} className="ag-btn ag-btn-primary">Open Repo and models</AreaLink>
    </StageCard>
  );
}

function progress(r: KgRepository): string {
  if (r.status === "running" && r.files) return `Indexing ${r.parsed ? `${r.parsed.toLocaleString("en-US")} of ` : ""}${r.files.toLocaleString("en-US")} files`;
  if (r.status === "running") return `Indexing ${r.branch ?? "its branch"}`;
  if (r.status === "queued") return "Queued: it starts within a minute";
  if (r.status === "failed" || r.status === "too_large") return `Not indexed: ${r.error ?? r.status.replace("_", " ")}`;
  if (r.status === "indexed") return r.summary ?? "Indexed";
  return "Not indexed yet: queued at the next knowledge tick (within a minute)";
}

/** Empty: the repositories are indexing (it refreshes itself), or failed, or have not started. */
export function KgIndexing({ repos, onIndex, busy }: { repos: KgRepository[]; onIndex?: (repo: string) => void; busy?: boolean }) {
  const working = repos.some((r) => r.status === "queued" || r.status === "running" || r.status === "pending");
  const failed = repos.filter((r) => r.status === "failed" || r.status === "too_large");
  return (
    <StageCard testId="kg-indexing" tone={failed.length && !working ? "amber" : undefined}
               title={working ? "Indexing the repository" : failed.length ? "The repository could not be indexed" : "The graph has not been built yet"}
               body={(
                 <>
                   <ul className="kg-index-list" data-testid="kg-index-list">
                     {repos.map((r) => (
                       <li key={r.repo} className="kg-index-row" data-status={r.status}>
                         <span className="inf-dot" data-tone={r.status === "running" || r.status === "queued" ? "accent" : r.status === "indexed" ? "green" : r.status === "failed" ? "red" : "grey"} aria-hidden />
                         <span className="inf-mono">{r.repo}</span>
                         <span className="kg-index-what">{progress(r)}</span>
                         {r.status === "running" && r.files && r.parsed ? (
                           <span className="kg-bar" role="progressbar" aria-valuemin={0} aria-valuemax={r.files} aria-valuenow={r.parsed} aria-label={`${r.repo} indexing`}>
                             <i style={{ width: `${Math.min(100, Math.round((r.parsed / r.files) * 100))}%` }} />
                           </span>
                         ) : null}
                       </li>
                     ))}
                   </ul>
                   {working ? <p className="kg-quiet">This page refreshes on its own. No model is called: indexing parses the code.</p> : null}
                 </>
               )}>
      {!working && onIndex ? (
        <GatedButton permission={INDEX_NOW} className="ag-btn ag-btn-primary" data-testid="kg-index-start" disabled={busy}
                     onClick={() => onIndex("")}>{busy ? "Starting…" : failed.length ? "Try indexing again" : "Index now"}</GatedButton>
      ) : null}
    </StageCard>
  );
}

/** A read failed: a route not on this install is a designed card that says when it arrives
 *  (tester round 1 P0-3, never "Not Found · Retry"); anything else is the server's words + Retry. */
export function KgReadError({ error, onRetry, testId, what }: {
  error: { status: number; text: string; unavailable?: boolean }; onRetry: () => void; testId: string; what: string;
}) {
  if (error.unavailable) {
    return (
      <div className="kg-stage kg-stage-short" data-testid={`${testId}-unavailable`} data-tone="amber">
        <div className="kg-empty" role="status">
          <h3 className="kg-empty-title">Not on this install yet</h3>
          <p className="kg-empty-body">{what} arrives with this release&apos;s knowledge update. The code graph works now; this view fills in once the update is installed.</p>
          <div className="kg-empty-actions"><button type="button" className="ag-btn" onClick={onRetry}>Check again</button></div>
        </div>
      </div>
    );
  }
  return <KgError text={error.text} onRetry={onRetry} testId={testId} />;
}

/** An error: the server's words and Retry, inline; the other panels keep working. */
export function KgError({ text, onRetry, testId = "kg-error" }: { text: string; onRetry: () => void; testId?: string }) {
  return (
    <div className="kg-banner" data-tone="red" role="alert" data-testid={testId}>
      <span className="kg-banner-text">{text}</span>
      <button type="button" className="ag-btn" onClick={onRetry} data-testid={`${testId}-retry`}>Retry</button>
    </div>
  );
}

export function ago(at: string | null | undefined, now = Date.now()): string {
  if (!at) return "";
  const t = Date.parse(at);
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} h ago`;
  return `${Math.round(h / 24)} days ago`;
}

/** "Built 4 min ago · Rebuild", or the stale badge when the rollup is behind the code. */
export function KgBuilt({ builtAt, stale, onRebuild, busy, indexing }: {
  builtAt: string | null; stale: boolean; onRebuild: () => void; busy?: boolean; indexing?: boolean;
}) {
  return (
    <span className="kg-built" data-testid={stale ? "kg-stale" : "kg-built"} data-stale={stale || undefined}>
      {indexing ? <span className="kg-live" data-testid="kg-indexing-chip"><span className="kg-live-dot" aria-hidden />Indexing…</span> : null}
      {stale ? (
        <span className="kg-stale-badge" title="The code changed since the graph view was built">Graph is from the last index{builtAt ? ` (${ago(builtAt)})` : ""}</span>
      ) : builtAt ? <span className="inf-muted">Built {ago(builtAt)}</span> : null}
      <GatedButton permission={INDEX_NOW} className="ag-btn" data-testid="kg-rebuild" disabled={busy || indexing}
                   title="Index the repository again now and rebuild the graph view. No model is called." onClick={onRebuild}>
        {busy ? "Starting…" : "Rebuild"}
      </GatedButton>
    </span>
  );
}

/** Empty layer: only the Rules (or Learning) chip on and nothing of it — in the whole repo, or
 *  around the node in Focus (tester round 1 P1-6). Says what is true and offers the next step. */
export function KgEmptyLayer({ layer, onScan, scope = "repo", children }: {
  layer: "rules" | "learning" | "domain" | "context"; onScan?: () => void; scope?: "repo" | "here"; children?: React.ReactNode;
}) {
  if (layer === "domain") {
    return (
      <StageCard testId="kg-empty-layer" title={scope === "here" ? "No regulation connects to this" : "No regulations or documents yet"}
                 body="Add a regulation in the Documents tab: BOSS splits it into clauses, and agents trace each clause to the rules and code that implement it." />
    );
  }
  if (layer === "context") {
    return (
      <StageCard testId="kg-empty-layer" title={scope === "here" ? "No agent decided anything about this yet" : "No agent decisions yet"}
                 body="The Context Graph is what agents did and decided: each case, every step's decision with the agent, workflow version and fingerprint it ran under, the precedent it set and the remediation it proposed. Run a workflow and it appears here as the steps finish." />
    );
  }
  return layer === "rules" ? (
    <StageCard testId="kg-empty-layer" title={scope === "here" ? "No rules here yet" : "No rules yet"}
               body={scope === "here"
                 ? `No business rule governs this code yet. Scan it: ${scanReader()} reads it within a budget you set and proposes the rules it finds, each with the lines that show it. You approve them in Rules review.`
                 : `Scan an area for business rules: ${scanReader()} reads its code within a budget you set and proposes the rules it finds, each with the lines that show it. You approve them in Rules review.`}>
      {children}
      {onScan ? <button type="button" className="ag-btn ag-btn-primary" data-testid="kg-empty-layer-scan" onClick={onScan}>Choose an area to scan</button> : null}
    </StageCard>
  ) : (
    <StageCard testId="kg-empty-layer" title={scope === "here" ? "Nothing learned about this yet" : "Nothing learned yet"}
               body="Agents record what they try, rule out, find and fix while they work: test failures and their fixes, root causes, precedents. They appear here, linked to the code, within seconds." />
  );
}

/** A large neighbourhood: what the limit left out, and Show more up to the most one view reads. */
export function KgTruncation({ shown, hidden, level, onMore, maxed, max = 200, counts }: {
  shown: number; hidden: number; level: string; onMore?: () => void; maxed: boolean; max?: number;
  /** The Focus's one count source (lib/kg-view.ts focusCounts): wins over shown/hidden. */
  counts?: { total: number; shown: number; noun: string; maxed: boolean } | null;
}) {
  const noun = counts?.noun ?? (level === "class" ? "classes" : level === "package" ? "packages" : level === "method" ? "symbols" : "nodes");
  if (counts) { shown = counts.shown; hidden = counts.total - counts.shown; maxed = counts.maxed; }
  const total = (shown + hidden).toLocaleString("en-US");
  // Short, so the bar stays one line beside Built · Rebuild; the whole sentence is its tooltip.
  const full = `Showing the ${shown.toLocaleString("en-US")} most connected of ${total} connected ${noun}${maxed ? `. ${max} is the most one view reads: narrow it with layers or hops.` : "."}`;
  return (
    <p className="kg-trunc" data-testid="kg-truncated" role="status" title={full}>
      {!maxed && onMore ? <><button type="button" className="inf-link" data-testid="kg-show-more" onClick={onMore}>Show {max}</button> · </> : null}
      Showing {shown.toLocaleString("en-US")} of {total} connected {noun}{maxed ? ` · ${max} is the most one view reads` : ""}
    </p>
  );
}

/** Locked (entitlement): the add-on, explained; never a broken button. */
export function KgLocked({ reason }: { reason: string }) {
  return (
    <StageCard testId="kg-locked-state" title="Rules and Learning are an add-on"
               body="The code graph stays fully available. Rules agents propose, and what they learn while they work, are part of the Knowledge add-on.">
      <LockedPanel label="Locked" reason={reason} testId="kg-locked" />
    </StageCard>
  );
}

/** Archived project: read-only, said once at the top. */
export function KgArchived() {
  return (
    <p className="kg-banner" data-tone="amber" role="note" data-testid="kg-archived">
      This project is archived. Its graph stays readable; scans, rule review and indexing are off until it is restored.
    </p>
  );
}
