"use client";

import { type SelfImprovementFeedItem } from "../lib/self-improvement";
import { type EvalRun } from "../lib/evals";
import { type SkillInvocation } from "../lib/skill-invocations";
import {
  runSelfImprovementCanaryAction,
  startSelfImprovementRolloutAction,
  advanceSelfImprovementRolloutAction,
  runEvalsAction,
  evaluateAutonomyAction,
  pollProviderHealthAction,
  recordRouterRationaleAction,
  invokeSkillAction,
} from "./actions";

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
        <button className="button" type="submit">⚡ Run canary suite (auto)</button>
        <p className="detail">Runs the canary + eval-harness suites and records the outcome automatically.</p>
      </form>
    );
  }
  if (proposal.status === "rollout_approved") {
    return (
      <form action={startSelfImprovementRolloutAction} className="composer compact">
        <input type="hidden" name="proposal_id" value={proposal.id} />
        <input type="hidden" name="actor" value={actor} />
        <button className="button" type="submit">▶ Start staged rollout</button>
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
            <button className="button secondary" type="submit">✓ Advance (healthy)</button>
          </form>
          <form action={advanceSelfImprovementRolloutAction}>
            <input type="hidden" name="proposal_id" value={proposal.id} />
            <input type="hidden" name="actor" value={actor} />
            <input type="hidden" name="health" value="degraded" />
            <button className="button secondary" type="submit" style={{ color: "#f85149" }}>
              ⚠ Report degraded (auto-rollback)
            </button>
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

// --- Eval-harness card (mirrors the canary card) ---
export function EvalHarnessCard({ projectId, actor, run }: { projectId: string; actor: string; run: EvalRun }) {
  return (
    <article className="stack-card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10 }}>
        <h3 style={{ margin: 0 }}>Eval harness</h3>
        <form action={runEvalsAction}>
          <input type="hidden" name="project_id" value={projectId} />
          <input type="hidden" name="actor" value={actor} />
          <button className="button secondary" type="submit">Run evals</button>
        </form>
      </div>
      <p className="detail">
        Regression suites — routing fallback, policy audit round-trip, factory fail-closed gate,
        autonomy-ladder monotonicity. Gates self-improvement rollouts.
      </p>
      {!run ? (
        <p className="detail">No eval runs yet — click “Run evals”.</p>
      ) : (
        <>
          <p className="detail" style={{ marginBottom: 8 }}>
            Latest: <b style={{ color: tone(run.status) }}>{run.status.toUpperCase()}</b> — {run.passed} pass · {run.failed} fail
          </p>
          <div style={{ display: "grid", gap: 5 }}>
            {run.results.map((r) => (
              <div key={r.eval} style={{ fontSize: 12.5, color: "var(--ink-soft)" }}>
                <span style={{ color: tone(r.status) }}>●</span>{" "}
                <b style={{ color: "var(--ink)" }}>{r.eval}</b>{" "}
                <span style={{ color: "var(--muted)" }}>— {r.detail}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </article>
  );
}

// --- Evidence-based autonomy evaluation ---
export function AutonomyEvaluateCard({ projectId, actor }: { projectId: string; actor: string }) {
  return (
    <article className="stack-card">
      <h3 style={{ margin: 0 }}>Autonomy — evidence evaluation</h3>
      <p className="detail">
        Gather a role’s allowed/denied/scope-violation events since its last review and run a
        governance review — auto-proposes an upgrade decision or auto-downgrades on a violation.
      </p>
      <form action={evaluateAutonomyAction} className="composer compact">
        <input type="hidden" name="project_id" value={projectId} />
        <input type="hidden" name="actor" value={actor} />
        <label className="field">
          <span>Role key</span>
          <input name="role_key" className="input" placeholder="metatron" required />
        </label>
        <button className="button secondary" type="submit">Evaluate autonomy</button>
      </form>
    </article>
  );
}

// --- Router health + rationale ---
export function RouterHealthCard({
  projectId,
  actor,
  providerHealth,
}: {
  projectId: string;
  actor: string;
  providerHealth?: { total: number; healthy: number };
}) {
  return (
    <article className="stack-card">
      <h3 style={{ margin: 0 }}>Router health & rationale</h3>
      <p className="detail">
        Poll every model lane’s health and record the router’s decision (selected lane + ranked
        fallback chain + rejected sources) as a durable rationale artifact.
      </p>
      {providerHealth ? (
        <p className="detail">
          Provider lanes:{" "}
          <b style={{ color: providerHealth.total && providerHealth.healthy < providerHealth.total ? "#d29922" : "#3fb950" }}>
            {providerHealth.healthy}/{providerHealth.total} healthy
          </b>
        </p>
      ) : null}
      <div style={{ display: "flex", gap: 8 }}>
        <form action={pollProviderHealthAction}>
          <input type="hidden" name="project_id" value={projectId} />
          <input type="hidden" name="actor" value={actor} />
          <button className="button secondary" type="submit">Poll provider health</button>
        </form>
        <form action={recordRouterRationaleAction}>
          <input type="hidden" name="project_id" value={projectId} />
          <input type="hidden" name="actor" value={actor} />
          <button className="button secondary" type="submit">Record rationale</button>
        </form>
      </div>
    </article>
  );
}

// --- Skill invocation (mock mode) + recent invocations ---
export function SkillInvokeCard({
  projectId,
  invocations,
}: {
  projectId: string;
  invocations: SkillInvocation[];
}) {
  return (
    <article className="stack-card">
      <h3 style={{ margin: 0 }}>Skill invocation (mock)</h3>
      <p className="detail">
        Run a skill through the runtime — per-skill permission + cost + retry enforcement, with mock
        mode (full gauntlet, zero side effects). The API drives mock only.
      </p>
      <form action={invokeSkillAction} className="composer compact">
        <input type="hidden" name="project_id" value={projectId} />
        <label className="field">
          <span>Skill id</span>
          <input name="skill_id" className="input" placeholder="cloudflare.dns.record.upsert" required />
        </label>
        <label className="field">
          <span>Caller</span>
          <input name="caller" className="input" placeholder="resource_plane_kernel" required />
        </label>
        <button className="button secondary" type="submit">Invoke (mock)</button>
      </form>
      {invocations.length > 0 ? (
        <div className="feed-list" style={{ marginTop: 8 }}>
          {invocations.slice(0, 6).map((inv) => (
            <div className="feed-row" key={inv.id}>
              <div>
                <h4 style={{ margin: 0 }}>{inv.skillId}</h4>
                <p className="detail">
                  {inv.caller} · {inv.mode} ·{" "}
                  <span style={{ color: tone(inv.status) }}>{inv.status}</span>
                  {inv.reason ? ` · ${inv.reason}` : ""}
                </p>
              </div>
              <span className="pill" style={{ color: tone(inv.permissionDecision) }}>{inv.permissionDecision}</span>
            </div>
          ))}
        </div>
      ) : null}
    </article>
  );
}
