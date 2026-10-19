"use client";

/**
 * NEW ALERT RULE (BOSSAPP-598 feedback 4): a rule on any metric the app ACTUALLY reports,
 * without writing PromQL — and the PromQL it writes, always on show.
 *
 *   App          [ Order API · staging ▾ ]            (preselected in the app drawer)
 *   Metric       [ orders_created_total      ] counter   (the app's scraped/ingested series)
 *   Measure      [ rate ▾ ]  over [ 5m ▾ ]  where [ result="error"      ]
 *   Fire when    [ > ▾ ] [ 0.5 ]   for [ 2 ] min    Severity ⬣SEV1 ▲SEV2 ◐SEV3 ⓘSEV4
 *   Name         [ OrdersCreatedTotalHigh ]   Summary [ … ]
 *   PromQL       sum(rate(orders_created_total{app="Order API",environment="staging",result="error"}[5m])) > 0.5
 *                [ Edit PromQL ]                       (experts: the fields above are set aside)
 *   Last 24 h    Would have fired 3 times in the last 24 h      (live, as you change it)
 *                                                       [ Cancel ]  [ Add rule ]
 *
 * The server checks the PromQL (promql_lite) and names anything it cannot evaluate in words; the
 * preview is the same evaluator, replayed over 24 hours.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";

import { GatedButton } from "../../components/gated";
import {
  PREVIEW_PATH, SEVERITIES, forWord, rulesPutPath, useMetricNames, type MetricName, type Sev, type SevInfo,
} from "../../lib/app-alerts";
import { showToast } from "../client-forms";
import { Sheet } from "../infra/kit";
import { obsWrite } from "./obs-write";
import { SevFlag, sevTip } from "./severity";

export type Agg = "rate" | "increase" | "avg" | "p95";
const AGGS: { key: Agg; label: string; tip: string }[] = [
  { key: "rate", label: "rate", tip: "Per-second rate of a counter over the window" },
  { key: "increase", label: "increase", tip: "How much a counter grew over the window" },
  { key: "avg", label: "average", tip: "Average of a gauge over the window" },
  { key: "p95", label: "p95", tip: "95th percentile of a histogram over the window" },
];
const WINDOWS = ["1m", "5m", "15m", "30m", "1h"];
const OPS = [">", ">=", "<", "<="] as const;
type Op = typeof OPS[number];

/** Which aggregations make sense for a metric type. */
export function aggsFor(m: MetricName | null): Agg[] {
  if (!m) return ["rate", "increase", "avg"];
  if (m.type === "histogram") return ["p95", "rate"];
  if (m.type === "counter") return ["rate", "increase"];
  if (m.type === "gauge") return ["avg"];
  return ["rate", "increase", "avg"];
}

/** The PromQL the builder writes. A histogram's rate counts its observations (`_count`). */
export function buildExpr(o: { name: string; type?: string; agg: Agg; window: string; selector: string; filter: string; op: Op; threshold: string }): string {
  const sel = [o.selector.trim(), o.filter.trim()].filter(Boolean).join(",");
  const w = o.window;
  const t = o.threshold.trim() || "0";
  let lhs: string;
  if (o.agg === "p95") lhs = `histogram_quantile(0.95, sum by (le) (rate(${o.name}_bucket{${sel}}[${w}])))`;
  else if (o.agg === "avg") lhs = `avg(avg_over_time(${o.name}{${sel}}[${w}]))`;
  else {
    const n = o.type === "histogram" ? `${o.name}_count` : o.name;
    lhs = `sum(${o.agg}(${n}{${sel}}[${w}]))`;
  }
  return `${lhs} ${o.op} ${t}`;
}

/** BOSS's own runbooks (app_alerts.RUNBOOKS), or none. */
export const RUNBOOKS: { url: string; label: string }[] = [
  { url: "boss:runbook/app-serving", label: "BOSS · The app is serving badly" },
  { url: "boss:runbook/app-down", label: "BOSS · The app is down" },
  { url: "", label: "None" },
];

/** An HTTP series (requests, durations) is the app serving: its runbook by default. */
export function defaultRunbook(metric: string): string {
  return /^http_|_requests?_|_duration_/.test(metric) ? RUNBOOKS[0].url : "";
}

/** orders_created_total → OrdersCreatedTotalHigh: a Prometheus alert name. */
export function suggestName(metric: string, op: Op): string {
  const base = metric.split(/[^A-Za-z0-9]+/).filter(Boolean).map((p) => p[0].toUpperCase() + p.slice(1)).join("");
  const name = `${base}${op.startsWith(">") ? "High" : "Low"}`.replace(/^[^A-Za-z_]+/, "");
  return name.slice(0, 80) || "CustomAlert";
}

export function RuleBuilder({ targetId, apps, projectId, actor, severities = SEVERITIES, onClose, onSaved }: {
  /** The app, when opened from its drawer; else `apps` to pick from (Observability › Alerts). */
  targetId: string | null; apps?: { targetId: string; app: string; env: string }[];
  projectId: string; actor: string; severities?: SevInfo[]; onClose: () => void; onSaved: (targetId: string) => void;
}) {
  const [app, setApp] = useState<string>(targetId ?? apps?.[0]?.targetId ?? "");
  const names = useMetricNames(projectId, app || null);
  const metrics = useMemo(() => names.data?.metrics ?? [], [names.data]);
  const [metricName, setMetricName] = useState("");
  const metric = metrics.find((m) => m.name === metricName) ?? null;
  const allowed = aggsFor(metric);
  const [agg, setAgg] = useState<Agg>("rate");
  const useAgg: Agg = allowed.includes(agg) ? agg : allowed[0];
  const [win, setWin] = useState("5m");
  const [filter, setFilter] = useState("");
  const [op, setOp] = useState<Op>(">");
  const [threshold, setThreshold] = useState("");
  const [hold, setHold] = useState("2");
  const [sev, setSev] = useState<Sev>("sev3");
  const [alert, setAlert] = useState("");
  const [named, setNamed] = useState(false);
  const [summary, setSummary] = useState("");
  // THE RUNBOOK THE AGENTS FOLLOW: a rule on the app's own HTTP series is "the app is serving
  // badly" unless the person picks otherwise; the alert, the incident and its investigation name it.
  const [book, setBook] = useState<string | null>(null);
  const runbook = book ?? defaultRunbook(metricName);
  const [expert, setExpert] = useState(false);
  const [raw, setRaw] = useState("");
  const [preview, setPreview] = useState<{ word: string; bad: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const built = metric ? buildExpr({ name: metric.name, type: metric.type, agg: useAgg, window: win, selector: names.data?.selector ?? "",
                                     filter, op, threshold }) : "";
  const expr = expert ? raw : built;
  const name = named ? alert : metric ? suggestName(metric.name, op) : "";
  const forS = (Number(hold) || 0) * 60;

  // The live preview: the server replays the expression over the last 24 hours.
  const seq = useRef(0);
  useEffect(() => {
    setPreview(null);
    if (!app || !expr.trim()) return;
    const n = ++seq.current;
    const timer = setTimeout(() => {
      void obsWrite<{ word?: string }>(PREVIEW_PATH, { project_id: projectId, subject_id: app, expr, for: forWord(forS) },
                                       { scope: `obs:builder-preview:${app}:${n}`, label: "Preview" }).then((r) => {
        if (n !== seq.current) return;
        setPreview(r.ok ? { word: r.body?.word ?? "No answer", bad: false } : { word: r.error ?? "No preview", bad: true });
      });
    }, 600);
    return () => clearTimeout(timer);
  }, [app, expr, forS, projectId]);

  const save = async () => {
    setBusy(true);
    setError(null);
    const change = { alert: name, expr, for: forWord(forS), keep_firing_for: "0m", labels: { severity: sev },
                     annotations: { summary: summary || `${name} on ${apps?.find((a) => a.targetId === app)?.app ?? "the app"}`,
                                    ...(runbook ? { runbook_url: runbook } : {}) },
                     enabled: true, triage: true, auto_respond: false };
    const r = await obsWrite(rulesPutPath(app), { project_id: projectId, actor, reason: "New alert rule", rules: [change] },
                             { scope: `obs:rule-new:${app}`, label: "Add rule" }, "PUT");
    setBusy(false);
    if (!r.ok) { setError(r.error ?? "Not saved"); return; }
    showToast("success", "Rule added");
    onSaved(app);
  };

  return (
    <Sheet title="New alert rule" testId="obs-rule-builder" onClose={onClose}>
      <div className="obs-rform">
        {apps && !targetId ? (
          <label className="inf-field">
            <span>App</span>
            <select value={app} data-testid="obs-builder-app" title="The app whose metrics the rule reads"
                    onChange={(e) => { setApp(e.target.value); setMetricName(""); }}>
              {apps.map((a) => <option key={a.targetId} value={a.targetId}>{a.app} · {a.env}</option>)}
            </select>
          </label>
        ) : null}
        {!expert ? (
          <>
            <label className="inf-field">
              <span>Metric</span>
              <input list="obs-builder-metrics" value={metricName} data-testid="obs-builder-metric" autoComplete="off" spellCheck={false}
                     placeholder={names.data ? `${metrics.length} series names` : "Loading…"}
                     title={names.data?.word ?? "Series names this app reports"}
                     onChange={(e) => {
                       const v = e.target.value.trim();
                       setMetricName(v);
                       // A new metric starts on the measure its type is read with (p95 for a histogram).
                       setAgg(aggsFor(metrics.find((m) => m.name === v) ?? null)[0]);
                     }} />
              <datalist id="obs-builder-metrics">
                {metrics.map((m) => <option key={m.name} value={m.name}>{m.type}{m.help ? ` · ${m.help}` : ""}</option>)}
              </datalist>
            </label>
            {metric ? (
              <span className="obs-line">
                <span className="obs-chip" data-testid="obs-builder-type" title={metric.help ?? "The metric's type, as the app declares it"}>{metric.type}</span>
                {metric.labels.length ? <span className="inf-faint" data-copy="data" title="Its labels, for the filter">{metric.labels.join(", ")}</span> : null}
              </span>
            ) : names.data && metrics.length === 0 ? (
              <span className="inf-faint" data-testid="obs-builder-nometrics" title={names.data.word ?? undefined}>No series yet</span>
            ) : names.error ? <span className="inf-error" title={names.error}>Can&apos;t load metrics</span> : null}
            <div className="obs-builder-pick">
              <label className="inf-field">
                <span>Measure</span>
                <select value={useAgg} data-testid="obs-builder-agg" title={AGGS.find((a) => a.key === useAgg)?.tip}
                        onChange={(e) => setAgg(e.target.value as Agg)}>
                  {allowed.map((k) => AGGS.find((a) => a.key === k)!).map((a) => <option key={a.key} value={a.key} title={a.tip}>{a.label}</option>)}
                </select>
              </label>
              <label className="inf-field">
                <span>Window</span>
                <select value={win} data-testid="obs-builder-window" title="The range each check reads: the granularity"
                        onChange={(e) => setWin(e.target.value)}>
                  {WINDOWS.map((w) => <option key={w} value={w}>{w}</option>)}
                </select>
              </label>
              <label className="inf-field">
                <span>Where</span>
                <input value={filter} data-testid="obs-builder-filter" autoComplete="off" spellCheck={false} placeholder='status=~"5.."'
                       title="Optional label matchers, comma separated" onChange={(e) => setFilter(e.target.value)} />
              </label>
            </div>
            <div className="obs-builder-pick">
              <label className="inf-field">
                <span>Fire when</span>
                <select value={op} data-testid="obs-builder-op" title="The comparison" onChange={(e) => setOp(e.target.value as Op)}>
                  {OPS.map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
              </label>
              <label className="inf-field">
                <span>Threshold</span>
                <input type="number" inputMode="decimal" step="any" value={threshold} data-testid="obs-builder-threshold"
                       title="The value it is compared with" onChange={(e) => setThreshold(e.target.value)} />
              </label>
            </div>
          </>
        ) : null}
        <div className="obs-builder-pick">
          <label className="inf-field">
            <span>For (min)</span>
            <input type="number" inputMode="numeric" min={0} max={1440} value={hold} data-testid="obs-builder-for"
                   title="How long it must stay true before it fires" onChange={(e) => setHold(e.target.value)} />
          </label>
          <label className="inf-field">
            <span>Name</span>
            <input value={name} data-testid="obs-builder-name" autoComplete="off" spellCheck={false}
                   title="Letters, digits and _, like a Prometheus alert name" onChange={(e) => { setAlert(e.target.value); setNamed(true); }} />
          </label>
        </div>
        <div className="obs-rrow" role="radiogroup" aria-label="Severity">
          <span className="obs-rlabel">Severity</span>
          <span className="obs-rbody">
            {severities.map((s) => (
              <label key={s.key} className="obs-rcheck" title={sevTip(s.key, severities)}>
                <input type="radio" name="builder-severity" checked={sev === s.key} onChange={() => setSev(s.key)}
                       data-testid={`obs-builder-sev-${s.key}`} />
                <SevFlag sev={s.key} catalog={severities} />
              </label>
            ))}
          </span>
        </div>
        <label className="inf-field">
          <span>Summary</span>
          <input value={summary} data-testid="obs-builder-summary" autoComplete="off" onChange={(e) => setSummary(e.target.value)}
                 title="What a person reads when it fires" />
        </label>
        <label className="inf-field">
          <span>Runbook</span>
          <select value={runbook} data-testid="obs-builder-runbook" title="The steps an agent and a person follow when it fires"
                  onChange={(e) => setBook(e.target.value)}>
            {RUNBOOKS.map((r) => <option key={r.url} value={r.url}>{r.label}</option>)}
          </select>
        </label>
        <div className="obs-adetail-row">
          <span className="obs-fact-k" title="The query BOSS evaluates">PromQL</span>
          {expert ? (
            <textarea value={raw} rows={4} spellCheck={false} className="obs-promql-edit" data-testid="obs-builder-expr"
                      onChange={(e) => setRaw(e.target.value)} />
          ) : (
            <code className="obs-promql" data-copy="data" data-testid="obs-builder-promql">{built || "Pick a metric"}</code>
          )}
          <button type="button" className="inf-link" aria-pressed={expert} data-testid="obs-builder-expert"
                  title={expert ? "Back to the fields; your edit is dropped" : "Write the PromQL yourself"}
                  onClick={() => { if (!expert) setRaw(built); setExpert((v) => !v); }}>{expert ? "Use the builder" : "Edit PromQL"}</button>
        </div>
        <div className="obs-builder-preview" data-testid="obs-builder-preview">
          <span className="obs-fact-k" title="Replayed over the last 24 hours, as you change it">Last 24 h</span>
          {preview ? <span className={preview.bad ? "inf-error" : "obs-strong"} data-testid="obs-builder-preview-word" data-copy="data">{preview.word}</span>
            : expr.trim() ? <span className="inf-faint">Checking…</span> : <span className="inf-faint">Pick a metric</span>}
        </div>
        {error ? <span className="inf-error" data-testid="obs-builder-error" data-copy="data">{error}</span> : null}
        <div className="inf-actions">
          <span className="ag-spacer" />
          <button type="button" className="ag-btn" onClick={onClose}>Cancel</button>
          <GatedButton permission="observability:manage" type="button" className="ag-btn ag-btn-primary" data-testid="obs-builder-save"
                       disabled={busy || !app || !expr.trim() || !name.trim()} aria-busy={busy || undefined} onClick={() => void save()}>
            {busy ? "Saving…" : "Add rule"}
          </GatedButton>
        </div>
      </div>
    </Sheet>
  );
}
