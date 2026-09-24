"use client";

import { useActionState } from "react";
import { collectSoc2EvidenceAction } from "./actions-paid";
import { SubmitButton } from "./submit-button";

// --- SOC2 evidence bundle ---
export function Soc2Card({ projectId }: { projectId: string }) {
  const [state, action, pending] = useActionState(collectSoc2EvidenceAction, null);
  const summary = state?.ok ? (state.data?.summary as Record<string, unknown> | undefined) : undefined;
  const controls = state?.ok ? (state.data?.controls as Array<Record<string, unknown>> | undefined) : undefined;
  return (
    <article className="stack-card">
      <h3 style={{ margin: 0 }}>SOC2 evidence bundle</h3>
      <p className="detail">
        Generate a Trust Services Common-Criteria evidence bundle from live audit, policy-eval,
        break-glass, change-management, and canary/eval data.
      </p>
      <form action={action}>
        <input type="hidden" name="project_id" value={projectId} />
        <SubmitButton className="button secondary" type="submit" disabled={pending}>
          {pending ? "Collecting…" : "Collect SOC2 evidence"}
        </SubmitButton>
      </form>
      {state && !state.ok ? <p className="detail" style={{ color: "#f85149" }}>{state.error}</p> : null}
      {summary ? (
        <p className="detail" style={{ marginTop: 6 }}>
          <b style={{ color: "#3fb950" }}>{String(summary.controls_satisfied)}</b>/{String(summary.controls_total)} controls satisfied
          {" "}· artifact {String(state?.data?.artifact_id ?? "")}
        </p>
      ) : null}
      {controls ? (
        <div style={{ display: "grid", gap: 4, marginTop: 6 }}>
          {controls.map((c) => (
            <div key={String(c.id)} style={{ fontSize: 12.5, color: "var(--ink-soft)" }}>
              <span style={{ color: c.status === "satisfied" ? "#3fb950" : "#d29922" }}>●</span>{" "}
              <b style={{ color: "var(--ink)" }}>{String(c.id)}</b>{" "}
              <span style={{ color: "var(--muted)" }}>{String(c.name)}</span>
            </div>
          ))}
        </div>
      ) : null}
    </article>
  );
}
