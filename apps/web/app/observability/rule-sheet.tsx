"use client";

/**
 * Edit a rule (or add one), the side sheet (BOSSAPP-598 mockup):
 *
 *   Error rate alert
 *     Fire when    errors (5xx) are above [ 5 ]% of requests
 *     Over         [ 5 ] min, with at least [ 20 ] requests
 *     Hold         [ 0 ] min before it fires          (Prometheus `for`)
 *     Keep firing  [ 0 ] min after it clears       (Prometheus `keep_firing_for`)
 *     Severity     ( ) SEV1 (•) SEV2 ( ) SEV3 ( ) SEV4
 *     When it fires  [x] An agent triages it   [x] Respond automatically (asks your consent once)
 *     Notify       [x] Console [x] On-call email [ ] #ops Slack     (PUT /v1/alert-rules/{id}/notify)
 *     Runbook      [ boss:runbook/app-serving ]
 *     PromQL       100 * sum(increase(http_requests_total{…}[5m])) / … > 5 and …   (read only)
 *                  a rule of your own: [Preview] → "Would have fired 2 times in the last 24 h"
 *                                                          [ Cancel ]  [ Save rule ]
 *
 * A built-in rule is changed by its settings (its PromQL follows them); a rule of your own is
 * PromQL, in the Prometheus alerting-rule shape. The server checks every value and says why.
 *
 * TUNE (P1-C) is this sheet. Preview replays the NEW values over the last 24 h next to the rule as
 * it is. A rule the repo owns (boss.deploy.yaml, `tuneVia: pull_request`) shows where it lives
 * (path:line) and its primary press is Propose PR: a pull request that changes only that rule's
 * lines, its row is not overwritten. Any other rule is saved here, audited.
 */
import React, { useState } from "react";

import { GatedButton } from "../../components/gated";
import { SEVERITIES, forWord, proposePath, rulesPutPath, tunePreviewPath, whereWord, type AlertRule, type RuleKind, type Sev, type SevInfo } from "../../lib/app-alerts";
import { showToast } from "../client-forms";
import { Sheet } from "../infra/kit";
import { obsWrite } from "./obs-write";
import { useContactPoints, usePolicies } from "../../lib/monitoring";
import { RuleFacts } from "./rule-facts";
import { SevFlag, sevTip } from "./severity";

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

export function RuleSheet({ rule, targetId, projectId, actor, onClose, onSaved, severities = SEVERITIES }: {
  rule: AlertRule | null; targetId: string; projectId: string; actor: string; onClose: () => void; onSaved: () => void;
  severities?: SevInfo[];
}) {
  const custom = !rule || rule.kind === "custom";
  const [params, setParams] = useState<Record<string, string>>(
    Object.fromEntries(Object.entries(rule?.params ?? {}).map(([k, v]) => [k, String(v)])));
  const [hold, setHold] = useState(String(Math.round((rule?.forSeconds ?? 0) / 60)));
  const [keep, setKeep] = useState(String(Math.round((rule?.facts.keepFiringForS ?? 0) / 60)));
  const [severity, setSeverity] = useState<Sev>(rule?.severity ?? "sev3");
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
  const [preview, setPreview] = useState<{ word: string; now?: string; bad: boolean } | null>(null);
  const [proposed, setProposed] = useState<{ url: string | null; number: number | null } | null>(null);
  const repo = rule?.tuneVia === "pull_request";
  const holdMin = Number(hold) || 0;
  /** The rule's own values as the change: all of them (Preview), or only what changed (Propose PR). */
  const tuneChange = (onlyChanged: boolean): Record<string, unknown> => {
    const c: Record<string, unknown> = {};
    if (custom) { if (!onlyChanged || expr !== rule?.expr) c.expr = expr; }
    else {
      const p = Object.fromEntries(Object.entries(params).map(([k, v]) => [k, Number(v)]));
      const moved = Object.fromEntries(Object.entries(p).filter(([k, v]) => v !== rule?.params[k]));
      if (!onlyChanged) c.params = p; else if (Object.keys(moved).length) c.params = moved;
    }
    if (!onlyChanged || holdMin * 60 !== (rule?.forSeconds ?? 0)) c.for = forWord(holdMin * 60);
    if (onlyChanged && severity !== rule?.severity) c.severity = severity;
    return c;
  };
  const runPreview = async () => {
    setPreview(null);
    if (rule) {
      const r = await obsWrite<{ now?: { word?: string }; tuned?: { word?: string } }>(tunePreviewPath(targetId, rule.id),
        { project_id: projectId, change: tuneChange(false) }, { scope: `obs:rule-tune-preview:${rule.id}`, label: "Preview" });
      setPreview(r.ok ? { word: r.body?.tuned?.word ?? "No answer", now: r.body?.now?.word, bad: false } : { word: r.error ?? "No preview", bad: true });
      return;
    }
    const r = await obsWrite<{ word?: string }>("/v1/alert-rules/preview", { project_id: projectId, subject_id: targetId, expr,
      for: forWord(holdMin * 60) }, { scope: `obs:rule-preview:${targetId}`, label: "Preview" });
    setPreview(r.ok ? { word: r.body?.word ?? "No answer", bad: false } : { word: r.error ?? "No preview", bad: true });
  };
  const spec = rule && rule.kind !== "custom" ? FIELDS[rule.kind] : null;
  const title = spec ? spec.head : rule ? `${rule.alert} alert` : "Add a rule";

  const saveNotify = async (): Promise<boolean> => {
    if (!rule || !notify) return true;
    const n = await obsWrite(`/v1/alert-rules/${encodeURIComponent(rule.id)}/notify`, { project_id: projectId, actor, contact_point_ids: [...notify] },
                             { scope: `obs:rule-notify:${rule.id}`, label: "Notify" }, "PUT");
    if (!n.ok) setError(`Rule saved, but who is told was not: ${n.error ?? "refused"}`);
    return n.ok;
  };

  /** A repo rule: its yaml values go to a pull request; BOSS's own switches are saved here. */
  const propose = async (r: AlertRule) => {
    const change = tuneChange(true);
    const keepS = (Number(keep) || 0) * 60;
    const side: Record<string, unknown> = {};
    if (triage !== r.triage) side.triage = triage;
    if (respond !== r.autoRespond) side.auto_respond = respond;
    if (keepS !== r.facts.keepFiringForS) side.keep_firing_for = forWord(keepS);
    if (!Object.keys(change).length && !Object.keys(side).length && !notify) { setError("Nothing to change"); return; }
    setBusy(true);
    setError(null);
    if (Object.keys(change).length) {
      const p = await obsWrite<{ pull_request?: { url?: string; number?: number } }>(proposePath(targetId, r.id),
        { project_id: projectId, actor, reason, change }, { scope: `obs:rule-propose:${r.id}`, label: "Propose PR" });
      if (!p.ok) { setBusy(false); setError(p.error ?? "Not proposed"); return; }
      setProposed({ url: p.body?.pull_request?.url ?? null, number: p.body?.pull_request?.number ?? null });
    }
    if (Object.keys(side).length) {
      const w = await obsWrite(rulesPutPath(targetId), { project_id: projectId, actor, reason, rules: [{ id: r.id, ...side }] },
                               { scope: `obs:rule:${r.id}`, label: "Save" }, "PUT");
      if (!w.ok) { setBusy(false); setError(w.error ?? "Not saved"); return; }
    }
    const told = await saveNotify();
    setBusy(false);
    if (!told) return;
    if (!Object.keys(change).length) { showToast("success", "Saved"); onSaved(); return; }
    showToast("success", "Pull request opened");
  };

  const save = async () => {
    if (rule && repo) { await propose(rule); return; }
    setBusy(true);
    setError(null);
    const change: Record<string, unknown> = custom
      ? { alert, expr, for: forWord(holdMin * 60), keep_firing_for: forWord((Number(keep) || 0) * 60), labels: { severity }, annotations: { summary, ...(runbook ? { runbook_url: runbook } : {}) },
          enabled, triage, auto_respond: respond }
      : { params: Object.fromEntries(Object.entries(params).map(([k, v]) => [k, Number(v)])), for: forWord(holdMin * 60),
          keep_firing_for: forWord((Number(keep) || 0) * 60), severity, enabled, triage, auto_respond: respond, runbook_url: runbook };
    if (rule) change.id = rule.id;
    const r = await obsWrite(rulesPutPath(targetId), { project_id: projectId, actor, reason, rules: [change] },
                             { scope: `obs:rule:${rule?.id ?? "new"}`, label: rule ? "Save" : "Add rule" }, "PUT");
    setBusy(false);
    if (!r.ok) { setError(r.error ?? "Not saved"); return; }
    if (rule && notify) {
      setBusy(true);
      const ok = await saveNotify();
      setBusy(false);
      if (!ok) return;
    }
    showToast("success", rule ? (notify ? `Rule saved; ${notify.size ? `tells ${notify.size} contact point${notify.size === 1 ? "" : "s"}` : "tells the console only"}` : "Rule saved") : "Rule added");
    onSaved();
  };

  return (
    <Sheet title={title} testId="obs-rule-sheet" onClose={onClose}>
      <div className="obs-rform">
        {repo ? (
          <div className="obs-rrow" data-testid="obs-rule-where">
            <span className="obs-rlabel" title="Declared in the app's repo: Propose PR changes only these lines">Lives in</span>
            <span className="obs-rbody">
              {rule?.where ? (rule.where.href
                ? <a className="inf-link inf-mono" href={rule.where.href} target="_blank" rel="noreferrer noopener" data-copy="data"
                     title={`Lines ${rule.where.line}, ${rule.where.endLine} of ${rule.where.path}, as deployed`}>{whereWord(rule.where)} </a>
                : <span className="inf-mono" data-copy="data">{whereWord(rule.where)}</span>)
                : <span className="inf-mono" title="Its line is found when the pull request is opened">boss.deploy.yaml</span>}
            </span>
          </div>
        ) : null}
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
            <label className="inf-field">
              <span>Summary</span>
              <input value={summary} onChange={(e) => setSummary(e.target.value)} data-testid="obs-rule-summary" autoComplete="off" />
            </label>
          </>
        ) : null}
        <div className="obs-rrow">
          <span className="obs-rlabel" title="Prometheus' for: how long it must stay breached">For</span>
          <span className="obs-rbody">
            <NumberField f={{ key: "for", before: "", after: "min before it fires", tip: "0 fires on the first breach" }}
                         value={hold} bounds={[0, 1440]} onChange={setHold} />
          </span>
        </div>
        <div className="obs-rrow">
          <span className="obs-rlabel" title="Prometheus' keep_firing_for: stays firing this long after it clears">Keep firing</span>
          <span className="obs-rbody">
            <NumberField f={{ key: "keep_firing_for", before: "", after: "min after it clears", tip: "0 resolves on the first clear check" }}
                         value={keep} bounds={[0, 1440]} onChange={setKeep} />
          </span>
        </div>
        <div className="obs-rrow">
          <span className="obs-rlabel" title="Replays these values over the app's last 24 hours; nothing is saved">Last 24 h</span>
          <span className="obs-rbody">
            <button type="button" className="ag-btn ag-btn-sm" data-testid="obs-rule-preview" disabled={custom && !expr.trim()}
                    title="How often these values would have fired in the last 24 hours" onClick={() => void runPreview()}>Preview</button>
            {preview ? (
              <span className={preview.bad ? "inf-error" : "obs-strong"} data-testid="obs-rule-preview-word" data-copy="data"
                    title={preview.now ? `As it is now: ${preview.now}` : undefined}>{preview.word}</span>
            ) : null}
            {preview?.now ? <span className="inf-faint" data-testid="obs-rule-preview-now" data-copy="data">now: {preview.now}</span> : null}
          </span>
        </div>
        <div className="obs-rrow" role="radiogroup" aria-label="Severity">
          <span className="obs-rlabel">Severity</span>
          <span className="obs-rbody">
            {severities.map((s) => (
              <label key={s.key} className="obs-rcheck" title={sevTip(s.key, severities)}>
                <input type="radio" name="severity" checked={severity === s.key} onChange={() => setSeverity(s.key)} data-testid={`obs-rule-sev-${s.key}`} />
                <SevFlag sev={s.key} catalog={severities} />
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
          <input value={runbook} onChange={(e) => setRunbook(e.target.value)} data-testid="obs-rule-runbook" autoComplete="off" disabled={repo}
                 spellCheck={false} placeholder="docs/runbooks/checkout.md"
                 title={repo ? "Set in boss.deploy.yaml: change it in the repo" : "A markdown page kept with the app, or a URL"} />
        </label>
        <label className="obs-rcheck">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} data-testid="obs-rule-enabled" disabled={repo}
                 title={repo ? "Set in boss.deploy.yaml: change it in the repo" : undefined} />
          <span>Rule on</span>
        </label>
        {rule ? (
          <div className="obs-adetail-row" data-testid="obs-rule-promql-facts">
            <span className="obs-fact-k" title="As saved; follows the settings above when you save">Now</span>
            {!custom ? <code className="obs-promql" data-copy="data" data-testid="obs-rule-promql">{rule.expr}</code> : null}
            <RuleFacts r={rule} severities={severities} />
          </div>
        ) : null}
        <label className="inf-field">
          <span>Why (kept with the change)</span>
          <input value={reason} onChange={(e) => setReason(e.target.value)} data-testid="obs-rule-reason" autoComplete="off" />
        </label>
        {error ? <span className="inf-error" data-testid="obs-rule-error" data-copy="data">{error}</span> : null}
        {proposed ? (
          <span className="obs-line" data-testid="obs-rule-proposed">
            <span className="obs-strong">Proposed</span>
            {proposed.url ? <a className="inf-link" href={proposed.url} target="_blank" rel="noreferrer noopener" data-copy="data"
                               title="Takes effect when it merges and the app deploys">PR{proposed.number ? ` #${proposed.number}` : ""} ↗</a> : null}
          </span>
        ) : null}
        <div className="inf-actions">
          <span className="ag-spacer" />
          {proposed ? (
            <button type="button" className="ag-btn ag-btn-primary" data-testid="obs-rule-done" onClick={onSaved}>Done</button>
          ) : (
            <>
              <button type="button" className="ag-btn" onClick={onClose}>Cancel</button>
              <GatedButton permission="observability:manage" type="button" className="ag-btn ag-btn-primary" data-testid="obs-rule-save"
                           data-via={repo ? "pull_request" : "console"}
                           title={repo ? "Opens a pull request changing only this rule; applies once merged and deployed"
                             : rule ? "Saved now; the change is recorded with who and why" : undefined}
                           disabled={busy || (custom && (!alert.trim() || !expr.trim()))} aria-busy={busy || undefined} onClick={save}>
                {busy ? (repo ? "Proposing…" : "Saving…") : !rule ? "Add rule" : repo ? "Propose PR" : "Save"}
              </GatedButton>
            </>
          )}
        </div>
      </div>
    </Sheet>
  );
}
