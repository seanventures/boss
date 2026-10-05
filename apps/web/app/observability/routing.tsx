"use client";

/**
 * ROUTING (spec §4.10; was System Configuration › AI Capacity › Model route decisions): which
 * source each model call was routed to, or why it was refused. Four dropdown chips — Provider,
 * Budget, Boundary, Outcome — replace the old GET form; their options come from the view. With
 * no filter the view's own 8 rows show; a filter (or `Show all`, a sheet that pages) reads
 * GET /v1/model-route-decisions with the same four filters.
 */
import React, { useEffect, useState } from "react";

import type { RoutingRow } from "../../lib/obs-view";
import { Empty, Sheet, ZoneHead } from "../infra/kit";
import { obsRead } from "./obs-write";
import { useLocalTime } from "../../lib/local-time";

export type RouteFilters = { provider: string; budget: string; boundary: string; outcome: string };
const NONE: RouteFilters = { provider: "", budget: "", boundary: "", outcome: "" };
const CHIPS: { key: keyof RouteFilters; label: string }[] = [
  { key: "provider", label: "Provider" }, { key: "budget", label: "Budget" },
  { key: "boundary", label: "Boundary" }, { key: "outcome", label: "Outcome" },
];
const SHOWN = 8;
const SHEET_PAGE = 25;

/** The route-decisions query for these filters (Outcome maps to the API's selected/denied flags). */
export function routesQuery(projectId: string, f: RouteFilters, limit: number): string {
  const p = new URLSearchParams({ project_id: projectId, limit: String(limit) });
  if (f.provider) p.set("provider", f.provider);
  if (f.budget) p.set("budget_state", f.budget);
  if (f.boundary) p.set("data_boundary", f.boundary);
  if (f.outcome === "selected") p.set("selected_only", "true");
  if (f.outcome === "denied") p.set("denied_only", "true");
  return `/v1/model-route-decisions?${p.toString()}`;
}

/** One ModelRouteDecisionSummary (the list route) as a routing row. */
export function routeRow(w: Record<string, unknown>): RoutingRow {
  const s = (v: unknown) => (typeof v === "string" && v ? v : null);
  return {
    id: String(w.id ?? ""), at: String(w.created_at ?? ""), provider: s(w.selected_provider), budget: s(w.budget_state),
    boundary: s(w.data_boundary),
    outcome: w.denial_reason ? "denied" : w.selected_source_id ? "selected" : "none", model: s(w.selected_model_id),
  };
}

async function readRoutes(projectId: string, f: RouteFilters, limit: number): Promise<RoutingRow[] | null> {
  const body = await obsRead<{ items?: Record<string, unknown>[] }>(routesQuery(projectId, f, limit));
  return body ? (body.items ?? []).map(routeRow) : null;
}

const word = (v: string | null) => (v ? v.replace(/[_-]/g, " ") : "—");

/** One outcome language for both sources: the Models view sends the server's word (`Routed`,
 *  `Denied`, `No route`), the full list route a key (`selected` / `denied`). */
export function outcomeOf(v: string | null | undefined): { key: "selected" | "denied" | "none"; word: string } {
  const o = (v ?? "").trim().toLowerCase();
  if (o === "denied" || o === "refused") return { key: "denied", word: "Refused" };
  if (o === "selected" || o === "routed") return { key: "selected", word: "Routed" };
  return { key: "none", word: "No route" };
}

function RouteLine({ r }: { r: RoutingRow }) {
  const lt = useLocalTime();
  const t = Date.parse(r.at);
  const out = outcomeOf(r.outcome);
  return (
    <li className="obs-line obs-route" data-testid="obs-route-row" data-outcome={out.key}>
      <span className="inf-num inf-muted" title={Number.isFinite(t) ? lt.full(t) : undefined}>
        {Number.isFinite(t) ? lt.hhmm(t) : "—"}
      </span>
      <span data-copy="data">{word(r.provider)}</span>
      <span className="inf-muted" title="Budget state">{word(r.budget)}</span>
      <span className="inf-muted" title="Data boundary">{word(r.boundary)}</span>
      <span className={out.key === "denied" ? "obs-bad" : out.key === "selected" ? "obs-ok" : "inf-muted"}>{out.word}</span>
      {r.model ? <span className="inf-mono inf-faint inf-trunc" data-copy="data">{r.model}</span> : null}
    </li>
  );
}

export function Routing({ projectId, rows, total, options }: {
  projectId: string; rows: RoutingRow[]; total: number;
  options: { provider: string[]; budget: string[]; boundary: string[]; outcome: string[] };
}) {
  const [f, setF] = useState<RouteFilters>(NONE);
  const [filtered, setFiltered] = useState<RoutingRow[] | null>(null);
  const [all, setAll] = useState(false);
  const active = Object.values(f).some(Boolean);
  useEffect(() => {
    if (!active) { setFiltered(null); return; }
    let live = true;
    void readRoutes(projectId, f, SHOWN).then((r) => { if (live) setFiltered(r ?? []); });
    return () => { live = false; };
  }, [projectId, f, active]);
  const shown = (active ? filtered ?? [] : rows).slice(0, SHOWN);

  return (
    <section className="obs-zone" aria-labelledby="obs-routing-head" data-testid="obs-routing">
      <ZoneHead id="obs-routing-head" label="ROUTING" count={total} tip="Which source each model call was routed to">
        <span className="obs-chips">
          {CHIPS.map((c) => (
            <label key={c.key} className="obs-dropchip" data-on={f[c.key] ? "1" : undefined}>
              <span className="ag-sr">{c.label}</span>
              <select value={f[c.key]} data-testid={`obs-route-${c.key}`} aria-label={c.label}
                      onChange={(e) => setF((v) => ({ ...v, [c.key]: e.target.value }))}>
                <option value="">{c.label}</option>
                {(c.key === "outcome" && !options.outcome.length ? ["selected", "denied"] : options[c.key]).map((o) => (
                  <option key={o} value={o}>{c.key === "outcome" ? (o === "denied" ? "Refused" : "Routed") : word(o)}</option>
                ))}
              </select>
            </label>
          ))}
          {active ? <button type="button" className="inf-link" data-testid="obs-route-clear" onClick={() => setF(NONE)}>Clear</button> : null}
        </span>
      </ZoneHead>
      {shown.length === 0 ? (
        <Empty testId="obs-routing-empty">{active ? "Nothing matches" : "No routing yet"}</Empty>
      ) : (
        <ul className="obs-list">{shown.map((r) => <RouteLine key={r.id} r={r} />)}</ul>
      )}
      {total > SHOWN || (active && (filtered?.length ?? 0) >= SHOWN) ? (
        <button type="button" className="inf-link" data-testid="obs-route-all" onClick={() => setAll(true)}>Show all</button>
      ) : null}
      {all ? <AllRoutes projectId={projectId} f={f} onClose={() => setAll(false)} /> : null}
    </section>
  );
}

function AllRoutes({ projectId, f, onClose }: { projectId: string; f: RouteFilters; onClose: () => void }) {
  const [limit, setLimit] = useState(SHEET_PAGE);
  const [rows, setRows] = useState<RoutingRow[] | null>(null);
  useEffect(() => {
    let live = true;
    void readRoutes(projectId, f, limit).then((r) => { if (live) setRows(r ?? []); });
    return () => { live = false; };
  }, [projectId, f, limit]);
  return (
    <Sheet title="Routing" testId="obs-routing-sheet" onClose={onClose}>
      {rows === null ? <span className="inf-faint">Loading…</span> : rows.length === 0 ? <Empty>No routing yet</Empty> : (
        <ul className="obs-list">{rows.map((r) => <RouteLine key={r.id} r={r} />)}</ul>
      )}
      {rows && rows.length >= limit && limit < 200 ? (
        <button type="button" className="ag-btn" data-testid="obs-route-more" onClick={() => setLimit((n) => Math.min(200, n + SHEET_PAGE))}>Show more</button>
      ) : null}
    </Sheet>
  );
}
