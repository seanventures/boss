"use client";

import { type EvalRun } from "../lib/evals";
import { type SkillInvocation } from "../lib/skill-invocations";
import {
  runEvalsAction,
  evaluateAutonomyAction,
  pollProviderHealthAction,
  recordRouterRationaleAction,
  invokeSkillAction,
} from "./actions";
import { SubmitButton } from "./submit-button";
import { ActionForm } from "./client-forms";

function tone(s: string): string {
  const x = (s || "").toLowerCase();
  if (["pass", "succeeded", "completed", "rolled_out", "allow", "healthy"].includes(x)) return "#3fb950";
  if (["fail", "failed", "rolled_back", "deny", "degraded"].includes(x)) return "#f85149";
  if (["rolling_out", "in_progress", "defer"].includes(x)) return "#d29922";
  return "#8b949e";
}

// --- Eval-harness card (mirrors the canary card) ---
export function EvalHarnessCard({ projectId, actor, run }: { projectId: string; actor: string; run: EvalRun }) {
  return (
    <article className="stack-card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10 }}>
        <h3 style={{ margin: 0 }}>Eval harness</h3>
        <ActionForm permission="assurance:run" action={runEvalsAction}>
          <input type="hidden" name="project_id" value={projectId} />
          <input type="hidden" name="actor" value={actor} />
          <SubmitButton className="button secondary" type="submit" data-testid="eval-run">Run evals</SubmitButton>
        </ActionForm>
      </div>
      <p className="detail">
        Regression suites — routing fallback, policy audit round-trip, factory fail-closed gate,
        autonomy-ladder monotonicity. Gates self-improvement rollouts.
      </p>
      {!run ? (
        <p className="detail">No eval runs yet — click “Run evals”.</p>
      ) : (
        <>
          <p className="detail" style={{ marginBottom: 8 }} data-testid="eval-latest">
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
      <ActionForm permission="autonomy:read" action={evaluateAutonomyAction} className="composer compact">
        <input type="hidden" name="project_id" value={projectId} />
        <input type="hidden" name="actor" value={actor} />
        <label className="field">
          <span>Role key</span>
          <input name="role_key" className="input" placeholder="metatron" required />
        </label>
        <SubmitButton className="button secondary" type="submit">Evaluate autonomy</SubmitButton>
      </ActionForm>
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
        <ActionForm permission="model_provider:read" action={pollProviderHealthAction}>
          <input type="hidden" name="project_id" value={projectId} />
          <input type="hidden" name="actor" value={actor} />
          <SubmitButton className="button secondary" type="submit">Poll provider health</SubmitButton>
        </ActionForm>
        <ActionForm permission="model_provider:manage" action={recordRouterRationaleAction}>
          <input type="hidden" name="project_id" value={projectId} />
          <input type="hidden" name="actor" value={actor} />
          <SubmitButton className="button secondary" type="submit">Record rationale</SubmitButton>
        </ActionForm>
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
      <ActionForm permission="skill:invoke" action={invokeSkillAction} className="composer compact">
        <input type="hidden" name="project_id" value={projectId} />
        <label className="field">
          <span>Skill id</span>
          <input name="skill_id" className="input" placeholder="cloudflare.dns.record.upsert" required />
        </label>
        <label className="field">
          <span>Caller</span>
          <input name="caller" className="input" placeholder="resource_plane_kernel" required />
        </label>
        <SubmitButton className="button secondary" type="submit">Invoke (mock)</SubmitButton>
      </ActionForm>
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
