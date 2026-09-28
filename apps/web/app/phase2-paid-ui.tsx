"use client";

import { type SelfImprovementFeedItem } from "../lib/self-improvement";
import {
  runSelfImprovementCanaryAction,
  startSelfImprovementRolloutAction,
  advanceSelfImprovementRolloutAction,
} from "./actions-paid";
import { SubmitButton } from "./submit-button";

function tone(s: string): string {
  const x = (s || "").toLowerCase();
  if (["pass", "succeeded", "completed", "rolled_out", "allow", "healthy"].includes(x)) return "#3fb950";
  if (["fail", "failed", "rolled_back", "deny", "degraded"].includes(x)) return "#f85149";
  if (["rolling_out", "in_progress", "defer"].includes(x)) return "#d29922";
  return "#8b949e";
}

// --- Self-improvement runtime: auto-canary + staged rollout with auto-rollback ---
export function SelfImprovementRuntime({
  proposal,
  projectId,
  actor,
}: {
  proposal: SelfImprovementFeedItem;
  projectId: string;
  actor: string;
}) {
  const st = proposal.rolloutState as
    | { stages?: string[]; current_index?: number; status?: string }
    | undefined;

  if (proposal.status === "canary_approved") {
    return (
      <form action={runSelfImprovementCanaryAction} className="composer compact">
        <input type="hidden" name="proposal_id" value={proposal.id} />
        <input type="hidden" name="actor" value={actor} />
        <SubmitButton className="button" type="submit">⚡ Run canary suite (auto)</SubmitButton>
        <p className="detail">Runs the canary + eval-harness suites and records the outcome automatically.</p>
      </form>
    );
  }
  if (proposal.status === "rollout_approved") {
    return (
      <form action={startSelfImprovementRolloutAction} className="composer compact">
        <input type="hidden" name="proposal_id" value={proposal.id} />
        <input type="hidden" name="actor" value={actor} />
        <SubmitButton className="button" type="submit">▶ Start staged rollout</SubmitButton>
        <p className="detail">Ship narrow → widen, each stage gated by a health observation.</p>
      </form>
    );
  }
  if (proposal.status === "rolling_out" && st) {
    const stages = st.stages ?? [];
    const idx = Number(st.current_index ?? 0);
    return (
      <div className="composer compact">
        <p className="detail" style={{ marginBottom: 4 }}>
          Staged rollout:{" "}
          {stages.map((s, i) => (
            <span key={s} style={{ color: i < idx ? "#3fb950" : i === idx ? "#d29922" : "var(--muted)" }}>
              {i === idx ? "▶ " : i < idx ? "✓ " : "○ "}{s}{i < stages.length - 1 ? " · " : ""}
            </span>
          ))}
        </p>
        <div style={{ display: "flex", gap: 8 }}>
          <form action={advanceSelfImprovementRolloutAction}>
            <input type="hidden" name="proposal_id" value={proposal.id} />
            <input type="hidden" name="actor" value={actor} />
            <input type="hidden" name="health" value="healthy" />
            <SubmitButton className="button secondary" type="submit">✓ Advance (healthy)</SubmitButton>
          </form>
          <form action={advanceSelfImprovementRolloutAction}>
            <input type="hidden" name="proposal_id" value={proposal.id} />
            <input type="hidden" name="actor" value={actor} />
            <input type="hidden" name="health" value="degraded" />
            <SubmitButton className="button secondary" type="submit" style={{ color: "#f85149" }}>
              ⚠ Report degraded (auto-rollback)
            </SubmitButton>
          </form>
        </div>
      </div>
    );
  }
  if (st?.status) {
    return <p className="detail">Rollout: <b style={{ color: tone(String(st.status)) }}>{String(st.status)}</b></p>;
  }
  return null;
}
