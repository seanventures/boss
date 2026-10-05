"use client";

/**
 * Observability › Sources (spec §4.13): where the telemetry comes from.
 *
 *   STACK         OTel → Prometheus → Grafana · ● Running · boss-1 ↗ · prometheus: running …   [Redeploy] Grafana ↗
 *   SCRAPED APPS  App · Env · Scrape URL · Dashboard ↗ / No dashboard · Not watched
 *   SOURCES       Kind · Name · URL · Enabled                                          [Check config]
 *   GAPS          No metrics backend · No log backend · Nothing watched yet
 *                                                                                      [Add tools…]
 *
 * The stack's status is the server's (cached 60 s — never an SSH probe per poll). `Deploy
 * stack` / `Redeploy` posts the existing stack-deploy route and then re-polls this view (the old
 * panel never refreshed after a deploy). `Status unknown` offers `Check now`, which asks for a
 * fresh reading once (`fresh=1`). No machine: `Needs a machine` + `Add host ↗` into Infra.
 */
import React, { useState } from "react";

import { hostHref } from "../../lib/infra-view";
import { sourcesUrl, useSources, type Sources } from "../../lib/obs-view";
import { showToast } from "../client-forms";
import { notifyChanged } from "../changed";
import { ago, plainClick, useNow } from "../agents/live-util";
import type { ObsTabProps } from "./obs-home";
import { AddToolsSheet } from "./add-tools-sheet";
import { leaveTo } from "./obs-util";
import { obsRead, obsWrite } from "./obs-write";
import { Empty, Skeleton, StateWord, ZoneHead } from "../infra/kit";
import { useLocalTime } from "../../lib/local-time";

const CHECK_WORD: Record<string, { word: string; tone: "green" | "red" | "grey" }> = {
  config_ok: { word: "Config OK", tone: "green" }, reachable: { word: "Reachable", tone: "green" },
  misconfigured: { word: "Misconfigured", tone: "red" }, unreachable: { word: "Unreachable", tone: "red" },
};
type CheckItem = { id?: string; name?: string; kind?: string; status: string; config_error?: string; note?: string };

function Stack({ s, projectId, actor, refresh }: { s: Sources; projectId: string; actor: string; refresh: () => void }) {
  const lt = useLocalTime();
  const st = s.stack;
  const now = useNow(30_000);
  const [busy, setBusy] = useState<"deploy" | "check" | null>(null);
  const deploy = async () => {
    if (!st.host || busy) return;
    setBusy("deploy");
    const r = await obsWrite("/v1/observability/stack/deploy", { project_id: projectId, host_target_id: st.host.id, actor },
                             { scope: "obs:stack", label: st.state === "running" ? "Redeploy" : "Deploy stack" });
    setBusy(null);
    if (r.ok) { showToast("success", "Stack deploying"); notifyChanged("observability", { stack: "deploy" }); }
    else showToast("error", r.error ?? "Not deployed");
    refresh();
  };
  const check = async () => {
    setBusy("check");
    await obsRead(sourcesUrl(projectId, true));
    setBusy(null);
    refresh();
  };
  const unknown = st.state === "unknown" || Boolean(st.error);
  return (
    <section className="obs-zone" aria-labelledby="obs-stack-head" data-testid="obs-stack">
      <ZoneHead id="obs-stack-head" label="STACK">
        {st.canDeploy && st.host ? (
          <button type="button" className="ag-btn" data-testid="obs-stack-deploy" disabled={busy !== null} onClick={deploy}
                  title="Renders the scrape config from your targets and puts the stack on its machine">
            {busy === "deploy" ? "Deploying…" : st.state === "running" ? "Redeploy" : "Deploy stack"}
          </button>
        ) : null}
        {st.grafanaUrl ? (
          <a className="inf-link" href={st.grafanaUrl} target="_blank" rel="noreferrer noopener" data-testid="obs-stack-grafana">Grafana ↗</a>
        ) : null}
      </ZoneHead>
      <div className="obs-line obs-stackline">
        <span className="obs-strong">OTel → Prometheus → Grafana</span>
        <span data-testid="obs-stack-state" title={st.error ?? (st.checkedAt ? `Checked ${lt.full(st.checkedAt)}` : undefined)}>
          <StateWord w={{ word: unknown ? "Status unknown" : st.word, tone: unknown ? "hollow" : st.tone }} />
        </span>
        {st.host ? (
          <a className="inf-link" href={hostHref(st.host.id)} data-copy="data" title={`${st.host.name} in Infra`}
             onClick={(e) => { if (plainClick(e)) { e.preventDefault(); leaveTo(hostHref(st.host!.id)); } }}>{st.host.name} ↗</a>
        ) : null}
        {st.checkedAt && !unknown ? <span className="inf-faint inf-num" title={lt.full(st.checkedAt)}>checked {ago(st.checkedAt, now)}</span> : null}
        {unknown ? (
          <button type="button" className="inf-link" data-testid="obs-stack-check" disabled={busy !== null} onClick={check}>
            {busy === "check" ? "Checking…" : "Check now"}
          </button>
        ) : null}
        {st.needsMachine ? (
          <span className="obs-line" data-testid="obs-stack-needs-machine">
            <span className="inf-muted" title="The stack needs a machine to run on">Needs a machine</span>
            <a className="inf-link" href="?area=infra&view=fleet" data-testid="obs-stack-add-host"
               onClick={(e) => { if (plainClick(e)) { e.preventDefault(); leaveTo("?area=infra&view=fleet"); } }}>Add host ↗</a>
          </span>
        ) : null}
      </div>
      {st.containers.length ? (
        <span className="obs-chips" data-testid="obs-stack-containers">
          {st.containers.map((c) => (
            <span key={c.name} className="obs-chip" data-tone={c.status === "running" ? "green" : c.status ? "amber" : undefined} data-copy="data">
              {c.name}: {c.status}
            </span>
          ))}
        </span>
      ) : null}
    </section>
  );
}

function SourcesList({ s, projectId, actor }: { s: Sources; projectId: string; actor: string }) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ items: CheckItem[]; ok: number; count: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const check = async () => {
    setBusy(true);
    setError(null);
    const r = await obsWrite<{ items?: CheckItem[]; ok_count?: number; count?: number }>(
      "/v1/observability-suite/health-check", { project_id: projectId, actor, dry_run: true },
      { scope: "obs:check-config", label: "Check config" });
    setBusy(false);
    if (r.ok && r.body) setResult({ items: r.body.items ?? [], ok: r.body.ok_count ?? 0, count: r.body.count ?? 0 });
    else setError(r.error ?? "Not answered");
  };
  return (
    <section className="obs-zone" aria-labelledby="obs-sources-head" data-testid="obs-sources-list">
      <ZoneHead id="obs-sources-head" label="SOURCES" count={s.sources.length}>
        <button type="button" className="ag-btn" data-testid="obs-check-config" disabled={busy || !s.sources.length} onClick={check}
                title="Checks each source's settings. Doesn't call the source.">{busy ? "Checking…" : "Check config"}</button>
      </ZoneHead>
      {s.sources.length === 0 ? <Empty>No sources yet</Empty> : (
        <ul className="obs-list">
          {s.sources.map((x) => (
            <li key={x.id} className="obs-line" data-testid="obs-source-row">
              <span className="obs-chip">{x.kind}</span>
              <span className="obs-strong" data-copy="data">{x.name}</span>
              {x.url ? <span className="inf-mono inf-faint inf-trunc" title={x.url} data-copy="data">{x.url}</span> : <span className="inf-faint">—</span>}
              <span className="ag-spacer" />
              <span className={x.enabled ? "obs-ok" : "inf-faint"}>{x.enabled ? "Enabled" : "Disabled"}</span>
            </li>
          ))}
        </ul>
      )}
      {error ? <span className="inf-error" title={error}>Check failed</span> : null}
      {result ? (
        <div className="obs-checks" data-testid="obs-check-result">
          <span className="inf-muted inf-num">{result.ok} of {result.count} OK</span>
          <ul className="obs-list">
            {result.items.map((it, i) => {
              const w = CHECK_WORD[it.status] ?? { word: it.status.replace(/_/g, " "), tone: "grey" as const };
              return (
                <li key={it.id ?? i} className="obs-line" data-testid="obs-check-row">
                  <StateWord w={w} />
                  <span data-copy="data">{it.name ?? it.id}</span>
                  {it.kind ? <span className="inf-faint">{it.kind}</span> : null}
                  {it.config_error || it.note ? <span className="inf-muted inf-trunc" title={it.config_error || it.note} data-copy="data">{it.config_error || it.note}</span> : null}
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

export function SourcesTab({ projectId, actor }: ObsTabProps) {
  const sources = useSources(projectId);
  const s = sources.data;
  const [tools, setTools] = useState(false);
  if (sources.loading && !s) return <Skeleton testId="obs-sources-loading" />;
  if (!s) return <Empty testId="obs-sources-error">Can&apos;t load sources</Empty>;
  return (
    <div className="obs-sources" data-testid="obs-sources">
      <Stack s={s} projectId={projectId} actor={actor} refresh={sources.refresh} />

      <section className="obs-zone" aria-labelledby="obs-scraped-head" data-testid="obs-scraped">
        <ZoneHead id="obs-scraped-head" label="SCRAPED APPS" count={s.scraped.length} />
        {s.scraped.length === 0 ? <Empty>Nothing scraped yet</Empty> : (
          <div className="inf-table-wrap">
            <table className="inf-table">
              <thead><tr><th scope="col">App</th><th scope="col">Env</th><th scope="col">Scrape URL</th><th scope="col"><span className="ag-sr">Dashboard</span></th></tr></thead>
              <tbody>
                {s.scraped.map((a) => (
                  <tr key={`${a.app}|${a.env}`} data-testid="obs-scraped-row">
                    <td>
                      <span className="obs-gname">
                        <span data-copy="data" data-user-text>{a.app}</span>
                        {!a.watched ? <span className="obs-chip" data-tone="amber" title="Prometheus scrapes it, but no health check watches it">Not watched</span> : null}
                      </span>
                    </td>
                    <td data-copy="data">{a.env}</td>
                    <td><span className="inf-mono inf-trunc obs-url" title={a.scrapeUrl ?? undefined} data-copy="data">{a.scrapeUrl ?? "—"}</span></td>
                    <td>
                      {a.dashboardUrl ? <a className="inf-link" href={a.dashboardUrl} target="_blank" rel="noreferrer noopener">Dashboard ↗</a>
                        : <span className="inf-faint" title="Redeploy the stack to generate it">No dashboard</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <SourcesList s={s} projectId={projectId} actor={actor} />

      <section className="obs-zone" aria-labelledby="obs-gaps-head" data-testid="obs-gaps">
        <ZoneHead id="obs-gaps-head" label="GAPS" count={s.gaps.length} />
        {s.gaps.length === 0 ? <Empty>No gaps</Empty> : (
          <ul className="obs-list">
            {s.gaps.map((g) => (
              <li key={g.id} className="obs-line" data-testid="obs-gap">
                <span className="inf-dot" data-tone="amber" aria-hidden />
                <span title={g.tip}>{g.label}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <div className="obs-line obs-foot">
        <span className="ag-spacer" />
        <button type="button" className="ag-btn" data-testid="obs-add-tools" onClick={() => setTools(true)}
                title="Open-source tools to add, by workload">Add tools…</button>
      </div>
      {tools ? <AddToolsSheet onClose={() => setTools(false)} /> : null}
    </div>
  );
}
