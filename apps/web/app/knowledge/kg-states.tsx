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

const LAYER_LINE: Record<(typeof KG_LAYERS)[number], { name: string; what: string }> = {
  code: { name: "Code", what: "indexed from the repository" },
  rules: { name: "Rules", what: "proposed by agents, approved by people" },
  learning: { name: "Learning", what: "written by agents as they work" },
};

/** Faint hop rings behind a card: the Focus view's motif, drawn in tokens. */
export function Rings() {
  return (
    <svg className="kg-stage-rings" viewBox="0 0 800 460" preserveAspectRatio="xMidYMid slice" aria-hidden focusable="false">
      <circle cx="400" cy="230" r="120" />
      <circle cx="400" cy="230" r="210" />
      <circle cx="400" cy="230" r="300" />
    </svg>
  );
}

export function Layers() {
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

export function StageCard({ testId, eyebrow, title, body, children, layers = false, tone }: {
  testId: string; eyebrow: string; title: string; body: React.ReactNode; children?: React.ReactNode; layers?: boolean;
  tone?: "red" | "amber";
}) {
  return (
    <div className="kg-stage" data-testid={testId} data-tone={tone}>
      <Rings />
      <div className="kg-empty" role="status">
        <span className="kg-empty-eyebrow">{eyebrow}</span>
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
    <StageCard testId="kg-empty-graph" eyebrow="Graph" layers
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
    <StageCard testId="kg-indexing" eyebrow="Graph" tone={failed.length && !working ? "amber" : undefined}
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

/** Empty layer: e.g. only the Rules chip on, and no rule exists yet. */
export function KgEmptyLayer({ layer, onScan }: { layer: "rules" | "learning"; onScan?: () => void }) {
  return layer === "rules" ? (
    <StageCard testId="kg-empty-layer" eyebrow="Rules" title="No rules yet"
               body="Scan an area for business rules: Scout reads its code within a budget you set and proposes the rules it finds, each with the lines that show it. You approve them in Rules review.">
      {onScan ? <button type="button" className="ag-btn ag-btn-primary" data-testid="kg-empty-layer-scan" onClick={onScan}>Choose an area to scan</button> : null}
    </StageCard>
  ) : (
    <StageCard testId="kg-empty-layer" eyebrow="Learning" title="Nothing learned yet"
               body="Agents record what they try, rule out, find and fix while they work: test failures and their fixes, root causes, precedents. They appear here, linked to the code, within seconds." />
  );
}

/** A large neighbourhood: what the limit left out, and Show more (never more than 300). */
export function KgTruncation({ shown, hidden, level, onMore, maxed }: { shown: number; hidden: number; level: string; onMore?: () => void; maxed: boolean }) {
  const noun = level === "class" ? "classes" : level === "package" ? "packages" : level === "method" ? "symbols" : "nodes";
  return (
    <p className="kg-trunc" data-testid="kg-truncated" role="status">
      Showing {shown.toLocaleString("en-US")} of {(shown + hidden).toLocaleString("en-US")} connected {noun}, the most connected first
      {maxed ? " — 300 is the most one view draws; narrow it with layers or hops." : null}
      {!maxed && onMore ? <> · <button type="button" className="inf-link" data-testid="kg-show-more" onClick={onMore}>Show more</button></> : null}
    </p>
  );
}

/** Locked (entitlement): the add-on, explained; never a broken button. */
export function KgLocked({ reason }: { reason: string }) {
  return (
    <StageCard testId="kg-locked-state" eyebrow="Knowledge add-on" title="Rules and Learning are an add-on"
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
