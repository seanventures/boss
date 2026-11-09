"use client";

import React, { useEffect, useRef } from "react";
import type { WorkAgreement as Agreement } from "../lib/work-agreement";
import { GatedButton } from "./gated";
import styles from "./work-agreement.module.css";

const dollars = (micros: number | null) => micros === null ? "Not recorded yet" : `$${(micros / 1_000_000).toFixed(2)}`;

/** The exact server projection; unknowns remain visible alongside the planned effects. */
export function WorkAgreement({ agreement }: { agreement: Agreement }) {
  const a = agreement;
  return <div className={styles.details} data-testid="work-agreement">
    <p className={styles.lead}>This starts planning from your brief. The workflow below governs the next steps.</p>
    <h3>Your brief</h3><p className={styles.brief}>{a.input.directive}</p>
    <dl className={styles.facts}>
      <dt>Workflow</dt><dd>{a.workflow_name}, version {a.workflow_version}</dd>
      <dt>Repository</dt><dd>{a.repository?.full_name ?? "No unambiguous repository target recorded"}</dd>
      <dt>Base branch</dt><dd>{a.repository?.base_branch ?? "Not recorded yet"}</dd>
      <dt>Base commit</dt><dd>{a.repository?.base_revision ?? "Not read yet"}</dd>
      <dt>Approved specification</dt><dd>Not recorded yet</dd>
      <dt>Acceptance checks</dt><dd>{a.acceptance_criteria.length ? a.acceptance_criteria.join("; ") : "Not recorded yet"}</dd>
      <dt>Test commands</dt><dd>{a.test_commands.length ? a.test_commands.join("; ") : "Not recorded yet"}</dd>
      <dt>Provider / account</dt><dd>Not selected yet; routing happens at each model step</dd>
    </dl>
    {a.repository?.selection === "current_project_target" ? <p className={styles.note}>The current project target is shown for planning. Later task admission resolves the actual build target.</p> : null}
    <h3>Planned steps and stops</h3>
    <p className={styles.note}>Starts at: {a.steps.find((s) => s.id === a.entry_step)?.label ?? a.entry_step}</p>
    <ol className={styles.steps}>{a.steps.map((s) => <li key={s.id}><strong>{s.label}</strong>
      {s.does ? <p>{s.does}</p> : null}
      {s.runs ? <p className={styles.note}>Runs: {s.runs}{s.agent ? `, responsible agent: ${s.agent}` : ""}</p> : null}
      {s.approval_note ? <p className={styles.note}>{s.approval_note}</p> : null}
      {s.next.length ? <p className={styles.note}>Then: {s.next.map((id) => a.steps.find((step) => step.id === id)?.label ?? id).join(", ")}</p> : <p className={styles.note}>End of this path</p>}
      {s.produces?.length ? <p className={styles.note}>Produces: {s.produces.join(", ")}</p> : null}
    </li>)}</ol>
    {a.results.length ? <p>Expected workflow records: {a.results.join(", ")}</p> : null}
    <h3>Money</h3>
    <dl className={styles.facts}>
      <dt>Task estimate</dt><dd>{dollars(a.financial.estimate_usd_micros)}</dd>
      <dt>Reservation</dt><dd>{dollars(a.financial.reservation_usd_micros)}</dd>
      <dt>New authorized limit</dt><dd>{dollars(a.financial.authorized_limit_usd_micros)}</dd>
    </dl>
    {a.financial.history ? <p className={styles.note}>Prior completed runs ({a.financial.history.runs}): ${a.financial.history.low_usd.toFixed(2)}, ${a.financial.history.high_usd.toFixed(2)} API-equivalent value. {a.financial.history_basis} This is historical usage, not a quote, cash charge, reservation or spending cap.</p> : <p className={styles.note}>No completed-run cost history recorded.</p>}
    <h3>Bounds</h3><ul>{a.bounds.map((x) => <li key={x}>{x}</li>)}</ul>
    <h3>Still unknown</h3><ul>{a.unknowns.map((x) => <li key={x}>{x}</li>)}</ul>
    {a.start_anyway.length ? <p>Readiness exceptions you chose: {a.start_anyway.join(", ")}</p> : null}
    <p className={styles.note}>This preview expires after ten minutes. Starting rechecks the current workflow and your permissions. It does not approve later decisions or grant financial authority.</p>
  </div>;
}

export function WorkAgreementDialog({ agreement, loading, error, busy, projectId, pendingStart, checkingStart, onCheckStart, onCancel, onRetry, onConfirm }: {
  agreement: Agreement | null; loading: boolean; error: string; busy: boolean; projectId: string;
  pendingStart: boolean; checkingStart: boolean; onCheckStart: () => void;
  onCancel: () => void; onRetry: () => void; onConfirm: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const cancelRef = useRef(onCancel);
  cancelRef.current = onCancel;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.focus();
    return () => { if (previous?.isConnected) previous.focus(); };
  }, []);
  return <div className={styles.backdrop}>
    <div ref={ref} className={styles.sheet} role="dialog" aria-modal="true" aria-labelledby="work-agreement-title" tabIndex={-1}
      onKeyDown={(e) => {
        if (e.key === "Escape" && !busy) { e.preventDefault(); cancelRef.current(); }
        if (e.key !== "Tab") return;
        const controls = Array.from(ref.current?.querySelectorAll<HTMLElement>("button:not(:disabled), a[href], [tabindex='0']") ?? []);
        const first = controls[0], last = controls[controls.length - 1];
        if (!first) { e.preventDefault(); return; }
        if (e.shiftKey && (document.activeElement === first || document.activeElement === ref.current)) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && (document.activeElement === last || document.activeElement === ref.current)) { e.preventDefault(); first.focus(); }
      }}>
      <header className={styles.header}><p>Before you start</p><h2 id="work-agreement-title">{pendingStart && !busy ? "Check the original start" : "Review the work agreement"}</h2></header>
      <div className={styles.body} aria-busy={loading}>
        {loading ? <p role="status">Reading the current workflow…</p> : null}
        {error ? <p role="alert" className={styles.error}>{error}</p> : null}
        {pendingStart && !busy ? <p>A start request was sent and its outcome is not confirmed here. Check the recorded run before reviewing or starting another intent.</p> : null}
        {agreement ? <WorkAgreement agreement={agreement} /> : null}
      </div>
      <footer className={styles.footer}>
        <button type="button" className="button ghost" disabled={busy} onClick={onCancel}>{pendingStart ? "Close" : "Cancel"}</button>
        {pendingStart && !busy ? <button type="button" className="button" disabled={checkingStart} onClick={onCheckStart} data-testid="work-agreement-check-start">{checkingStart ? "Checking…" : "Check start result"}</button> : null}
        {!pendingStart && !loading && error ? <button type="button" className="button ghost" disabled={busy} onClick={onRetry}>Review again</button> : null}
        <GatedButton permission="workflow_run:start" projectId={projectId} className="button" disabled={pendingStart || !agreement || loading || busy || !!error}
          onClick={onConfirm} data-testid="work-agreement-confirm">{busy ? "Starting…" : "Confirm and start workflow"}</GatedButton>
      </footer>
    </div>
  </div>;
}
