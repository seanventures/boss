"use client";

/**
 * OBSERVABILITY › APPS AS ROWS (BOSSAPP-914 screen 15, 15-03…15-08): every app of every project
 * you may read, one row each —
 *
 *   [HD]  HelpDesk AI Demo                       ╱╲╱‾‾╲  [0 firing]  [♥ Project › Health]
 *         staging · 99.9% · p95 180ms
 *
 * the project's mark and name (the app's own name too when it differs), the environment, mono
 * uptime · p95 over the window, a 140 px sparkline of the window's latency (green healthy, amber
 * degraded, red down), the alerts firing on it now, and the link to that project's Health tab
 * (which links back to "Observability › this app"). A row opens the app drawer (Alerts with every
 * rule action, and Overview — unchanged, §13); an app of another project opens there.
 *
 * Reads GET /v1/observability/apps/all (one row per app across the projects you may read).
 * The Pulse (the 24-hour strip with deploy pins and its table) stays one press away: "Pulse".
 */
import React from "react";
import { Heartbeat } from "@phosphor-icons/react";

import { usePolled, type Polled } from "../../lib/factory-floor";
import { ms, pct, parsePulseStrip, type PulseCell, type Win } from "../../lib/obs-view";
import { plainClick } from "../agents/live-util";
import { leaveTo } from "./obs-util";
import { Empty, Skeleton } from "../infra/kit";

export type AppRow = {
  targetId: string; app: string; env: string; project: { id: string; name: string; initials: string };
  state: string; word: string; uptime: number | null; p95Ms: number | null; strip: PulseCell[]; firing: number;
  healthHref: string; drawerHref: string;
};
export type AppsAll = { rows: AppRow[]; projects: number; partial: string[];
  counts: { up: number; slow: number; down: number; noSignal: number; notDeployed: number } };

const obj = (v: unknown): Record<string, unknown> => (v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {});
const str = (v: unknown, d = "") => (typeof v === "string" ? v : d);
const nn = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : null);

export function parseAppsAll(w: unknown): AppsAll {
  const o = obj(w);
  const rows = (Array.isArray(o.rows) ? o.rows : []).map((r) => {
    const x = obj(r); const p = obj(x.project);
    return {
      targetId: str(x.target_id), app: str(x.app), env: str(x.env),
      project: { id: str(p.id), name: str(p.name, str(p.id)), initials: str(p.initials) },
      state: str(x.state, "unknown"), word: str(x.word, "No signal"), uptime: nn(x.uptime), p95Ms: nn(x.p95_ms),
      strip: parsePulseStrip(x.strip), firing: nn(x.firing) ?? 0, healthHref: str(x.health_href), drawerHref: str(x.drawer_href),
    };
  }).filter((r) => r.targetId);
  const c = obj(o.counts);
  return { rows, projects: nn(o.projects) ?? 0, partial: (Array.isArray(o.partial) ? o.partial : []).map((x) => str(x)),
           counts: { up: nn(c.up) ?? 0, slow: nn(c.slow) ?? 0, down: nn(c.down) ?? 0, noSignal: nn(c.no_signal) ?? 0, notDeployed: nn(c.not_deployed) ?? 0 } };
}

/** Each project's mark keeps one of four gradients (the wireframe's HD / NP / HW marks; obs.css). */
export const markOf = (id: string) => String([...id].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7) % 4);

export const appsAllUrl = (win: Win) => `/v1/observability/apps/all?win=${encodeURIComponent(win)}`;

/** The window's latency as a 140 px line (falls back to the share of good answers). */
export function AppSpark({ strip, state, label }: { strip: PulseCell[]; state: string; label: string }) {
  const W = 120; const H = 26;
  const lat = strip.map((c) => c.p95);
  const usable = lat.filter((v): v is number => v !== null).length >= 2;
  const values = usable ? lat.map((v, i) => v ?? (i ? lat[i - 1] ?? 0 : 0)) as number[]
    : strip.map((c) => (c.n ? c.ok / c.n : 0));
  const have = strip.some((c) => c.n > 0);
  const tone = state === "down" ? "red" : state === "degraded" ? "amber" : state === "up" ? "green" : "grey";
  if (!have) {
    return <svg className="obs-arow-spark" width="140" height="26" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label={`${label}: no checks in this window`} data-tone="grey">
      <line x1={0} x2={W} y1={H - 4} y2={H - 4} strokeDasharray="3 3" /></svg>;
  }
  const max = Math.max(...values); const min = Math.min(...values); const span = max - min || 1;
  const step = values.length > 1 ? W / (values.length - 1) : 0;
  // Latency: higher is worse, drawn higher; the share of good answers is drawn inverted the same way.
  const y = (v: number) => (usable ? H - 3 - ((v - min) / span) * (H - 6) : 3 + ((max - v) / span) * (H - 6));
  const points = values.map((v, i) => `${(i * step).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return (
    <svg className="obs-arow-spark" width="140" height="26" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" data-tone={tone}
         aria-label={`${label}: ${usable ? `p95 from ${Math.round(min)} to ${Math.round(max)} ms` : "share of good answers"} over the window`}>
      <polyline fill="none" points={points} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export const useAppsAll = (win: Win) => usePolled(appsAllUrl(win), parseAppsAll, () => 30_000);

export function AppsRows({ all, query, currentProject, onOpen }: {
  all: Polled<AppsAll>; query: string; currentProject: string; onOpen: (targetId: string) => void;
}) {
  if (all.loading && !all.data) return <Skeleton testId="obs-arows-loading" />;
  if (!all.data && all.error) {
    return (
      <Empty testId="obs-arows-error">
        Couldn&apos;t load the apps{" "}
        <button type="button" className="inf-link" onClick={() => all.refresh()} data-testid="obs-arows-retry">Retry</button>
      </Empty>
    );
  }
  const q = query.trim().toLowerCase();
  const rows = (all.data?.rows ?? []).filter((r) => !q || `${r.project.name} ${r.app} ${r.env}`.toLowerCase().includes(q));
  if (!all.data?.rows.length) return <Empty testId="obs-arows-empty">No apps deployed yet</Empty>;
  if (!rows.length) return <Empty testId="obs-arows-nomatch">Nothing matches</Empty>;
  return (
    <section className="obs-arows" aria-label="Apps" data-testid="obs-arows">
      <ul className="obs-arows-list">
        {rows.map((r) => {
          const tone = r.state === "down" ? "red" : r.state === "degraded" || r.firing ? "amber" : undefined;
          const name = r.project.name;
          const sameName = name.toLowerCase().replace(/[^a-z0-9]/g, "") === r.app.toLowerCase().replace(/[^a-z0-9]/g, "");
          const open = (e: React.MouseEvent) => {
            if ((e.target as HTMLElement).closest("a")) return;
            if (r.project.id === currentProject) onOpen(r.targetId); else leaveTo(r.drawerHref);
          };
          const metrics = [r.uptime !== null ? pct(r.uptime) : null, r.p95Ms !== null ? `p95 ${ms(r.p95Ms)}` : null].filter(Boolean).join(" · ");
          return (
            <li key={r.targetId} className="obs-arow" data-state={r.state} data-tone={tone} data-testid={`obs-arow-${r.targetId}`}
                onClick={open} title={`Open ${name} · ${r.env}`}>
              <span className="obs-arow-mark" aria-hidden data-mark={markOf(r.project.id)}>{r.project.initials || name.slice(0, 2).toUpperCase()}</span>
              <span className="obs-arow-main">
                <button type="button" className="obs-arow-t" data-testid={`obs-arow-open-${r.targetId}`}>
                  {name}{sameName ? null : <span className="obs-arow-app"> · {r.app}</span>}</button>
                <span className="obs-arow-sub">
                  <span>{r.env}</span>
                  {metrics ? <span className="obs-mono" data-testid={`obs-arow-metrics-${r.targetId}`}>{metrics}</span>
                    : <span className="obs-faint">{r.state === "not_deployed" ? "not deployed" : "no checks yet"}</span>}
                  <span className="obs-arow-word" data-tone={tone}>{r.word}</span>
                </span>
              </span>
              <span className="obs-arow-sparkcell"><AppSpark strip={r.strip} state={r.state} label={`${name} ${r.env}`} /></span>
              <span className={`obs-arow-tag`} data-tone={r.firing ? (r.state === "down" ? "red" : "amber") : "green"}
                    data-testid={`obs-arow-firing-${r.targetId}`} title={r.firing ? "Alerts firing now" : "No alert firing"}>
                {r.firing} firing</span>
              <span className="obs-arow-links">
                <a className="obs-chip obs-project-health" href={r.healthHref} data-testid="obs-app-project-health"
                   title={`Project › Health — ${name}'s alerts, incidents and monitoring`}
                   onClick={(e) => { if (plainClick(e)) { e.preventDefault(); leaveTo(r.healthHref); } }}>
                  <Heartbeat size={12} aria-hidden /><span className="obs-project-health-word">Project › Health</span></a>
              </span>
            </li>
          );
        })}
      </ul>
      {all.data.partial.length ? <span className="obs-faint obs-arows-partial" data-testid="obs-arows-partial">Some projects unreadable just now</span> : null}
    </section>
  );
}
