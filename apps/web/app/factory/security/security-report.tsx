"use client";

/**
 * The before/after (spec §4.6 "Factory ▸ build ▸ Security report"):
 *
 *   8 of 8 targeted fixed · 20 found before · 15 fixed · 5 still open · 0 new
 *   1a2b3c4 → 9f8e7d6                                         [Markdown]
 *   Critical ▮ 0 → 0   High ▮▮▮ 3 → 0   Medium ▮▮▮▮ 9 → 4 …
 *   Targeted   SQL injection in findUser   Fixed
 *   New        None (a pass needs none high or critical)
 *
 * Read from `GET /v1/security/reports/{app}?before&after` — the same classification the re-review
 * judges by — and exported as Markdown by the same route (`format=md`).
 */
import React, { useEffect, useMemo, useState } from "react";

import {
  SEVERITIES, TOOL_LABEL, reportHeadline, reportScans, securityApi, shortSha, targetRows,
  type Report, type Review,
} from "../../../lib/security-review";
import { SevPill, errorWords } from "./security-bits";

export type SecurityReportProps = {
  projectId: string; appKey: string;
  /** The review whose targets are judged; or give `before`/`after` for a plain comparison. */
  review?: Review; before?: string; after?: string;
  onBack?: () => void;
};

export function SecurityReport({ projectId, appKey, review, before, after, onBack }: SecurityReportProps) {
  const scans = review ? reportScans(review) : before && after ? { before, after } : null;
  const [rep, setRep] = useState<Report | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    if (!scans) return;
    let stop = false;
    securityApi.report(projectId, appKey, scans.before, scans.after)
      .then((r) => { if (!stop) setRep(r); }).catch((e) => { if (!stop) setErr(errorWords(e)); });
    return () => { stop = true; };
  }, [projectId, appKey, scans?.before, scans?.after]); // eslint-disable-line react-hooks/exhaustive-deps

  const targeted = useMemo(() => (review ? review.recommendations.filter((r) => r.selected).flatMap((r) => r.fingerprints) : []), [review]);
  const targets = useMemo(() => (rep ? targetRows(rep, targeted) : []), [rep, targeted]);
  const max = rep ? Math.max(1, ...SEVERITIES.flatMap((s) => [rep.severityBefore[s], rep.severityAfter[s]])) : 1;
  const passed = rep && targets.length ? targets.every((t) => ["Fixed", "Removed", "Justified"].includes(t.outcome))
    && rep.summary.newHighOrCritical === 0 : null;

  return (
    <section className="sec-report" data-testid="sec-report" aria-label="Security report">
      <header className="sec-head">
        {onBack ? <button type="button" className="ag-link" onClick={onBack} data-testid="sec-report-back">← Security</button> : null}
        <h2 className="sec-title">Security report</h2>
        {rep ? (
          <span className="sec-shas" data-testid="sec-report-shas" title="Before → after: the commits scanned">
            {shortSha(rep.before)} → {shortSha(rep.after)}
          </span>
        ) : null}
        {scans ? (
          <a className="ag-btn sec-push" href={securityApi.reportMarkdownHref(projectId, appKey, scans.before, scans.after)}
             download={`security-report-${appKey}.md`} data-testid="sec-report-md" title="Download as Markdown">Markdown</a>
        ) : null}
      </header>
      {!scans ? <p className="sec-muted">No re-scan yet, so there is no after to compare.</p> : null}
      {err ? <p className="sec-error" role="alert">{err}</p> : null}
      {scans && !rep && !err ? <p className="sec-muted">Loading…</p> : null}
      {rep ? (
        <>
          <p className="sec-headline" data-testid="sec-report-headline">
            {passed !== null ? <span className="sec-verdict" data-pass={passed}>{passed ? "Passed" : "Did not pass"}</span> : null}
            {reportHeadline(rep, targets)}
          </p>
          {rep.rulesDiffer ? (
            <p className="sec-warn" role="note" data-testid="sec-rules-differ"
               title={rep.differences.map((d) => `${d.tool} ${d.field}: ${d.before} → ${d.after}`).join("\n")}>
              The two scans ran different tool versions or rules: a disappearance may be the rules', not the fix's.
            </p>
          ) : null}
          <div className="sec-bars" role="table" aria-label="Findings by severity, before and after">
            {SEVERITIES.map((s) => (
              <div key={s} className="sec-bar-row" role="row" data-testid={`sec-bar-${s}`}>
                <span role="rowheader" className="sec-bar-label"><SevPill severity={s} /></span>
                <span role="cell" className="sec-bar-pair">
                  <span className="sec-bar" data-when="before" data-sev={s} style={{ width: `${(rep.severityBefore[s] / max) * 100}%` }} />
                  <span className="sec-bar" data-when="after" data-sev={s} style={{ width: `${(rep.severityAfter[s] / max) * 100}%` }} />
                </span>
                <span role="cell" className="sec-bar-num" title="Before → after">{rep.severityBefore[s]} → {rep.severityAfter[s]}</span>
              </div>
            ))}
          </div>
          {review ? (
            <div className="sec-card" data-testid="sec-targets">
              <h3>Targeted ({targets.length})</h3>
              {targets.length ? (
                <ul className="sec-list">
                  {targets.map((t) => (
                    <li key={t.fingerprint} className="sec-row">
                      <SevPill severity={t.severity} />
                      <span className="sec-finding-title" data-user-text>{t.title}</span>
                      <span className="sec-where" data-user-text>{t.where}</span>
                      <span className="sec-outcome" data-outcome={t.outcome}>{t.outcome}</span>
                    </li>
                  ))}
                </ul>
              ) : <p className="sec-muted">Nothing was chosen to fix.</p>}
            </div>
          ) : null}
          <div className="sec-card" data-testid="sec-new">
            <h3 title="A pass needs no new high or critical finding">New ({rep.new.length})</h3>
            {rep.new.length ? (
              <ul className="sec-list">
                {rep.new.map((f) => (
                  <li key={f.fingerprint} className="sec-row">
                    <SevPill severity={f.severity} />
                    <span className="sec-finding-title" data-user-text>{f.title}</span>
                    <span className="sec-where" data-user-text>{f.where}</span>
                    <span className="sec-tool">{TOOL_LABEL[f.tool] ?? f.tool}</span>
                  </li>
                ))}
              </ul>
            ) : <p className="sec-muted">None.</p>}
          </div>
          {rep.stillOpen.length ? (
            <details className="sec-card" data-testid="sec-still-open">
              <summary><h3>Still open ({rep.stillOpen.length})</h3></summary>
              <ul className="sec-list">
                {rep.stillOpen.map((f) => (
                  <li key={f.fingerprint} className="sec-row">
                    <SevPill severity={f.severity} />
                    <span className="sec-finding-title" data-user-text>{f.title}</span>
                    <span className="sec-where" data-user-text>{f.where}</span>
                    <span className="sec-tool">{f.triageReason ? "Justified" : (TOOL_LABEL[f.tool] ?? f.tool)}</span>
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
          <p className="sec-muted sec-tools-line">
            {rep.after.tools.map((t) => `${TOOL_LABEL[t.name] ?? t.name} ${t.version || "—"}${t.status === "failed" ? " (failed)" : ""}`).join(" · ")}
          </p>
        </>
      ) : null}
    </section>
  );
}
