"use client";

/**
 * App › Security (APP_TEMPLATES_AND_SECURITY_LOOP.md §4.6): one surface for an app's security.
 *
 *   Security · Shift swap board                         [Scan now] [Security review]
 *   Last scan  2 min ago · 1a2b3c4 · Code 3 · Dependencies 3 · Running app 14
 *   Review     Scan ✓ · Panel ✓ · Choose (needs you) · Fix · Re-scan · …       [Report]
 *              ☑ P0 Parameterise the user lookup … [Fix selected]
 *   Findings   [Tool ▾] [Severity ▾] [Status ▾]   row → evidence + Accept / False positive
 *
 * Reads `/v1/security/*` with the person's session; every write is pre-greyed for a viewer who
 * cannot make it (security_scan:run, security_finding:triage) and the API decides again.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";

import { GatedButton, useGate } from "../../../components/gated";
import {
  NO_FILTER, SEVERITIES, STATUS_LABEL, STEP_LABEL, STEP_TIP, TOOLS, TOOL_LABEL, TOOL_TIP,
  filterFindings, reportScans, reviewWord, securityApi, shortSha, stepWord, whereOf,
  type Finding, type FindingFilter, type Review, type Scan,
} from "../../../lib/security-review";
import { ReviewChecklist } from "./review-checklist";
import { SecurityReport } from "./security-report";
import { ago, localTime, useNow } from "../../agents/live-util";
import { SevPill, errorWords } from "./security-bits";

const POLL_MS = 8000;
/** Why Accept and False positive are disabled before a reason is typed. */
export const NEEDS_REASON = "Say why first: a few words, kept on the finding";
const SEV_SHORT: Record<string, string> = { critical: "Crit", high: "High", medium: "Med", low: "Low", info: "Info" };

export type AppSecurityProps = { projectId: string; appKey: string; appName?: string };

export function AppSecurity({ projectId, appKey, appName }: AppSecurityProps) {
  const [scans, setScans] = useState<Scan[] | null>(null);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [scanId, setScanId] = useState<string>("");
  const [findings, setFindings] = useState<Finding[]>([]);
  const [filter, setFilter] = useState<FindingFilter>(NO_FILTER);
  const [open, setOpen] = useState<string>("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"" | "scan" | "review">("");
  const [report, setReport] = useState<Review | null>(null);
  const [tick, setTick] = useState(0);
  const now = useNow(30_000);

  const load = useCallback(async () => {
    try {
      const [s, r] = await Promise.all([securityApi.scans(projectId, appKey), securityApi.reviews(projectId, appKey)]);
      setScans(s);
      setReviews(r);
      setError("");
    } catch (e) {
      setScans((prev) => prev ?? []);
      setError(errorWords(e));
    }
  }, [projectId, appKey]);

  useEffect(() => { void load(); }, [load, tick]);
  const live = (scans ?? []).some((s) => s.status === "queued" || s.status === "running")
    || reviews.some((r) => r.status === "running" || r.status === "waiting");
  useEffect(() => {
    if (!live) return undefined;
    const id = window.setInterval(() => setTick((t) => t + 1), POLL_MS);
    return () => window.clearInterval(id);
  }, [live]);

  // The scan shown: the one picked, else the newest that finished, else the newest.
  const shown = useMemo(() => {
    const all = scans ?? [];
    return all.find((s) => s.id === scanId) ?? all.find((s) => s.status === "done") ?? all[0] ?? null;
  }, [scans, scanId]);
  useEffect(() => {
    if (!shown || shown.status !== "done") { setFindings([]); return; }
    let stop = false;
    securityApi.findings(shown.id).then((f) => { if (!stop) setFindings(f); }).catch((e) => { if (!stop) setError(errorWords(e)); });
    return () => { stop = true; };
  }, [shown?.id, shown?.status, tick]); // eslint-disable-line react-hooks/exhaustive-deps

  async function scanNow() {
    setBusy("scan");
    try { await securityApi.scanNow(projectId, appKey); setTick((t) => t + 1); }
    catch (e) { setError(errorWords(e)); }
    finally { setBusy(""); }
  }
  async function startReview() {
    setBusy("review");
    try { await securityApi.startReview(projectId, appKey, ""); setTick((t) => t + 1); }
    catch (e) { setError(errorWords(e)); }
    finally { setBusy(""); }
  }

  const review = reviews[0] ?? null;
  const visible = filterFindings(findings, filter);

  if (report) {
    return (
      <div className="sec" data-testid="sec-app">
        <SecurityReport projectId={projectId} appKey={appKey} review={report} onBack={() => setReport(null)} />
      </div>
    );
  }
  return (
    <div className="sec" data-testid="sec-app">
      <header className="sec-head">
        <h2 className="sec-title">Security</h2>
        {appName ? <span className="sec-sub" data-user-text>{appName}</span> : null}
        <span className="sec-acts">
          <GatedButton permission="security_scan:run" projectId={projectId} className="ag-btn" data-testid="sec-scan-now"
                       title="Run Semgrep, OSV-Scanner and ZAP on this app now" disabled={busy !== ""}
                       aria-busy={busy === "scan" || undefined} onClick={() => void scanNow()}>Scan now</GatedButton>
          <GatedButton permission="security_scan:run" projectId={projectId} className="ag-btn ag-btn-primary"
                       data-testid="sec-review-start" disabled={busy !== "" || !!(review && reviewWord(review) === "Running")}
                       title="Scan, a panel recommends, you choose, BOSS fixes and re-scans"
                       aria-busy={busy === "review" || undefined} onClick={() => void startReview()}>Security review</GatedButton>
        </span>
      </header>
      {error ? <p className="sec-error" role="alert" data-testid="sec-error">{error}</p> : null}

      {scans === null ? <p className="sec-muted">Loading…</p> : null}
      {scans && !scans.length ? (
        <p className="sec-empty" data-testid="sec-no-scans">Not scanned yet. Scan now finds flaws in its code, dependencies and running app.</p>
      ) : null}
      {shown ? <ScanSummary scan={shown} scans={scans ?? []} onPick={setScanId} now={now} /> : null}

      {review ? (
        <section className="sec-card" data-testid="sec-review" aria-label="Security review">
          <div className="sec-card-head">
            <h3>Review</h3>
            <span className="sec-word" data-testid="sec-review-word">{reviewWord(review)}</span>
            {review.status === "completed" && reportScans(review) ? (
              <button type="button" className="ag-btn sec-push" data-testid="sec-open-report"
                      title="Before vs after: fixed, still open, new" onClick={() => setReport(review)}>Report</button>
            ) : null}
          </div>
          <ol className="sec-steps" aria-label="Steps">
            {review.steps.map((s) => (
              <li key={s.id} className="sec-step" data-state={stepWord(s)} title={`${STEP_TIP[s.id] ?? s.id}${s.note ? ` — ${s.note}` : ""}${s.error ? ` — ${s.error}` : ""}`}>
                <span className="sec-step-name">{STEP_LABEL[s.id] ?? s.id}</span>
                <span className="sec-step-word">{stepWord(s)}</span>
              </li>
            ))}
          </ol>
          {review.awaitingChoice ? (
            <ReviewChecklist projectId={projectId} runId={review.runId} onDone={() => setTick((t) => t + 1)} />
          ) : null}
        </section>
      ) : null}

      {shown && shown.status === "done" ? (
        <section className="sec-card" aria-label="Findings" data-testid="sec-findings">
          <div className="sec-card-head">
            <h3>Findings</h3>
            <span className="sec-muted">{visible.length} of {findings.length}</span>
          </div>
          <div className="sec-filters">
            <Select label="Tool" value={filter.tool} onChange={(tool) => setFilter({ ...filter, tool })}
                    options={TOOLS.map((t) => [t, TOOL_LABEL[t]])} testId="sec-filter-tool" />
            <Select label="Severity" value={filter.severity} onChange={(severity) => setFilter({ ...filter, severity })}
                    options={SEVERITIES.map((s) => [s, s[0].toUpperCase() + s.slice(1)])} testId="sec-filter-severity" />
            <Select label="Status" value={filter.status} onChange={(status) => setFilter({ ...filter, status })}
                    options={Object.entries(STATUS_LABEL).filter(([k]) => k !== "fixed")} testId="sec-filter-status" />
          </div>
          {visible.length ? (
            <ul className="sec-list">
              {visible.map((f) => (
                <FindingRow key={f.id} f={f} open={open === f.id} projectId={projectId}
                            onToggle={() => setOpen(open === f.id ? "" : f.id)}
                            onTriaged={(next) => setFindings(findings.map((x) => (x.id === next.id ? next : x)))} />
              ))}
            </ul>
          ) : <p className="sec-muted" data-testid="sec-none">{findings.length ? "None match." : "Nothing found."}</p>}
        </section>
      ) : null}
    </div>
  );
}

function Select({ label, value, onChange, options, testId }: {
  label: string; value: string; onChange: (v: string) => void; options: [string, string][]; testId: string;
}) {
  return (
    <label className="sec-select">
      <span className="sr-only">{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)} data-testid={testId} aria-label={label}>
        <option value="">{label}: all</option>
        {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
      </select>
    </label>
  );
}

function ScanSummary({ scan, scans, onPick, now }: { scan: Scan; scans: Scan[]; onPick: (id: string) => void; now: number | null }) {
  const when = (s: Scan) => (ago(s.endedAt || s.startedAt, now) || "—") + (now !== null && (s.endedAt || s.startedAt) ? " ago" : "");
  const running = scan.status === "queued" || scan.status === "running";
  return (
    <section className="sec-card" aria-label="Last scan" data-testid="sec-summary">
      <div className="sec-card-head">
        <h3>{running ? "Scanning" : "Last scan"}</h3>
        <span className="sec-muted" title={localTime(scan.endedAt || scan.startedAt)}>
          {when(scan)}{scan.commit ? ` · ${shortSha(scan)}` : ""}
          {scan.zapMode === "api" ? " · active API scan" : ""}
        </span>
        {scans.length > 1 ? (
          <label className="sec-select sec-push">
            <span className="sr-only">Scan</span>
            <select value={scan.id} onChange={(e) => onPick(e.target.value)} aria-label="Pick a scan" data-testid="sec-pick-scan">
              {scans.map((s) => <option key={s.id} value={s.id}>{`${when(s)} · ${s.commit ? s.commit.slice(0, 7) : s.status}`}</option>)}
            </select>
          </label>
        ) : null}
      </div>
      {scan.status === "failed" ? <p className="sec-error" role="status">Scan failed{scan.error ? `: ${scan.error}` : ""}</p> : null}
      {scan.status === "done" ? (
        <div className="sec-table" role="table" aria-label="Findings by tool and severity">
          <div className="sec-tr sec-th" role="row">
            <span role="columnheader">Tool</span>
            {SEVERITIES.map((s) => <span key={s} role="columnheader" className="sec-num" title={s}>{SEV_SHORT[s]}</span>)}
            <span role="columnheader" className="sec-ver">Version</span>
          </div>
          {TOOLS.map((t) => {
            const meta = scan.tools.find((x) => x.name === t);
            if (!meta) return null;
            const c = scan.byTool[t];
            return (
              <div key={t} className="sec-tr" role="row" data-testid={`sec-tool-${t}`}>
                <span role="cell" title={TOOL_TIP[t]}>{TOOL_LABEL[t]}</span>
                {meta.status === "failed" ? (
                  <span role="cell" className="sec-failed" title={meta.error}>Failed{meta.error ? `: ${meta.error}` : ""}</span>
                ) : SEVERITIES.map((s) => <span key={s} role="cell" className="sec-num" data-sev={c?.[s] ? s : undefined}>{c?.[s] ?? 0}</span>)}
                <span role="cell" className="sec-ver" title={meta.rulesDigest ? `rules ${meta.rulesDigest}` : undefined}>{meta.version || "—"}</span>
              </div>
            );
          })}
        </div>
      ) : null}
    </section>
  );
}

function FindingRow({ f, open, projectId, onToggle, onTriaged }: {
  f: Finding; open: boolean; projectId: string; onToggle: () => void; onTriaged: (f: Finding) => void;
}) {
  const [reason, setReason] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const gate = useGate("security_finding:triage", projectId);
  /** Accept and False positive need a reason: they stay disabled, and say so, until there is one. */
  const reasoned = reason.trim().length >= 3;
  async function triage(status: "accepted" | "false_positive") {
    if (!reasoned) { setErr(NEEDS_REASON); return; }
    setBusy(true);
    try {
      await securityApi.triage(f.id, status, reason.trim());
      onTriaged({ ...f, status, triageReason: reason.trim() });
      setErr("");
    } catch (e) { setErr(errorWords(e)); }
    finally { setBusy(false); }
  }
  const ev = f.evidence as Record<string, unknown>;
  const snippet = String(ev.snippet ?? ev.excerpt ?? ev.summary ?? "");
  return (
    <li className="sec-finding" data-testid={`sec-finding-${f.id}`} data-open={open || undefined}>
      <button type="button" className="sec-finding-row" aria-expanded={open} onClick={onToggle}>
        <SevPill severity={f.severity} />
        <span className="sec-finding-title" data-user-text>{f.title}</span>
        <span className="sec-where" data-user-text>{whereOf(f)}</span>
        <span className="sec-tool" title={TOOL_TIP[f.tool]}>{TOOL_LABEL[f.tool] ?? f.tool}</span>
        <span className="sec-status" data-status={f.status}>{STATUS_LABEL[f.status] ?? f.status}</span>
      </button>
      {open ? (
        <div className="sec-detail">
          <dl className="sec-dl">
            <dt>Rule</dt><dd data-user-text>{f.ruleId}</dd>
            {f.cwe ? <><dt>CWE</dt><dd>{f.cwe}</dd></> : null}
            {f.owasp ? <><dt>OWASP</dt><dd>{f.owasp}</dd></> : null}
            {f.confidence ? <><dt>Confidence</dt><dd>{f.confidence}</dd></> : null}
          </dl>
          {snippet ? <pre className="sec-evidence" data-user-text title="Redacted before it was stored">{snippet}</pre> : null}
          {f.status === "accepted" || f.status === "false_positive" ? (
            <p className="sec-muted">{STATUS_LABEL[f.status]}{f.triagedBy ? ` by ${f.triagedBy}` : ""}: <span data-user-text>{f.triageReason}</span></p>
          ) : (
            <div className="sec-triage" data-can={gate.allowed ? "yes" : "no"}>
              {/* GREYED WITH THE REASON for a viewer or auditor, like Scan now (tester P2): the
                  field and both buttons are there, disabled, and say why. */}
              <input className="cap-input" value={reason} onChange={(e) => { setReason(e.target.value); setErr(""); }} maxLength={2000}
                     placeholder="Why (required)" aria-label="Why" data-testid={`sec-reason-${f.id}`}
                     disabled={!gate.allowed} title={gate.allowed ? "Say why, in a few words: it stays on the finding" : gate.reason} />
              <GatedButton permission="security_finding:triage" projectId={projectId} className="ag-btn" disabled={busy || !reasoned}
                           title={reasoned ? "Keep it, knowingly: it stays listed as accepted" : NEEDS_REASON} data-testid={`sec-accept-${f.id}`}
                           onClick={() => void triage("accepted")}>Accept</GatedButton>
              <GatedButton permission="security_finding:triage" projectId={projectId} className="ag-btn" disabled={busy || !reasoned}
                           title={reasoned ? "Not a real flaw: it stays listed, marked false positive" : NEEDS_REASON} data-testid={`sec-fp-${f.id}`}
                           onClick={() => void triage("false_positive")}>False positive</GatedButton>
              {gate.allowed && !reasoned ? <span className="sec-muted" role="status" data-testid={`sec-reason-why-${f.id}`}>{NEEDS_REASON}</span> : null}
            </div>
          )}
          {err ? <p className="sec-error" role="alert">{err}</p> : null}
        </div>
      ) : null}
    </li>
  );
}

