"use client";

import { type CanaryRun } from "../lib/canaries";
import { runCanariesAction } from "./actions";
import { SubmitButton } from "./submit-button";
import { ActionForm } from "./client-forms";

function tone(s: string): string {
  return s === "pass" ? "#3fb950" : s === "fail" ? "#f85149" : "#8b949e";
}

export function CanaryCard({ projectId, run }: { projectId: string; run: CanaryRun }) {
  return (
    <article className="stack-card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10 }}>
        <h3 style={{ margin: 0 }}>Canary suite</h3>
        <ActionForm permission="assurance:run" action={runCanariesAction}>
          <input type="hidden" name="project_id" value={projectId} />
          <SubmitButton
            type="submit"
            style={{
              background: "var(--accent)", color: "var(--accent-ink)", border: "none",
              borderRadius: 8, padding: "6px 13px", fontSize: 12.5, fontWeight: 600, cursor: "pointer",
            }}
          >
            Run canaries
          </SubmitButton>
        </ActionForm>
      </div>
      <p className="detail">
        Smokes that prove the non-negotiables — exactly-once effects, no-drop comms, durable
        execution, and budget gating.
      </p>
      {!run ? (
        <p className="detail">No canary runs yet — click “Run canaries”.</p>
      ) : (
        <>
          <p className="detail" style={{ marginBottom: 8 }}>
            Latest:{" "}
            <b style={{ color: tone(run.status) }}>{run.status.toUpperCase()}</b> — {run.passed} pass
            · {run.failed} fail · {run.skipped} skip
          </p>
          <div style={{ display: "grid", gap: 5 }}>
            {run.results.map((r) => (
              <div key={r.canary} style={{ fontSize: 12.5, color: "var(--ink-soft)" }}>
                <span style={{ color: tone(r.status) }}>●</span>{" "}
                <b style={{ color: "var(--ink)" }}>{r.canary}</b>{" "}
                <span style={{ color: "var(--muted)" }}>— {r.detail}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </article>
  );
}
