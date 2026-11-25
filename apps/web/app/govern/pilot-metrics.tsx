"use client";

/**
 * Govern › More › Pilot metrics (BOSSAPP-1292): the numbers a guided preview is judged on, before
 * and after BOSS — PR open-to-approval time, regressions caught (held before merge, stopped after
 * it), post-deploy alerts and how many BOSS fixed on its own. Each number opens its sources (a
 * build, an incident). The BEFORE numbers come from the customer and are typed in here.
 *
 * Reads GET /v1/pilot-metrics (budget:read at org); downloads /v1/pilot-metrics/export as CSV or
 * JSON; writes POST /v1/pilot-metrics/baseline (budget:manage at org).
 */
import React, { useCallback, useEffect, useState } from "react";
import {
  ArrowClockwise, ArrowSquareOut, CaretDown, CaretUp, CheckCircle, DownloadSimple, FloppyDisk, PencilSimple, ShieldCheck,
  Siren, Timer, TrendDown, TrendUp, X,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { obsWrite } from "../observability/obs-write";
import type { Team } from "./scoped-budgets";

type ReviewItem = { pull_request_url: string | null; run_id: string; project_id: string; opened_at: string | null;
  approved_at: string | null; hours: number | null };
type BlockedItem = { run_id: string; project_id: string; title: string | null; at: string | null; gates_failed: string[] };
type AfterItem = { kind: string; entity_type?: string; entity_id?: string; run_id: string | null; change_set_id: string | null;
  project_id: string; at: string | null };
type AlertItem = { alert_id: string; project_id: string; alertname: string; fired_at: string | null; notified: boolean;
  incident_id: string | null; auto_remediated: boolean; fix_run_id: string | null; handled_by_person: boolean };
export type BaselineKey = "review_hours_median" | "regressions_caught_per_month" | "post_deploy_alerts_per_month" | "auto_remediated_pct";
export type PilotReport = {
  label: string; days: number; since: string; until: string;
  review: { opened: number; approved: number; waiting: number; median_hours: number | null; p90_hours: number | null;
    mean_hours: number | null; items: ReviewItem[] };
  regressions: { blocked_before_merge: number; stopped_after_merge: number; total: number;
    before_merge_items: BlockedItem[]; after_merge_items: AfterItem[] };
  post_deploy: { alerted: number; notified: number; auto_remediated: number; auto_remediated_pct: number | null; items: AlertItem[] };
  baseline: { values: Partial<Record<BaselineKey, number>>; note: string; updated_by: string | null; updated_at: string | null };
  after: Record<BaselineKey, number | null>;
};

const enc = encodeURIComponent;
export const WINDOWS = [7, 30, 90] as const;
/** Lower is better for every metric but the auto-remediated share. */
const HIGHER_IS_BETTER: Record<BaselineKey, boolean> = {
  review_hours_median: false, regressions_caught_per_month: true, post_deploy_alerts_per_month: false, auto_remediated_pct: true,
};
export const BASELINE_LABELS: Record<BaselineKey, { label: string; unit: string }> = {
  review_hours_median: { label: "Review time (median)", unit: "h" },
  regressions_caught_per_month: { label: "Regressions caught / month", unit: "" },
  post_deploy_alerts_per_month: { label: "Post-deploy alerts / month", unit: "" },
  auto_remediated_pct: { label: "Fixed without a person", unit: "%" },
};

export const buildHref = (runId: string) => `/?area=code_tasks&run=${enc(runId)}`;
export const incidentHref = (id: string) => `/?area=observability&view=alerts&incident=${enc(id)}`;

/** The scope query the report and the export share. */
export function pilotQuery(projectId: string, choice: string, days: number): string {
  const [scope, id] = choice === "org" ? ["org", ""] : choice === "project" ? ["project", projectId] : ["team", choice.slice(5)];
  return `project_id=${enc(projectId)}&scope_type=${scope}&scope_id=${enc(id)}&days=${days}`;
}

/** "-62% vs before", with whether that is better. Null when there is nothing to compare. */
export function versus(key: BaselineKey, before: number | undefined, after: number | null): { text: string; better: boolean; up: boolean } | null {
  if (before === undefined || after === null || after === undefined) return null;
  if (before === 0) return after === 0 ? { text: "Same as before", better: true, up: false } : null;
  const change = Math.round(((after - before) / before) * 100);
  if (change === 0) return { text: "Same as before", better: true, up: false };
  const better = HIGHER_IS_BETTER[key] ? change > 0 : change < 0;
  return { text: `${change > 0 ? "+" : ""}${change}% vs before`, better, up: change > 0 };
}

const fmt = (n: number | null | undefined, unit: string) =>
  n === null || n === undefined ? "–" : `${Number.isInteger(n) ? n : n.toFixed(1)}${unit}`;
const shortDay = (iso: string | null) =>
  iso ? new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" }) : "";

/** Hours per PR, oldest first, as small bars against the baseline line. */
function ReviewBars({ items, baseline }: { items: ReviewItem[]; baseline?: number }) {
  const done = items.filter((i) => i.hours !== null).slice(0, 30).reverse();
  if (!done.length) return null;
  const W = 300; const H = 48;
  const peak = Math.max(1, baseline ?? 0, ...done.map((i) => i.hours ?? 0));
  const bw = W / done.length;
  const y = (v: number) => H - (v / peak) * (H - 4);
  return (
    <svg className="gv-chart gv-pm-chart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img"
         aria-label={`Review hours for ${done.length} pull requests`} data-testid="gv-pm-review-chart">
      {done.map((i, n) => (
        <rect key={`${i.run_id}-${n}`} className="gv-chart-bar" x={n * bw + 1} width={Math.max(1, bw - 2)}
              y={y(i.hours ?? 0)} height={Math.max(1, H - y(i.hours ?? 0))}>
          <title>{`${fmt(i.hours, "h")}, opened ${shortDay(i.opened_at)}`}</title>
        </rect>
      ))}
      {baseline !== undefined ? <line className="gv-chart-cap" x1={0} x2={W} y1={y(baseline)} y2={y(baseline)} /> : null}
    </svg>
  );
}

/** A share bar: part of whole, with words. */
function Share({ part, whole, label, testid }: { part: number; whole: number; label: string; testid: string }) {
  const pct = whole ? Math.round((part / whole) * 100) : 0;
  return (
    <div className="gv-pm-share" data-testid={testid}>
      <div className="gv-bar" data-thin role="img" aria-label={`${part} of ${whole} ${label}`}>
        <span className="gv-bar-fill" style={{ width: `${pct}%` }} />
      </div>
      <span className="gv-muted">{part} of {whole} {label}</span>
    </div>
  );
}

function Sources({ count, testid, children }: { count: number; testid: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  if (!count) return null;
  return (
    <>
      <button type="button" className="ag-btn gv-pm-sources" aria-expanded={open} data-testid={`${testid}-toggle`}
              onClick={() => setOpen(!open)}>
        {open ? <CaretUp size={14} aria-hidden /> : <CaretDown size={14} aria-hidden />}{open ? "Hide sources" : `Sources (${count})`}
      </button>
      {open ? <ul className="gv-pm-items" data-testid={testid}>{children}</ul> : null}
    </>
  );
}

function Metric({ icon, title, value, sub, keyName, report, testid, tip, children }: {
  icon: React.ReactNode; title: string; value: string; sub: string; keyName: BaselineKey; report: PilotReport; testid: string;
  tip?: string; children?: React.ReactNode;
}) {
  const before = report.baseline.values[keyName];
  const vs = versus(keyName, before, report.after[keyName]);
  const unit = BASELINE_LABELS[keyName].unit;
  return (
    <section className="gv-card gv-pm-metric" data-testid={testid} aria-label={title}>
      <h3 className="gv-sec-head" title={tip}>{icon}{title.toUpperCase()}</h3>
      <div className="gv-pm-value">
        <b data-testid={`${testid}-value`}>{value}</b>
        {vs ? (
          <span className="gv-state" data-state={vs.better ? "green" : "yellow"} data-testid={`${testid}-vs`}>
            {vs.up ? <TrendUp size={14} aria-hidden /> : <TrendDown size={14} aria-hidden />}{vs.text}
          </span>
        ) : null}
      </div>
      <span className="gv-muted">{sub}{before !== undefined ? ` · before ${fmt(before, unit)}` : " · no before number yet"}</span>
      {children}
    </section>
  );
}

function BaselineCard({ projectId, actor, report, onSaved }: {
  projectId: string; actor: string; report: PilotReport; onSaved: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const keys = Object.keys(BASELINE_LABELS) as BaselineKey[];
  const open = () => {
    setDraft(Object.fromEntries(keys.map((k) => [k, report.baseline.values[k] === undefined ? "" : String(report.baseline.values[k])])));
    setNote(report.baseline.note); setSaid(null); setEditing(true);
  };
  const save = async () => {
    setBusy(true); setSaid(null);
    const values = Object.fromEntries(keys.filter((k) => draft[k] !== "").map((k) => [k, Number(draft[k])]));
    const r = await obsWrite("/v1/pilot-metrics/baseline", { actor, project_id: projectId, values, note },
                             { scope: "pilot-metrics", label: "Save baseline" });
    setBusy(false);
    if (!r.ok) { setSaid(r.error ?? "Not saved."); return; }
    setEditing(false); onSaved();
  };
  return (
    <section className="gv-card" aria-labelledby="gv-pm-base-h" data-testid="gv-pm-baseline">
      <div className="gv-fc-head">
        <h3 id="gv-pm-base-h" className="gv-sec-head">BEFORE BOSS</h3>
        <span className="gv-spacer" />
        {!editing ? (
          <GatedButton permission="budget:manage" type="button" className="ag-btn" data-testid="gv-pm-baseline-edit"
                       title="Type in the team's numbers from before the preview" onClick={open}>
            <PencilSimple size={14} aria-hidden />Edit</GatedButton>
        ) : null}
      </div>
      {!editing ? (
        <ul className="gv-pm-base-list">
          {keys.map((k) => (
            <li key={k}><span className="gv-muted">{BASELINE_LABELS[k].label}</span>
              <b data-testid={`gv-pm-base-${k}`}>{fmt(report.baseline.values[k], BASELINE_LABELS[k].unit)}</b></li>
          ))}
          {report.baseline.note ? <li className="gv-pm-note" data-user-text>{report.baseline.note}</li> : null}
        </ul>
      ) : (
        <div className="gv-pm-base-form" data-testid="gv-pm-baseline-form">
          {keys.map((k) => (
            <label key={k} className="gv-pm-field"><span>{BASELINE_LABELS[k].label}{BASELINE_LABELS[k].unit ? ` (${BASELINE_LABELS[k].unit === "h" ? "hours" : "%"})` : ""}</span>
              <input className="gv-input" type="number" inputMode="decimal" min={0} step="any" value={draft[k] ?? ""}
                     data-testid={`gv-pm-input-${k}`} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} /></label>
          ))}
          <label className="gv-pm-field gv-pm-field-wide"><span>Where the numbers come from</span>
            <input className="gv-input" value={note} maxLength={500} data-testid="gv-pm-input-note"
                   onChange={(e) => setNote(e.target.value)} /></label>
          <div className="gv-pm-actions">
            <GatedButton permission="budget:manage" type="button" className="ag-btn ag-btn-primary" disabled={busy}
                         data-testid="gv-pm-baseline-save" onClick={() => void save()}><FloppyDisk size={14} aria-hidden />Save</GatedButton>
            <button type="button" className="ag-btn" disabled={busy} data-testid="gv-pm-baseline-cancel"
                    onClick={() => setEditing(false)}><X size={14} aria-hidden />Cancel</button>
          </div>
          {said ? <span className="gv-field-error" role="alert" data-testid="gv-pm-baseline-error">{said}</span> : null}
        </div>
      )}
    </section>
  );
}

export function PilotMetrics({ projectId, actor }: { projectId: string; actor: string }) {
  const [teams, setTeams] = useState<Team[]>([]);
  const [choice, setChoice] = useState("org");
  const [days, setDays] = useState<number>(30);
  const [report, setReport] = useState<PilotReport | null>(null);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    let live = true;
    void fetch(`/v1/budget-policies/scoped/teams?project_id=${enc(projectId)}`, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null)).then((b) => { if (live) setTeams(b?.teams ?? []); }).catch(() => undefined);
    return () => { live = false; };
  }, [projectId]);
  useEffect(() => {
    let live = true;
    setError("");
    void fetch(`/v1/pilot-metrics?${pilotQuery(projectId, choice, days)}`, { cache: "no-store" })
      .then(async (r) => {
        const body = await r.json().catch(() => null);
        if (!live) return;
        if (r.ok && body?.review && body?.regressions && body?.post_deploy) setReport(body as PilotReport);
        else if (r.ok) { setReport(null); setError("Can't read the metrics"); }
        else { setReport(null); setError(body?.detail || "Can't read the metrics"); }
      })
      .catch(() => { if (live) { setReport(null); setError("Can't reach BOSS"); } });
    return () => { live = false; };
  }, [projectId, choice, days, tick]);

  const q = pilotQuery(projectId, choice, days);
  const r = report;
  return (
    <div className="gv-pm" data-testid="gv-pilot">
      <div className="gv-pm-bar">
        <select className="gv-input" aria-label="Whose numbers" value={choice} data-testid="gv-pm-scope"
                onChange={(e) => setChoice(e.target.value)}>
          <option value="org">Whole org</option><option value="project">This project</option>
          {teams.map((t) => <option key={t.id} value={`team:${t.id}`}>Team {t.name}</option>)}
        </select>
        <span className="ag-seg" role="group" aria-label="Window" data-testid="gv-pm-window">
          {WINDOWS.map((d) => (
            <button key={d} type="button" aria-pressed={days === d} data-testid={`gv-pm-days-${d}`} onClick={() => setDays(d)}>{d}d</button>
          ))}
        </span>
        <span className="gv-spacer" />
        <a className="ag-btn" href={`/v1/pilot-metrics/export?${q}&format=csv`} download data-testid="gv-pm-export-csv"
           title="Summary and every counted item, with its source"><DownloadSimple size={14} aria-hidden />CSV</a>
        <a className="ag-btn" href={`/v1/pilot-metrics/export?${q}&format=json`} download data-testid="gv-pm-export-json"
           title="The whole report as JSON"><DownloadSimple size={14} aria-hidden />JSON</a>
      </div>
      {error ? (
        <div className="ag-empty" role="alert" data-testid="gv-pm-error"><span>{error}</span>
          <button type="button" className="ag-btn" onClick={reload}><ArrowClockwise size={14} aria-hidden />Retry</button></div>
      ) : !r ? <div className="gv-skel" aria-busy="true" /> : (
        <>
          <div className="gv-pm-grid">
            <Metric icon={<Timer size={16} aria-hidden />} title="PR open to approval" keyName="review_hours_median" report={r}
                    testid="gv-pm-review" value={fmt(r.review.median_hours, "h")}
                    sub={`median of ${r.review.approved} approved${r.review.waiting ? `, ${r.review.waiting} waiting` : ""}`}>
              <ReviewBars items={r.review.items} baseline={r.baseline.values.review_hours_median} />
              <Sources count={r.review.items.length} testid="gv-pm-review-items">
                {r.review.items.map((i) => (
                  <li key={`${i.run_id}-${i.opened_at}`}>
                    <a className="ag-link" href={buildHref(i.run_id)} title={i.pull_request_url ?? i.run_id}>
                      {shortDay(i.opened_at)} build<ArrowSquareOut size={12} aria-hidden /></a>
                    <span className="gv-hbar-value">{i.hours === null ? "waiting" : fmt(i.hours, "h")}</span>
                  </li>
                ))}
              </Sources>
            </Metric>
            <Metric icon={<ShieldCheck size={16} aria-hidden />} title="Regressions caught" keyName="regressions_caught_per_month" report={r}
                    testid="gv-pm-regressions" value={String(r.regressions.total)}
                    tip="Held before merge: builds whose tests failed at a gate, counted once per build. Stopped after merge: deploys rolled back, changes rolled back or reverted."
                    sub={`${r.regressions.blocked_before_merge} held before merge, ${r.regressions.stopped_after_merge} stopped after`}>
              <Share part={r.regressions.blocked_before_merge} whole={r.regressions.total} label="before merge" testid="gv-pm-reg-share" />
              <Sources count={r.regressions.before_merge_items.length + r.regressions.after_merge_items.length} testid="gv-pm-reg-items">
                {r.regressions.before_merge_items.map((i) => (
                  <li key={i.run_id}>
                    <a className="ag-link" href={buildHref(i.run_id)} title={i.gates_failed.join(", ")} data-user-text>
                      {i.title || "Build"}<ArrowSquareOut size={12} aria-hidden /></a>
                    <span className="gv-hbar-value">held</span>
                  </li>
                ))}
                {r.regressions.after_merge_items.map((i, n) => (
                  <li key={`${i.kind}-${n}`}>
                    {i.run_id ? <a className="ag-link" href={buildHref(i.run_id)}>{i.kind === "deploy.rolled_back" ? "Deploy rolled back" : "Change undone"}<ArrowSquareOut size={12} aria-hidden /></a>
                      : <span title={`${i.entity_type ?? ""} ${i.entity_id ?? i.change_set_id ?? ""}`.trim()}>
                          {i.kind === "change_set.reverted" ? "Change reverted" : i.kind === "deploy.rolled_back" ? "Deploy rolled back" : "Change rolled back"}</span>}
                    <span className="gv-hbar-value">{shortDay(i.at)}</span>
                  </li>
                ))}
              </Sources>
            </Metric>
            <Metric icon={<Siren size={16} aria-hidden />} title="Post-deploy errors" keyName="auto_remediated_pct" report={r}
                    testid="gv-pm-errors" value={fmt(r.post_deploy.auto_remediated_pct, "%")}
                    sub={`${r.post_deploy.auto_remediated} of ${r.post_deploy.alerted} alerts fixed without a person`}>
              <Share part={r.post_deploy.auto_remediated} whole={r.post_deploy.alerted} label="fixed by BOSS" testid="gv-pm-err-share" />
              <span className="gv-muted">{r.post_deploy.notified} notified a person</span>
              <Sources count={r.post_deploy.items.length} testid="gv-pm-err-items">
                {r.post_deploy.items.map((i) => (
                  <li key={i.alert_id}>
                    {i.incident_id ? <a className="ag-link" href={incidentHref(i.incident_id)} data-user-text>{i.alertname}<ArrowSquareOut size={12} aria-hidden /></a>
                      : <span data-user-text>{i.alertname}</span>}
                    <span className="gv-hbar-value">
                      {i.auto_remediated ? <><CheckCircle size={12} aria-hidden /> fixed</> : i.handled_by_person ? "a person" : "alerted"}
                    </span>
                  </li>
                ))}
              </Sources>
            </Metric>
          </div>
          <BaselineCard projectId={projectId} actor={actor} report={r} onSaved={reload} />
          <span className="gv-muted" data-testid="gv-pm-window-line">{r.label}, last {r.days} days. Computed from builds, merge cards and alerts; nothing new is tracked.</span>
        </>
      )}
    </div>
  );
}
