"use client";

/**
 * Observability › Installs — the collector's dashboard, shown only in Collector mode
 * (docs/product/RUN_SCORECARDS.md, "Collector"). For the product AND for investors:
 *
 *   PROOF       "312 active installs this month, 41% still active after 30 days"     [CSV]
 *   ACTIVE      daily · weekly · monthly · all time
 *   NEW / WEEK  bars, 12 weeks           RETENTION  weekly cohorts × weeks since first seen
 *   ADOPTION    version · edition · OS · CPU · install method
 *   OPTED IN    usage scale (at least, and the spread) · ship rate and reasons · first-week funnel
 *               · scorecards by harness, model and role · error classes
 *
 * Only what installs sent: heartbeats from everyone who has not opted out, the rest only from
 * installs that turned Improve BOSS on. Nothing here is a guess.
 */
import React from "react";

import { improveHref, polledImprove, type CollectorView, type KeyN } from "../../lib/improve";
import { usePolledRead } from "../../lib/log-stream";
import { useConsoleLive } from "../../lib/console-live";
import type { ObsTabProps } from "./obs-home";
import { Empty, Skeleton } from "../infra/kit";
import "../improve/improve.css";

const SCALE_WORDS: Record<string, string> = {
  projects: "Projects", active_projects: "Active projects", people: "People", agents_builtin: "Built-in agents",
  agents_custom: "Custom agents", workflows_run: "Workflow runs", builds: "Builds", prs_merged: "PRs merged", deploys: "Deploys",
};
const FUNNEL_WORDS: Record<string, string> = {
  setup: "Model connected", first_build: "First build", first_pr: "First PR", first_merge: "First merge", first_deploy: "First deploy",
};
const CHIP_WORDS: Record<string, string> = {
  wrong_scope: "Wrong scope", poor_quality: "Poor quality", broke_something: "Broke something", too_slow: "Too slow",
  too_expensive: "Too expensive", misread_brief: "Misread brief", shallow: "Shallow", wrong_market: "Wrong market",
  great_sources: "Great sources", changed_my_mind: "Changed my mind", missed_requirements: "Missed requirements",
  over_scoped: "Over-scoped", good_debate: "Good debate",
};

function Bars({ rows, label, words, testId }: { rows: KeyN[]; label: string; words?: Record<string, string>; testId: string }) {
  const max = Math.max(1, ...rows.map((r) => r.n));
  if (!rows.length) return <span className="im-muted">None yet</span>;
  return (
    <div className="im-bars" role="list" aria-label={label} data-testid={testId}>
      {rows.map((r) => (
        <div key={r.key} className="im-bar" role="listitem">
          <span data-user-text="" title={r.key}>{words?.[r.key] ?? r.key}</span>
          <svg viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden="true">
            <rect className="im-bar-track" x={0} y={0} width={100} height={10} rx={3} />
            <rect className="im-bar-fill" x={0} y={0} width={(r.n / max) * 100} height={10} rx={3} />
          </svg>
          <span>{r.n}</span>
        </div>
      ))}
    </div>
  );
}

/** "claude_code · 0.82": the group with its mean score, so a bar's length is never the only fact. */
function scored(rows: { key: string; name?: string; score: number | null }[]): Record<string, string> {
  return Object.fromEntries(rows.map((r) => [r.key, `${r.name ?? r.key} · ${r.score === null ? "—" : r.score.toFixed(2)}`]));
}

function Panel({ label, tip, children, testId }: { label: string; tip?: string; children: React.ReactNode; testId?: string }) {
  return (
    <section className="im-panel" data-testid={testId}>
      <h4 className="im-panel-k" title={tip}>{label}</h4>
      {children}
    </section>
  );
}

export function InstallsTab({ projectId }: ObsTabProps) {
  const live = useConsoleLive();
  const read = usePolledRead<CollectorView>({
    key: projectId, pollMs: 120_000, paused: !live, read: polledImprove<CollectorView>("collector", { project_id: projectId }),
  });
  const v = read.data;
  if (read.error && !v) {
    return <Empty testId="in-off"><span title={read.error}>Collector mode is off</span>
      <a className="inf-link" href="/?area=budget&tab=sharing">Govern ↗</a></Empty>;
  }
  if (!v) return <Skeleton testId="in-loading" />;
  const heat = (p: number) => (p >= 60 ? "3" : p >= 30 ? "2" : p > 0 ? "1" : undefined);
  return (
    <div className="im-sc" data-testid="in">
      <div className="im-proof" data-testid="in-proof">
        <span className="im-proof-k" title="From heartbeats; installs that opted out are not counted">PROOF</span>
        <h3 className="im-proof-v" data-user-text="">{v.proof}</h3>
        <span className="im-controls">
          <a className="ag-btn" href={improveHref("collector_csv", { project_id: projectId })} download data-testid="in-csv">Export CSV</a>
        </span>
      </div>
      <div className="im-tiles" data-testid="in-active">
        <div className="im-tile" title="Seen in the last day"><span className="im-tile-k">Daily</span><span className="im-tile-v">{v.active.daily}</span></div>
        <div className="im-tile" title="Seen in the last 7 days"><span className="im-tile-k">Weekly</span><span className="im-tile-v">{v.active.weekly}</span></div>
        <div className="im-tile" title="Seen in the last 30 days"><span className="im-tile-k">Monthly</span><span className="im-tile-v">{v.active.monthly}</span></div>
        <div className="im-tile" title="Every install id ever seen (18 months kept)"><span className="im-tile-k">All time</span><span className="im-tile-v">{v.total_installs}</span></div>
        <div className="im-tile" title="Installs that turned Improve BOSS on, last 30 days"><span className="im-tile-k">Opted in</span><span className="im-tile-v">{v.opted_in}</span></div>
        {v.held_back ? (
          <div className="im-tile" title="Too many new ids from one address in a day: stored, not counted" data-testid="in-held">
            <span className="im-tile-k">Held back</span><span className="im-tile-v">{v.held_back}</span></div>
        ) : null}
      </div>
      <div className="im-grid2">
        <Panel label="NEW PER WEEK" tip="First seen, by week, last 12 weeks" testId="in-new">
          <Bars rows={v.new_per_week.map((r) => ({ key: r.week.slice(5), n: r.n }))} label="New installs per week" testId="in-weeks" />
        </Panel>
        <Panel label="RETENTION" tip="Share of each weekly cohort seen again, week by week" testId="in-cohorts">
          {v.cohorts.length ? (
            <div className="im-scroll" tabIndex={0} aria-label="Retention by weekly cohort" data-testid="in-cohorts-scroll">
            <table className="im-cohorts">
              <thead><tr><th scope="col">Cohort</th><th scope="col">N</th>
                {[0, 1, 2, 3, 4, 5, 6, 7].map((k) => <th key={k} scope="col">W{k}</th>)}</tr></thead>
              <tbody>
                {v.cohorts.map((c) => (
                  <tr key={c.week}><th scope="row">{c.week.slice(5)}</th><td>{c.size}</td>
                    {c.pct.map((p, k) => <td key={k} data-heat={heat(p)}>{p ? `${p}%` : "—"}</td>)}</tr>
                ))}
              </tbody>
            </table>
            </div>
          ) : <span className="im-muted">None yet</span>}
        </Panel>
        <Panel label="VERSIONS" testId="in-versions"><Bars rows={v.versions} label="Versions" testId="in-versions-bars" /></Panel>
        <Panel label="EDITION · METHOD"><Bars rows={[...v.editions, ...v.methods]} label="Edition and install method" testId="in-editions" /></Panel>
        <Panel label="OS · CPU"><Bars rows={[...v.os, ...v.arch]} label="OS and CPU type" testId="in-os" /></Panel>
        <Panel label="USAGE SCALE" tip="Opted-in installs. Ranges summed from their lower ends: at least this many." testId="in-scale">
          <Bars rows={Object.entries(v.scale).map(([k, s]) => ({ key: k, n: s.at_least }))} words={SCALE_WORDS} label="Usage scale, at least" testId="in-scale-bars" />
        </Panel>
        <Panel label="SHIP RATE" tip="Would you ship this? Yes, of every build rated" testId="in-ship">
          {v.ship.rated ? (
            <>
              <span className="im-tile-v">{v.ship.rate === null ? "—" : `${Math.round(v.ship.rate * 100)}%`}</span>
              <span className="im-muted" title="Yes · with changes · no, of every build rated">
                {v.ship.yes} · {v.ship.with_changes} · {v.ship.no} of {v.ship.rated}
              </span>
              <Bars rows={v.chips} words={CHIP_WORDS} label="Reasons" testId="in-chips" />
            </>
          ) : <span className="im-muted">No ratings yet</span>}
        </Panel>
        <Panel label="FIRST WEEK" tip="Opted-in installs that reached each step" testId="in-funnel">
          <Bars rows={v.funnel} words={FUNNEL_WORDS} label="Funnel" testId="in-funnel-bars" />
        </Panel>
        <Panel label="BY HARNESS" tip="Builds: count; score and merge rate in the title" testId="in-harness">
          <Bars rows={v.by.harness.map((r) => ({ key: r.key, n: r.n }))} words={scored(v.by.harness)} label="Builds by harness" testId="in-harness-bars" />
        </Panel>
        <Panel label="BY MODEL" testId="in-model">
          <Bars rows={v.by.model.map((r) => ({ key: r.key, n: r.n }))} words={scored(v.by.model)} label="Builds by model" testId="in-model-bars" />
        </Panel>
        <Panel label="BY ROLE" tip="Custom roles are counted as custom" testId="in-role">
          <Bars rows={v.by.role_key.map((r) => ({ key: r.key, n: r.n }))} words={scored(v.by.role_key)} label="Builds by role" testId="in-role-bars" />
        </Panel>
        <Panel label="ERRORS" testId="in-errors"><Bars rows={v.errors} label="Error classes" testId="in-errors-bars" /></Panel>
        <Panel label="INSTALLER" tip="Installer step failures are not reported yet: install.sh sends nothing" testId="in-installer">
          <span className="im-muted">Not reported</span>
        </Panel>
      </div>
    </div>
  );
}
