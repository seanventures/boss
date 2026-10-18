"use client";

/**
 * Edit a rule (or add one) — the side sheet (BOSSAPP-598 mockup):
 *
 *   Error rate alert
 *     Fire when    errors (5xx) are above [ 5 ]% of requests
 *     Over         [ 5 ] min, with at least [ 20 ] requests
 *     Hold         [ 0 ] min before it fires          (Prometheus `for`)
 *     Severity     ( ) Low  (•) High
 *     When it fires  [x] An agent triages it   [x] Respond automatically (asks your consent once)
 *     Notify       [x] Console [x] On-call email [ ] #ops Slack     (PUT /v1/alert-rules/{id}/notify)
 *     Runbook      [ boss:runbook/app-serving ]
 *     PromQL       100 * sum(increase(http_requests_total{…}[5m])) / … > 5 and …   (read only)
 *                  a rule of your own: [Preview] → "Would have fired 2 times in the last 24 h"
 *                                                          [ Cancel ]  [ Save rule ]
 *
 * A built-in rule is changed by its settings (its PromQL follows them); a rule of your own is
 * PromQL, in the Prometheus alerting-rule shape. The server checks every value and says why.
 */
import React, { useState } from "react";

import { GatedButton } from "../../components/gated";
import { forWord, rulesPutPath, type AlertRule, type RuleKind } from "../../lib/app-alerts";
import { showToast } from "../client-forms";
import { Sheet } from "../infra/kit";
import { obsWrite } from "./obs-write";
import { useContactPoints, usePolicies } from "../../lib/monitoring";

type Field = { key: string; before: string; after: string; tip: string };
/** Each built-in's settings, as the sentence the mockup reads. */
const FIELDS: Record<Exclude<RuleKind, "custom">, { head: string; rows: { label: string; fields: Field[] }[] }> = {
  error_rate: { head: "Error rate alert", rows: [
    { label: "Fire when", fields: [{ key: "threshold", before: "errors (5xx) are above", after: "% of requests", tip: "Share of requests answered 5xx" }] },
    { label: "Over", fields: [{ key: "window_minutes", before: "", after: "min,", tip: "The window the rate is measured over" },
                              { key: "min_requests", before: "with at least", after: "requests", tip: "Fewer requests than this is too little to judge" }] },
  ] },
  latency_p95: { head: "p95 latency alert", rows: [
    { label: "Fire when", fields: [{ key: "threshold", before: "p95 latency is above", after: "ms", tip: "95 of 100 requests are faster than this" }] },
    { label: "Over", fields: [{ key: "window_minutes", before: "", after: "min,", tip: "The window the latency is measured over" },
                              { key: "min_requests", before: "with at least", after: "requests", tip: "Fewer requests than this is too little to judge" }] },
  ] },
  health_check: { head: "Health check alert", rows: [
    { label: "Fire when", fields: [{ key: "min_failures", before: "", after: "probes fail,", tip: "Failed health probes, all of them in the window" },
                                   { key: "window_minutes", before: "within", after: "min", tip: "Every probe in this window must fail" }] },
  ] },
  no_traffic: { head: "No traffic alert", rows: [
    { label: "Fire when", fields: [{ key: "window_minutes", before: "no requests for", after: "min", tip: "Not one request in this window" }] },
    { label: "If", fields: [{ key: "expected_hours", before: "it had traffic in the last", after: "hours", tip: "Only an app that usually has traffic" }] },
  ] },
  deploy_health: { head: "Deploy health alert", rows: [] },
};

function NumberField({ f, value, bounds, onChange }: { f: Field; value: string; bounds?: [number, number]; onChange: (v: string) => void }) {
  return (
    <span className="obs-rfield">
      {f.before ? <span className="inf-muted">{f.before}</span> : null}
      <input type="number" inputMode="decimal" className="obs-rnum" value={value} aria-label={`${f.before} ${f.after}`.trim()}
             min={bounds?.[0]} max={bounds?.[1]} step="any" title={f.tip} data-testid={`obs-rule-${f.key}`}
             onChange={(e) => onChange(e.target.value)} />
      <span className="inf-muted">{f.after}</span>
    </span>
  );
}

export function RuleSheet({ rule, targetId, projectId, actor, onClose, onSaved }: {
  rule: AlertRule | null; targetId: string; projectId: string; actor: string; onClose: () => void; onSaved: () => void;
}) {
  const custom = !rule || rule.kind === "custom";
  const [params, setParams] = useState<Record<string, string>>(
    Object.fromEntries(Object.entries(rule?.params ?? {}).map(([k, v]) => [k, String(v)])));
  const [hold, setHold] = useState(String(Math.round((rule?.forSeconds ?? 0) / 60)));
  const [severity, setSeverity] = useState<"warning" | "critical">(rule?.severity ?? "warning");
  const [triage, setTriage] = useState(rule?.triage ?? true);
  const [respond, setRespond] = useState(rule?.autoRespond ?? false);
  const [enabled, setEnabled] = useState(rule?.enabled ?? true);
  const [runbook, setRunbook] = useState(rule?.runbook.url ?? "");
  const [alert, setAlert] = useState(rule?.alert ?? "");
  const [expr, setExpr] = useState(rule?.expr ?? "");
  const [summary, setSummary] = useState(rule?.summary ?? "");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cps = useContactPoints(projectId);
  const policies = usePolicies(rule ? projectId : "");
  const current = policies.data?.find((p) => p.ruleId === rule?.id)?.contactPointIds ?? null;
  const [notify, setNotify] = useState<Set<string> | null>(null);
  const told = notify ?? new Set(current ?? []);
  const [preview, setPreview] = useState<{ word: string; bad: boolean } | null>(null);
  const runPreview = async () => {
    setPreview(null);
    const r = await obsWrite<{ word?: string }>("/v1/alert-rules/preview", { project_id: projectId, subject_id: targetId, expr,
      for: forWord((Number(hold) || 0) * 60) }, { scope: `obs:rule-preview:${targetId}`, label: "Preview" });
    setPreview(r.ok ? { word: r.body?.word ?? "No answer", bad: false } : { word: r.error ?? "No preview", bad: true });
  };
  const spec = rule && rule.kind !== "custom" ? FIELDS[rule.kind] : null;
  const title = spec ? spec.head : rule ? `${rule.alert} alert` : "Add a rule";

  const save = async () => {
    setBusy(true);
    setError(null);
    const holdMin = Number(hold) || 0;
    const change: Record<string, unknown> = custom
      ? { alert, expr, for: forWord(holdMin * 60), labels: { severity }, annotations: { summary, ...(runbook ? { runbook_url: runbook } : {}) },
          enabled, triage, auto_respond: respond }
      : { params: Object.fromEntries(Object.entries(params).map(([k, v]) => [k, Number(v)])), for: forWord(holdMin * 60),
          severity, enabled, triage, auto_respond: respond, runbook_url: runbook };
    if (rule) change.id = rule.id;
    const r = await obsWrite(rulesPutPath(targetId), { project_id: projectId, actor, reason, rules: [change] },
                             { scope: `obs:rule:${rule?.id ?? "new"}`, label: "Save rule" }, "PUT");
    setBusy(false);
    if (!r.ok) { setError(r.error ?? "Not saved"); return; }
    if (rule && notify) {
      setBusy(true);
      const n = await obsWrite(`/v1/alert-rules/${encodeURIComponent(rule.id)}/notify`, { project_id: projectId, actor, contact_point_ids: [...notify] },
                               { scope: `obs:rule-notify:${rule.id}`, label: "Notify" }, "PUT");
      setBusy(false);
      if (!n.ok) { setError(`Rule saved, but who is told was not: ${n.error ?? "refused"}`); return; }
    }
    showToast("success", rule ? (notify ? `Rule saved; ${notify.size ? `tells ${notify.size} contact point${notify.size === 1 ? "" : "s"}` : "tells the console only"}` : "Rule saved") : "Rule added");
    onSaved();
  };

  return (
    <Sheet title={title} testId="obs-rule-sheet" onClose={onClose}>
      <div className="obs-rform">
        {spec ? spec.rows.map((row) => (
          <div key={row.label} className="obs-rrow">
            <span className="obs-rlabel">{row.label}</span>
            <span className="obs-rbody">
              {row.fields.map((f) => <NumberField key={f.key} f={f} value={params[f.key] ?? ""} bounds={rule?.bounds[f.key]}
                                                  onChange={(v) => setParams((p) => ({ ...p, [f.key]: v }))} />)}
            </span>
          </div>
        )) : null}
        {custom ? (
          <>
            <label className="inf-field">
              <span>Name</span>
              <input value={alert} onChange={(e) => setAlert(e.target.value)} data-testid="obs-rule-alert" placeholder="CheckoutSlow"
                     autoComplete="off" spellCheck={false} title="Letters, digits and _, like a Prometheus alert name" />
            </label>
            <label className="inf-field">
              <span>PromQL</span>
              <textarea value={expr} onChange={(e) => { setExpr(e.target.value); setPreview(null); }} data-testid="obs-rule-expr" rows={4} spellCheck={false}
                        className="obs-promql-edit" placeholder='sum(rate(http_requests_total{status=~"5.."}[5m])) > 1' />
            </label>
            <span className="obs-line">
              <button type="button" className="ag-btn ag-btn-sm" data-testid="obs-rule-preview" disabled={!expr.trim()}
                      title="Run it over the last 24 hours: how often would it have fired?" onClick={() => void runPreview()}>Preview</button>
              {preview ? <span className={preview.bad ? "inf-error" : "obs-strong"} data-testid="obs-rule-preview-word" data-copy="data">{preview.word}</span> : null}
            </span>
            <label className="inf-field">
              <span>Summary</span>
              <input value={summary} onChange={(e) => setSummary(e.target.value)} data-testid="obs-rule-summary" autoComplete="off" />
            </label>
          </>
        ) : null}
        <div className="obs-rrow">
          <span className="obs-rlabel" title="Prometheus' for: how long it must stay breached">Hold</span>
          <span className="obs-rbody">
            <NumberField f={{ key: "for", before: "", after: "min before it fires", tip: "0 fires on the first breach" }}
                         value={hold} bounds={[0, 1440]} onChange={setHold} />
          </span>
        </div>
        <div className="obs-rrow" role="radiogroup" aria-label="Severity">
          <span className="obs-rlabel">Severity</span>
          <span className="obs-rbody">
            {(["warning", "critical"] as const).map((s) => (
              <label key={s} className="obs-rcheck">
                <input type="radio" name="severity" checked={severity === s} onChange={() => setSeverity(s)} data-testid={`obs-rule-sev-${s}`} />
                <span>{s === "critical" ? "High" : "Low"}</span>
              </label>
            ))}
          </span>
        </div>
        <div className="obs-rrow">
          <span className="obs-rlabel">When it fires</span>
          <span className="obs-rbody">
            <label className="obs-rcheck" title="A model reads the breach, releases and incidents, and decides">
              <input type="checkbox" checked={triage} data-testid="obs-rule-triage"
                     onChange={(e) => { setTriage(e.target.checked); if (!e.target.checked) setRespond(false); }} />
              <span>An agent triages it</span>
            </label>
            <label className="obs-rcheck" title="Starts Incident Response on your standing grant; asks your consent once">
              <input type="checkbox" checked={respond} disabled={!triage} data-testid="obs-rule-auto"
                     onChange={(e) => setRespond(e.target.checked)} />
              <span>Respond automatically</span>
            </label>
          </span>
        </div>
        <div className="obs-rrow">
          <span className="obs-rlabel">Notify</span>
          <span className="obs-rbody">
            {!rule ? <span className="inf-faint" title="A new rule follows the notification policies">Save the rule first, then choose who is told</span>
              : !cps.data ? <span className="inf-faint" data-testid="obs-rule-notify-none"
                                  title={cps.error ? "Contact points could not be loaded" : undefined}>{cps.error ? "Console (contact points not loaded)" : "Loading…"}</span>
              : cps.data.length === 0 ? <span className="inf-faint" data-testid="obs-rule-notify-none" title="Add one in Observability › Notify">Console only: no contact points yet</span>
              : cps.data.map((c) => (
                <label key={c.id} className="obs-rcheck" data-testid={`obs-rule-notify-${c.id}`}
                       title={!c.verified && c.kind !== "console" ? "Unverified: high-urgency alerts skip it until it is verified" : `Tell ${c.name}`}>
                  <input type="checkbox" checked={told.has(c.id)}
                         onChange={(e) => { const x = new Set(told); if (e.target.checked) x.add(c.id); else x.delete(c.id); setNotify(x); }} />
                  <span>{c.name}</span>
                </label>
              ))}
          </span>
        </div>
        <label className="inf-field">
          <span>Runbook</span>
          <input value={runbook} onChange={(e) => setRunbook(e.target.value)} data-testid="obs-rule-runbook" autoComplete="off"
                 spellCheck={false} placeholder="docs/runbooks/checkout.md" title="A markdown page kept with the app, or a URL" />
        </label>
        <label className="obs-rcheck">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} data-testid="obs-rule-enabled" />
          <span>Rule on</span>
        </label>
        {rule && !custom ? (
          <div className="obs-adetail-row">
            <span className="obs-fact-k" title="Follows the settings above when saved">PromQL</span>
            <code className="obs-promql" data-copy="data" data-testid="obs-rule-promql">{rule.expr}</code>
          </div>
        ) : null}
        <label className="inf-field">
          <span>Why (kept with the change)</span>
          <input value={reason} onChange={(e) => setReason(e.target.value)} data-testid="obs-rule-reason" autoComplete="off" />
        </label>
        {error ? <span className="inf-error" data-testid="obs-rule-error" data-copy="data">{error}</span> : null}
        <div className="inf-actions">
          <span className="ag-spacer" />
          <button type="button" className="ag-btn" onClick={onClose}>Cancel</button>
          <GatedButton permission="observability:manage" type="button" className="ag-btn ag-btn-primary" data-testid="obs-rule-save"
                       disabled={busy || (custom && (!alert.trim() || !expr.trim()))} aria-busy={busy || undefined} onClick={save}>
            {busy ? "Saving…" : rule ? "Save rule" : "Add rule"}
          </GatedButton>
        </div>
      </div>
    </Sheet>
  );
}
