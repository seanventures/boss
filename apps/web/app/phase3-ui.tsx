"use client";

import { meetingCardTitle } from "../lib/meeting-title";
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
import { SubmitButton } from "./submit-button";

import { callAction } from "../lib/call-action";
function tone(s: string): string {
  const x = (s || "").toLowerCase();
  if (["actioned", "acknowledged", "approved", "pass", "on_track"].includes(x)) return "#3fb950";
  if (["deadlettered", "rejected", "fail", "no_quorum"].includes(x)) return "#f85149";
  if (["received", "parsed", "routed"].includes(x)) return "#d29922";
  return "#8b949e";
}

function Result({ state, testid }: { state: ActionResult | null; testid?: string }) {
  if (!state) return null;
  if (!state.ok) {
    return <p className="detail" style={{ color: "#f85149" }} data-testid={testid}
              data-ok="false">{state.error}</p>;
  }
  return (
    <pre className="mono" data-testid={testid} data-ok="true"
         style={{ whiteSpace: "pre-wrap", fontSize: 11.5, marginTop: 8, color: "var(--ink-soft)" }}>
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
  const [renderState, renderAction, renderPending] = useActionState(callAction(slackRenderAction), null);
  const [digestState, digestAction, digestPending] = useActionState(callAction(slackDigestAction), null);
  return (
    <article className="stack-card">
      <h3 style={{ margin: 0 }}>Slack — persona render & digest</h3>
      <form action={renderAction} className="composer compact">
        <div style={{ display: "flex", gap: 8 }}>
          <label className="field" style={{ flex: 1 }}>
            <span>Persona</span>
            <input name="persona" className="input" placeholder="Metatron" required
                   data-testid="slack-persona" />
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
          <textarea name="body" className="textarea" rows={2} placeholder="All systems green." required
                    data-testid="slack-body" />
        </label>
        <SubmitButton className="button secondary" type="submit" disabled={renderPending}
                      data-testid="slack-render">
          {renderPending ? "Rendering…" : "Render Slack message"}
        </SubmitButton>
      </form>
      <Result state={renderState} testid="slack-render-result" />
      <form action={digestAction} style={{ marginTop: 10 }}>
        <input type="hidden" name="project_id" value={projectId} />
        <SubmitButton className="button secondary" type="submit" disabled={digestPending}>
          {digestPending ? "Batching…" : "Batch draft Slack messages into digests"}
        </SubmitButton>
      </form>
      <Result state={digestState} />
    </article>
  );
}

// --- Meeting consensus: vote tally bound to a real meeting + stall sweep ---
export function MeetingConsensusCard({
  projectId,
  actor,
  meetingId,
  meetings,
}: {
  projectId: string;
  actor: string;
  meetingId?: string;
  meetings?: Array<{ id: string; title: string }>;
}) {
  const [tallyState, tallyAction, tallyPending] = useActionState(callAction(tallyVotesAction), null);
  const [sweepState, sweepAction, sweepPending] = useActionState(callAction(sweepStalledMeetingsAction), null);
  const bound = Boolean(meetingId);
  // AN OPERATOR FORM, NOT A FIXTURE. On boss-1 this card sat on the Meetings screen with votes
  // prefilled 2 / 1 / 0, "requires meeting_id" copy and an "mtg_… or mrun_…" placeholder — a
  // debug harness. The counts start empty (a vote nobody cast must not be one press away), the
  // copy says what the tally does, and the stall sweep is folded under Maintenance.
  const body = (
    <>
      {bound ? (
        <p className="detail">
          Record how this panel voted. BOSS applies the quorum and approval threshold and says
          whether the panel reached consensus.
        </p>
      ) : (
        <p className="detail" data-testid="meeting-consensus-requires-id">
          Pick the meeting this vote belongs to, then record how its panel voted.
        </p>
      )}
      <form action={tallyAction} className="composer compact">
        <input type="hidden" name="actor" value={actor} />
        {meetingId ? (
          <input type="hidden" name="meeting_id" value={meetingId} />
        ) : (
          <label className="field">
            <span>Meeting</span>
            {meetings && meetings.length > 0 ? (
              <select name="meeting_id" className="input" required defaultValue="">
                <option value="" disabled>Select a meeting…</option>
                {meetings.map((item) => (
                  <option key={item.id} value={item.id}>{meetingCardTitle(item.title).slice(0, 80)}</option>
                ))}
              </select>
            ) : (
              <span className="detail">No meetings yet. Convene one with New meeting first.</span>
            )}
          </label>
        )}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <label className="field" style={{ flex: 1 }}><span>Approve</span><input name="approve" className="input" type="number" min={0} required /></label>
          <label className="field" style={{ flex: 1 }}><span>Reject</span><input name="reject" className="input" type="number" min={0} required /></label>
          <label className="field" style={{ flex: 1 }}><span>Abstain</span><input name="abstain" className="input" type="number" min={0} required /></label>
          <label className="field" style={{ flex: 1 }}><span>Eligible</span><input name="eligible" className="input" type="number" min={0} placeholder="auto" /></label>
          <label className="field" style={{ flex: 1 }}><span>Quorum</span><input name="quorum" className="input" type="number" min={1} placeholder="auto" /></label>
        </div>
        <SubmitButton className="button secondary" type="submit"
                      disabled={tallyPending || (!bound && !(meetings && meetings.length > 0))}>
          {tallyPending ? "Tallying…" : "Record the vote"}
        </SubmitButton>
      </form>
      <Result state={tallyState} />
      <details style={{ marginTop: 10 }}>
        <summary className="detail">Maintenance</summary>
        <p className="detail">
          A panel that stopped advancing (its worker died mid-round) stays &ldquo;running&rdquo;
          until it is swept. Sweeping marks those runs failed so they can be convened again.
        </p>
        <form action={sweepAction}>
          <input type="hidden" name="actor" value={actor} />
          <SubmitButton className="button secondary" type="submit" disabled={sweepPending}>
            {sweepPending ? "Sweeping…" : "Sweep stalled meeting runs"}
          </SubmitButton>
        </form>
        <Result state={sweepState} />
      </details>
    </>
  );
  return (
    <article className="stack-card" data-testid="meeting-consensus-card">
      <h3 style={{ margin: 0 }}>Meeting consensus</h3>
      {bound ? body : (
        <details>
          <summary className="detail">Record a panel vote for a meeting</summary>
          {body}
        </details>
      )}
    </article>
  );
}

// --- Software-factory scale-up: WIP + confidence merge gate + chunk planner ---
export function FactoryScaleCard({ wip }: { wip: WipStatus }) {
  const [mergeState, mergeAction, mergePending] = useActionState(callAction(mergeCheckAction), null);
  const [chunkState, chunkAction, chunkPending] = useActionState(callAction(chunkPlanAction), null);
  const planeLabel =
    wip?.githubPlane === "mission_control_binding"
      ? `GitHub: Mission Control binding${wip.githubFullName ? ` (${wip.githubFullName})` : ""}`
      : wip?.githubPlane === "legacy_env"
        ? `GitHub: settings token${wip.githubFullName ? ` (${wip.githubFullName})` : ""}`
        : wip?.githubPlane
          ? `GitHub: ${wip.githubPlane}`
          : null;
  return (
    <article className="stack-card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10 }}>
        <h3 style={{ margin: 0 }}>Factory scale — WIP, gate, chunker</h3>
        {wip?.apiReachable ? (
          <span className="pill" style={{ color: wip.admit ? "#3fb950" : "#f85149" }}>
            {wip.inFlight}/{wip.wipLimit} in-flight · {wip.admit ? "slot free" : "WIP full"}
          </span>
        ) : (
          <span className="pill" style={{ color: "#f85149" }}>WIP unavailable</span>
        )}
      </div>
      {!wip?.apiReachable ? (
        <p className="detail" role="status" data-factory-wip="error">
          Factory WIP card could not load
          {wip?.blockedReason ? ` — ${wip.blockedReason}` : " — control plane request failed"}.
        </p>
      ) : (
        <p className="detail" data-factory-wip="ok">
          {planeLabel ?? "GitHub plane not advertised on this API."}
          {wip.fixtureSupported ? " · no-spend fixture supported" : ""}
          {wip.githubPlaneReason ? ` · ${wip.githubPlaneReason}` : ""}
        </p>
      )}

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
        <SubmitButton className="button secondary" type="submit" disabled={mergePending}>
          {mergePending ? "Checking…" : "Run merge check"}
        </SubmitButton>
      </form>
      <Result state={mergeState} />

      <p className="detail" style={{ marginTop: 10 }}>Spec → chunk planner: small, shippable PRs instead of one giant PR.</p>
      <form action={chunkAction} className="composer compact">
        <label className="field"><span>Title</span><input name="title" className="input" placeholder="Refactor auth module" data-testid="chunk-title" /></label>
        <label className="field">
          <span>Files (one per line)</span>
          <textarea name="files" className="textarea" rows={3} placeholder={"a.py\nb.py\nc.py\nd.py\ne.py"} />
        </label>
        <SubmitButton className="button secondary" type="submit" disabled={chunkPending}
                      data-testid="chunk-plan">
          {chunkPending ? "Planning…" : "Plan chunks"}
        </SubmitButton>
      </form>
      <Result state={chunkState} testid="chunk-plan-result" />
    </article>
  );
}
