"use client";

import React, { useCallback, useEffect, useId, useRef, useState } from "react";
import { GatedButton } from "../../components/gated";
import { postJson } from "./cockpit-actions";
import { SavedChecks } from "./saved-checks";
import { actorWords } from "../../lib/actor-words";

type Review = {
  id: string; actor: string; outcome: "accepted" | "changes_requested"; note: string;
  snapshot_hash: string; created_at: string;
  snapshot: { delivered_head: string | null };
};
export type ResultReviewData = {
  snapshot_hash: string; reviewable: boolean; unavailable_reason: string | null;
  snapshot: {
    project_id?: string;
    delivered_head: string | null; pull_request_url: string | null; receipt_id: string | null;
    receipt_status?: string | null; effect_kind?: string | null;
    criteria: string[];
    checks: { state: string; checked_head: string | null; artifact_id: string | null;
      quality_status: string | null; gates_failed: string[]; commands: string[] };
    cost: { observed_usd_micros: number | null; estimated_usd_micros: number;
      included_equivalent_usd_micros: number; held_usd_micros: number; confidence: string; unresolved_records: number; included_calls: number };
  };
  history: Review[];
};

const amount = (micros: number | null) => micros === null ? "Unknown" : `$${(micros / 1e6).toFixed(2)}`;
function safeLink(value: string | null): string | undefined {
  try { const url = new URL(value ?? ""); return ["http:", "https:"].includes(url.protocol) ? url.href : undefined; }
  catch { return undefined; }
}

export function ResultReview({ runId, onChangeRequested }: {
  runId: string; onChangeRequested: (note: string) => void;
}) {
  const [data, setData] = useState<ResultReviewData | null>(null);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState("");
  const noteId = useId();
  const headingId = useId();
  // An uncertain response is retried with exactly the same durable submission identity.
  const intent = useRef<{ body: string; id: string } | null>(null);
  const inFlight = useRef(false);
  const path = `/v1/code-tasks/${encodeURIComponent(runId)}/result-review`;
  const load = useCallback(async () => {
    try {
      const response = await fetch(path, { cache: "no-store" });
      if (!response.ok) throw new Error(`Result review could not load (${response.status}).`);
      const body = await response.json() as ResultReviewData;
      if (!body.snapshot || !Array.isArray(body.history)) throw new Error("Result review is unavailable.");
      setData(body); setError("");
    } catch (e) { setError(e instanceof Error ? e.message : "Result review could not load."); }
  }, [path]);
  useEffect(() => { void load(); }, [load]);

  async function submit(outcome: Review["outcome"]) {
    if (!data?.reviewable || inFlight.current || (outcome === "changes_requested" && !note.trim())) return;
    inFlight.current = true; setBusy(true); setError(""); setSaved("");
    const body = { snapshot_hash: data.snapshot_hash, outcome, note: note.trim() };
    const encoded = JSON.stringify(body);
    if (intent.current?.body !== encoded) intent.current = { body: encoded, id: crypto.randomUUID() };
    try {
      const result = await postJson(path, { ...body, submission_id: intent.current.id });
      if (!result.ok) { setError(result.error ?? "Review was not saved."); return; }
      setSaved(outcome === "accepted" ? "Accepted as useful for this recorded revision." : "Changes requested. Review the proposal before starting work.");
      if (outcome === "changes_requested") onChangeRequested(
        `Review of ${runId} at ${data.snapshot.delivered_head}:\n${note.trim()}`);
      await load();
    } finally { inFlight.current = false; setBusy(false); }
  }

  const snapshot = data?.snapshot;
  const pr = safeLink(snapshot?.pull_request_url ?? null);
  // /pull/N/commits/SHA is pinned to the saved PR revision; /files alone can move.
  const revision = pr && snapshot?.delivered_head ? `${pr.replace(/\/$/, "")}/commits/${snapshot.delivered_head}` : undefined;
  return <section className="fx-result-review fx-out-body" aria-labelledby={headingId}>
    <h2 id={headingId} className="fx-sec-title">Review the result</h2>
    {error ? <div role="alert">{error} <button type="button" className="ag-link" onClick={() => void load()}>Reload result</button></div> : null}
    {!data && !error ? <div role="status">Loading result…</div> : null}
    {snapshot ? <>
      <div>Delivered revision: {revision ? <a href={revision} target="_blank" rel="noreferrer">{snapshot.delivered_head?.slice(0, 12)} ↗</a> : "Revision unavailable"}.
        {pr ? <> <a href={pr} target="_blank" rel="noreferrer">Open pull request ↗</a></> : null}</div>
      <span className="fx-out-sub" title="Acceptance records usefulness of the saved revision. It does not pass checks, merge code, deploy, or authorize more work.">Usefulness only, approvals unchanged</span>
      {snapshot.criteria.length ? <ul>{snapshot.criteria.map((criterion, index) => <li key={index} data-user-text>{criterion}</li>)}</ul> : <div>No acceptance criteria recorded.</div>}
      <div className="fx-out-links"><span>Checks: {snapshot.checks.state.replaceAll("_", " ")}</span><span>Quality: {snapshot.checks.quality_status ?? "Not recorded"}</span></div>
      <details><summary>Evidence and receipts</summary>
        <div>Checked revision: <code>{snapshot.checks.checked_head ?? "Unknown"}</code></div>
        {snapshot.checks.gates_failed.length ? <div>Failed gates: {snapshot.checks.gates_failed.join(", ")}.</div> : null}
        {snapshot.checks.commands.length ? <ul>{snapshot.checks.commands.map((command, i) => <li key={i} data-user-text>{command}</li>)}</ul> : <div>No test commands recorded.</div>}
        <div>Test evidence: {snapshot.checks.artifact_id ? <a href={`/v1/artifacts/${encodeURIComponent(snapshot.checks.artifact_id)}`} target="_blank" rel="noreferrer">Open recorded report ↗</a> : "Not recorded"}.
          {" "}<span>Delivery receipt: {snapshot.receipt_status ?? "Not recorded"}</span></div>
        <div><code>{snapshot.receipt_id ?? "No receipt"}</code> <span>{snapshot.effect_kind ?? ""}</span></div>
        <span title="These are saved BOSS records, without a new remote branch check.">Saved evidence, no remote recheck</span>
      </details>
      <div className="fx-out-links" title="Observed cost receipts, their estimates, and outstanding reservations are separate. Unknown is not zero; saved costs may change after reconciliation.">
        <span>Observed cost: {amount(snapshot.cost.observed_usd_micros)}</span>
        <span>Estimate: {amount(snapshot.cost.estimated_usd_micros)}</span>
        <span>Outstanding holds: {amount(snapshot.cost.held_usd_micros)}</span>
        <span>Confidence: {snapshot.cost.confidence}</span>
      </div>
      <div className="fx-out-links"><span>{snapshot.cost.unresolved_records} unresolved records</span><span>{snapshot.cost.included_calls} included calls</span>
        <span title="API-equivalent value already included in subscription or local capacity; not cash paid.">Included value: {amount(snapshot.cost.included_equivalent_usd_micros)}</span></div>
      {!data?.reviewable ? <div>{data?.unavailable_reason}</div> : <>
        <label htmlFor={noteId}>Your review or bounded change</label>
        <textarea className="textarea" rows={3} id={noteId} value={note} maxLength={4000} onChange={(e) => setNote(e.target.value)} disabled={busy} />
        <div>
          <GatedButton permission="code_task:steer" type="button" className="ag-btn" disabled={busy} onClick={() => void submit("accepted")}>Accept as useful</GatedButton>
          <GatedButton permission="code_task:steer" type="button" className="ag-btn" disabled={busy || !note.trim()} onClick={() => void submit("changes_requested")}>Request a change</GatedButton>
        </div>
      </>}
      {saved ? <div role="status" title={saved}>{saved}</div> : null}
      {data?.history.length ? <details><summary>Review history ({data.history.length})</summary><ol>
        {data.history.map((review) => <li key={review.id}>
          <div className="fx-out-links"><span>{review.outcome === "accepted" ? "Accepted as useful" : "Changes requested"}</span>
            <code>{review.snapshot.delivered_head?.slice(0, 12) ?? "Unknown revision"}</code>
            <span>{actorWords(review.actor)}</span><time>{review.created_at}</time>
            {review.snapshot_hash !== data.snapshot_hash ? <span>Earlier evidence snapshot</span> : null}</div>
          {review.note ? <div data-user-text>{review.note}</div> : null}
          {review.outcome === "changes_requested" ? <button type="button" className="ag-link" onClick={() => onChangeRequested(`Review of ${runId} at ${review.snapshot.delivered_head}:\n${review.note}`)}>Prepare this change</button> : null}
        </li>)}
      </ol></details> : null}
      {snapshot.project_id ? <SavedChecks key={`${snapshot.project_id}:${runId}`} projectId={snapshot.project_id} runId={runId}
        snapshotHash={data!.snapshot_hash} criteria={snapshot.criteria}
        acceptedReviewId={data!.reviewable && data!.history[0]?.outcome === "accepted" && data!.history[0].snapshot_hash === data!.snapshot_hash ? data!.history[0].id : null} /> : null}
    </> : null}
  </section>;
}
