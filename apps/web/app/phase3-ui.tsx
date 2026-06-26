"use client";

import { useActionState } from "react";
import { type AckState } from "../lib/comms-ack";
import { type WipStatus } from "../lib/factory";
import {
  type ActionResult,
  tallyVotesAction,
  sweepStalledMeetingsAction,
  mergeCheckAction,
  chunkPlanAction,
  slackRenderAction,
  slackDigestAction,
} from "./actions";

function tone(s: string): string {
  const x = (s || "").toLowerCase();
  if (["actioned", "acknowledged", "approved", "pass", "on_track"].includes(x)) return "#3fb950";
  if (["deadlettered", "rejected", "fail", "no_quorum"].includes(x)) return "#f85149";
  if (["received", "parsed", "routed"].includes(x)) return "#d29922";
  return "#8b949e";
}

function Result({ state }: { state: ActionResult | null }) {
  if (!state) return null;
  if (!state.ok) return <p className="detail" style={{ color: "#f85149" }}>{state.error}</p>;
  return (
    <pre className="mono" style={{ whiteSpace: "pre-wrap", fontSize: 11.5, marginTop: 8, color: "var(--ink-soft)" }}>
      {JSON.stringify(state.data, null, 2)}
    </pre>
  );
}

// --- Inbox acknowledgment ledger (no silent drops) ---
export function InboxAckCard({ items }: { items: AckState[] }) {
  const pending = items.filter((a) => !a.terminal).length;
  return (
    <article className="stack-card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10 }}>
        <h3 style={{ margin: 0 }}>Inbound ack ledger</h3>
        <span className="pill" style={{ color: pending ? "#d29922" : "#3fb950" }}>
          {pending} pending · {items.length} total
        </span>
      </div>
      <p className="detail">
        Every inbound message advances received → parsed → routed → actioned / deadlettered /
        acknowledged. The durable “no silent drops” record.
      </p>
      {items.length === 0 ? (
        <p className="detail">No inbound messages yet.</p>
      ) : (
        <div className="feed-list">
          {items.slice(0, 8).map((a) => (
            <div className="feed-row" key={a.id}>
              <div>
                <h4 style={{ margin: 0 }}>
                  <span style={{ color: tone(a.state), textTransform: "uppercase", fontSize: 12 }}>{a.state}</span>{" "}
                  <span style={{ color: "var(--muted)" }}>{a.channel}</span>
                </h4>
                <p className="detail">
                  {a.routeKind ?? "—"}
                  {a.targetRef ? ` → ${a.targetRef}` : ""}
                  {a.reason ? ` · ${a.reason}` : ""}
                </p>
              </div>
              <span className="pill" style={{ color: a.terminal ? "#3fb950" : "#d29922" }}>
                {a.terminal ? "terminal" : "open"}
              </span>
            </div>
          ))}
        </div>
      )}
    </article>
  );
}

// --- Slack shaping: persona render + digest batching ---
export function SlackToolsCard({ projectId }: { projectId: string }) {
  const [renderState, renderAction, renderPending] = useActionState(slackRenderAction, null);
  const [digestState, digestAction, digestPending] = useActionState(slackDigestAction, null);
  return (
    <article className="stack-card">
      <h3 style={{ margin: 0 }}>Slack — persona render & digest</h3>
      <form action={renderAction} className="composer compact">
        <div style={{ display: "flex", gap: 8 }}>
          <label className="field" style={{ flex: 1 }}>
            <span>Persona</span>
            <input name="persona" className="input" placeholder="Metatron" required />
          </label>
          <label className="field" style={{ flex: 1 }}>
            <span>Role</span>
            <input name="role" className="input" placeholder="VP Agents" />
          </label>
          <label className="field" style={{ flex: 1 }}>
            <span>Department</span>
            <input name="department" className="input" placeholder="Engineering" />
          </label>
        </div>
        <label className="field">
          <span>Body</span>
          <textarea name="body" className="textarea" rows={2} placeholder="All systems green." required />
        </label>
        <button className="button secondary" type="submit" disabled={renderPending}>
          {renderPending ? "Rendering…" : "Render Slack message"}
        </button>
      </form>
      <Result state={renderState} />
      <form action={digestAction} style={{ marginTop: 10 }}>
        <input type="hidden" name="project_id" value={projectId} />
        <button className="button secondary" type="submit" disabled={digestPending}>
          {digestPending ? "Batching…" : "Batch draft Slack messages into digests"}
        </button>
      </form>
      <Result state={digestState} />
    </article>
  );
}

// --- Meeting consensus: vote tally + stall sweep ---
export function MeetingConsensusCard({ projectId, actor }: { projectId: string; actor: string }) {
  const [tallyState, tallyAction, tallyPending] = useActionState(tallyVotesAction, null);
  const [sweepState, sweepAction, sweepPending] = useActionState(sweepStalledMeetingsAction, null);
  return (
    <article className="stack-card">
      <h3 style={{ margin: 0 }}>Meeting consensus</h3>
      <p className="detail">Tally a panel vote under quorum/threshold rules, and sweep stalled meeting runs.</p>
      <form action={tallyAction} className="composer compact">
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <label className="field" style={{ flex: 1 }}><span>Approve</span><input name="approve" className="input" type="number" min={0} defaultValue={2} /></label>
          <label className="field" style={{ flex: 1 }}><span>Reject</span><input name="reject" className="input" type="number" min={0} defaultValue={1} /></label>
          <label className="field" style={{ flex: 1 }}><span>Abstain</span><input name="abstain" className="input" type="number" min={0} defaultValue={0} /></label>
          <label className="field" style={{ flex: 1 }}><span>Eligible</span><input name="eligible" className="input" type="number" min={0} placeholder="auto" /></label>
          <label className="field" style={{ flex: 1 }}><span>Quorum</span><input name="quorum" className="input" type="number" min={1} placeholder="auto" /></label>
        </div>
        <button className="button secondary" type="submit" disabled={tallyPending}>
          {tallyPending ? "Tallying…" : "Tally vote"}
        </button>
      </form>
      <Result state={tallyState} />
      <form action={sweepAction} style={{ marginTop: 10 }}>
        <input type="hidden" name="actor" value={actor} />
        <button className="button secondary" type="submit" disabled={sweepPending}>
          {sweepPending ? "Sweeping…" : "Sweep stalled meeting runs"}
        </button>
      </form>
      <Result state={sweepState} />
    </article>
  );
}

// --- Software-factory scale-up: WIP + confidence merge gate + chunk planner ---
export function FactoryScaleCard({ wip }: { wip: WipStatus }) {
  const [mergeState, mergeAction, mergePending] = useActionState(mergeCheckAction, null);
  const [chunkState, chunkAction, chunkPending] = useActionState(chunkPlanAction, null);
  return (
    <article className="stack-card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10 }}>
        <h3 style={{ margin: 0 }}>Factory scale — WIP, gate, chunker</h3>
        {wip ? (
          <span className="pill" style={{ color: wip.admit ? "#3fb950" : "#f85149" }}>
            {wip.inFlight}/{wip.wipLimit} in-flight · {wip.admit ? "slot free" : "WIP full"}
          </span>
        ) : null}
      </div>

      <p className="detail">Confidence-gated merge check — required gates must pass AND confidence ≥ floor.</p>
      <form action={mergeAction} className="composer compact">
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap", fontSize: 13 }}>
          <label><input type="checkbox" name="tests_passed" defaultChecked /> tests</label>
          <label><input type="checkbox" name="lint_passed" defaultChecked /> lint</label>
          <label><input type="checkbox" name="ci_green" defaultChecked /> ci-green</label>
          <label><input type="checkbox" name="reviewer_independent" defaultChecked /> reviewer</label>
          <label><input type="checkbox" name="adversarial" /> adversarial</label>
        </div>
        <label className="field"><span>Min confidence</span><input name="min_confidence" className="input" type="number" step="0.05" min={0} max={1} defaultValue={0.8} /></label>
        <button className="button secondary" type="submit" disabled={mergePending}>
          {mergePending ? "Checking…" : "Run merge check"}
        </button>
      </form>
      <Result state={mergeState} />

      <p className="detail" style={{ marginTop: 10 }}>Spec → chunk planner: small, shippable PRs instead of one giant PR.</p>
      <form action={chunkAction} className="composer compact">
        <label className="field"><span>Title</span><input name="title" className="input" placeholder="Refactor auth module" /></label>
        <label className="field">
          <span>Files (one per line)</span>
          <textarea name="files" className="textarea" rows={3} placeholder={"a.py\nb.py\nc.py\nd.py\ne.py"} />
        </label>
        <button className="button secondary" type="submit" disabled={chunkPending}>
          {chunkPending ? "Planning…" : "Plan chunks"}
        </button>
      </form>
      <Result state={chunkState} />
    </article>
  );
}
