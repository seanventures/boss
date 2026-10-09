"use client";

/**
 * The Panel's recommendations as the checklist that becomes the decision (spec §4.2 step 3, §4.6
 * "Panel"). P0/P1 start ticked; a recommendation the Panel called a false positive does not.
 * "Fix selected" records the ticks (`/v1/security/reviews/{run}/selection`, security_finding:triage)
 * and then approves the review's gate (`/v1/decisions/{id}/approve`), so the fix build starts from
 * exactly what was ticked. Findings left unticked can be accepted, with one reason, in the same act.
 */
import React, { useEffect, useMemo, useState } from "react";

import { GatedButton } from "../../../components/gated";
import { TOOL_LABEL, securityApi, type Review } from "../../../lib/security-review";
import { SevPill, errorWords } from "./security-bits";

export type ReviewChecklistProps = {
  /** The project the gate is checked at; omitted = the console's active project (Meetings). */
  projectId?: string; runId: string; onDone?: () => void; review?: Review;
};

export function ReviewChecklist({ projectId, runId, onDone, review: given }: ReviewChecklistProps) {
  const [review, setReview] = useState<Review | null>(given ?? null);
  const [ticked, setTicked] = useState<Set<string>>(new Set());
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [done, setDone] = useState(false);

  useEffect(() => {
    if (given) return;
    let stop = false;
    securityApi.review(runId).then((r) => { if (!stop) setReview(r); }).catch((e) => { if (!stop) setErr(errorWords(e)); });
    return () => { stop = true; };
  }, [runId, given]);
  useEffect(() => {
    if (review) setTicked(new Set(review.recommendations.filter((r) => r.selected).map((r) => r.id)));
  }, [review]);

  const unticked = useMemo(() => {
    if (!review) return [] as string[];
    const chosen = new Set(review.recommendations.filter((r) => ticked.has(r.id)).flatMap((r) => r.fingerprints));
    return review.recommendations.filter((r) => !ticked.has(r.id)).flatMap((r) => r.fingerprints)
      .filter((fp) => !chosen.has(fp)).map((fp) => review.findings[fp]?.id).filter((id): id is string => !!id);
  }, [review, ticked]);

  if (!review) return err ? <p className="sec-error" role="alert">{err}</p> : <p className="sec-muted">Loading…</p>;

  function flip(id: string) {
    const next = new Set(ticked);
    if (next.has(id)) next.delete(id); else next.add(id);
    setTicked(next);
  }
  async function fix() {
    if (!review) return;
    if (reason.trim() && reason.trim().length < 3) { setErr("Say why in a few words, or leave it empty"); return; }
    setBusy(true);
    try {
      const accept = reason.trim() ? unticked.map((id) => ({ finding_id: id, reason: reason.trim() })) : [];
      await securityApi.choose(runId, [...ticked], accept);
      if (review.decisionId) {
        await securityApi.approve(review.decisionId, `Fix ${ticked.size} of ${review.recommendations.length} recommendations.`);
      }
      setDone(true);
      setErr("");
      onDone?.();
    } catch (e) { setErr(errorWords(e)); }
    finally { setBusy(false); }
  }

  return (
    <div className="sec-check" data-testid="sec-checklist">
      <p className="sec-muted">Tick what to fix. P0 and P1 start ticked; the fix is built from your ticks.</p>
      <ul className="sec-recs">
        {review.recommendations.map((r) => (
          <li key={r.id} className="sec-rec" data-testid={`sec-rec-${r.id}`}>
            <label className="sec-rec-head">
              <input type="checkbox" checked={ticked.has(r.id)} onChange={() => flip(r.id)} disabled={done}
                     data-testid={`sec-tick-${r.id}`} aria-label={`Fix: ${r.title}`} />
              <span className="sec-prio" data-prio={r.priority} title="P0 exploitable now … P3 hygiene">{r.priority}</span>
              <span className="sec-rec-title" data-user-text>{r.title}</span>
              {r.effort ? <span className="sec-effort" title="Effort: S small, M medium, L large">{r.effort}</span> : null}
              <span className="sec-src" data-src={r.source}
                    title={r.source === "panel" ? "Recommended by the security panel" : "Derived by BOSS from a finding the panel did not cover"}>
                {r.source === "panel" ? "Panel" : "Derived"}
              </span>
            </label>
            {r.fix ? <p className="sec-rec-fix" data-user-text>{r.fix}</p> : null}
            {r.riskIfUnfixed ? <p className="sec-muted" data-user-text>Risk: {r.riskIfUnfixed}</p> : null}
            {r.falsePositiveReason ? <p className="sec-fp" data-user-text title="The panel's view; only a person can triage">Panel: false positive — {r.falsePositiveReason}</p> : null}
            <ul className="sec-rec-findings">
              {r.fingerprints.map((fp) => {
                const f = review.findings[fp];
                return (
                  <li key={fp}>
                    {f ? <><SevPill severity={f.severity} /> <span data-user-text>{f.title}</span>
                      <span className="sec-where" data-user-text> {f.where}</span>
                      <span className="sec-tool"> {TOOL_LABEL[f.tool] ?? f.tool}</span></> : <code>{fp.slice(0, 12)}</code>}
                  </li>
                );
              })}
            </ul>
          </li>
        ))}
      </ul>
      {!done ? (
        <div className="sec-check-foot">
          {unticked.length ? (
            <input className="cap-input" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={2000}
                   aria-label="Reason to accept the unticked findings" data-testid="sec-accept-reason"
                   placeholder={`Accept the ${unticked.length} unticked finding${unticked.length === 1 ? "" : "s"}? Say why (optional)`} />
          ) : null}
          <GatedButton permission="security_finding:triage" projectId={projectId} className="ag-btn ag-btn-primary"
                       disabled={busy || !review.decisionId} aria-busy={busy || undefined} data-testid="sec-fix-selected"
                       title={review.decisionId ? "Record your choice and start the fix build" : "The review is not waiting for a choice"}
                       onClick={() => void fix()}>
            {ticked.size ? `Fix selected (${ticked.size})` : "Fix nothing"}
          </GatedButton>
        </div>
      ) : <p className="sec-ok" role="status" data-testid="sec-chosen">Chosen. The fix build starts next.</p>}
      {err ? <p className="sec-error" role="alert" data-testid="sec-check-error">{err}</p> : null}
    </div>
  );
}
