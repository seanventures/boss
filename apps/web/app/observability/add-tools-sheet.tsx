"use client";

/**
 * `Add tools…` (spec §4.13; was the Observability suite's "Recommended OSS stack" and
 * "Recommend for workload"): pick a workload, `Recommend` names the components for it, which
 * the BOSS stack already covers (`In BOSS stack`) and what to add alongside, and the whole OSS
 * catalogue sits below in the same sheet. Reads GET /v1/observability-suite/catalog; Recommend
 * is POST /v1/observability-suite/recommend (it changes nothing).
 */
import { GatedButton } from "../../components/gated";
import React, { useEffect, useState } from "react";

import type { OssComponent, ObservabilitySuiteCatalog, RecommendResult, WorkloadProfile } from "../../lib/observability-suite";
import { Sheet } from "../infra/kit";
import { obsRead, obsWrite } from "./obs-write";

/** The five workloads, when the catalogue does not list its own. */
export const DEFAULT_WORKLOADS: WorkloadProfile[] = [
  { id: "minimal", title: "Minimal", description: "", component_ids: [] },
  { id: "web", title: "Web / API", description: "", component_ids: [] },
  { id: "microservices", title: "Microservices", description: "", component_ids: [] },
  { id: "k8s", title: "Kubernetes", description: "", component_ids: [] },
  { id: "enterprise", title: "Enterprise", description: "", component_ids: [] },
];

function ToolCard({ c }: { c: OssComponent }) {
  return (
    <li className="obs-tool" data-testid="obs-tool-card">
      <span className="obs-line">
        <span className="obs-strong" data-copy="data">{c.name}</span>
        <span className="obs-chip" data-role={c.role}>{c.role}</span>
        {c.deployed_by_boss_stack ? <span className="obs-chip" data-tone="green" title="The BOSS stack deploys this">In BOSS stack</span> : null}
      </span>
      <span className="inf-muted obs-tool-purpose inf-trunc" title={c.purpose} data-copy="data">{c.purpose}</span>
    </li>
  );
}

export function AddToolsSheet({ onClose }: { onClose: () => void }) {
  const [catalog, setCatalog] = useState<ObservabilitySuiteCatalog | null>(null);
  const [failed, setFailed] = useState(false);
  const [profile, setProfile] = useState("web");
  const [result, setResult] = useState<RecommendResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    void obsRead<ObservabilitySuiteCatalog>("/v1/observability-suite/catalog").then((c) => {
      if (!live) return;
      setCatalog(c);
      setFailed(c === null);
    });
    return () => { live = false; };
  }, []);
  const profiles = catalog?.profiles?.length ? catalog.profiles : DEFAULT_WORKLOADS;
  const items = catalog?.items ?? [];
  const recommend = async () => {
    setBusy(true);
    setError(null);
    const r = await obsWrite<RecommendResult>("/v1/observability-suite/recommend", { workload_profile: profile },
                                              { scope: "obs:recommend", label: "Recommend" });
    setBusy(false);
    if (r.ok && r.body) setResult(r.body); else setError(r.error ?? "No answer");
  };
  const picked = result ? (result.components?.length ? result.components : items.filter((c) => result.component_ids.includes(c.id))) : [];

  return (
    <Sheet title="Add tools" testId="obs-tools-sheet" onClose={onClose}>
      <div className="obs-line obs-tools-pick">
        <label className="inf-field obs-inline-field">
          <span>Workload</span>
          <select value={profile} data-testid="obs-tools-workload" onChange={(e) => { setProfile(e.target.value); setResult(null); }}>
            {profiles.map((p) => <option key={p.id} value={p.id}>{p.title || p.id}</option>)}
          </select>
        </label>
        <GatedButton permission="observability:read" type="button" className="ag-btn ag-btn-primary" data-testid="obs-tools-recommend" disabled={busy} onClick={recommend}>
          {busy ? "Recommending…" : "Recommend"}
        </GatedButton>
        {error ? <span className="inf-error" title={error}>Not answered</span> : null}
      </div>
      {result ? (
        <div className="obs-tools-result" data-testid="obs-tools-result">
          <span className="obs-strong" title={result.description} data-copy="data">{result.title}</span>
          <ul className="obs-tools">{picked.map((c) => <ToolCard key={c.id} c={c} />)}</ul>
          {result.add_alongside_boss_stack?.length ? (
            <span className="obs-line" data-testid="obs-tools-alongside">
              <span className="inf-muted">Add alongside</span>
              <span data-copy="data">{result.add_alongside_boss_stack.join(", ")}</span>
            </span>
          ) : null}
        </div>
      ) : null}
      <section className="obs-zone" aria-label="Catalogue" data-testid="obs-tools-catalog">
        <h3 className="ag-zone-label">CATALOGUE</h3>
        {catalog === null && !failed ? <span className="inf-faint">Loading…</span>
          : failed ? <span className="inf-faint">Catalogue unavailable</span>
            : <ul className="obs-tools">{items.map((c) => <ToolCard key={c.id} c={c} />)}</ul>}
      </section>
    </Sheet>
  );
}
