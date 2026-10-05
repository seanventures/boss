"use client";

import { type ChaosRun } from "../lib/chaos";
import { runChaosAction } from "./actions";
import { SubmitButton } from "./submit-button";
import { ActionForm } from "./client-forms";

// --- Chaos / load harness ---
export function ChaosCard({ projectId, runs }: { projectId: string; runs: ChaosRun[] }) {
  return (
    <article className="stack-card">
      <h3 style={{ margin: 0 }}>Chaos &amp; resilience</h3>
      <p className="detail">
        Inject controlled faults against live primitives and record recovery: lease reclaim (crashed
        worker), and deadletter storm (anomaly detector raises an incident).
      </p>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {["lease_reclaim", "deadletter_storm"].map((scenario) => (
          <ActionForm action={runChaosAction} key={scenario}>
            <input type="hidden" name="project_id" value={projectId} />
            <input type="hidden" name="scenario" value={scenario} />
            <SubmitButton className="button secondary" type="submit">Inject {scenario}</SubmitButton>
          </ActionForm>
        ))}
      </div>
      {runs.length > 0 ? (
        <div className="feed-list" style={{ marginTop: 8 }}>
          {runs.slice(0, 6).map((r) => (
            <div className="feed-row" key={r.id}>
              <div>
                <h4 style={{ margin: 0 }}>{r.scenario}</h4>
                <p className="detail">{r.detail}</p>
              </div>
              <span className="pill" style={{ color: r.recovered ? "#3fb950" : "#f85149" }}>
                {r.recovered ? "recovered" : "failed"}
              </span>
            </div>
          ))}
        </div>
      ) : null}
    </article>
  );
}

// The FinOps card moved to Govern › Envelopes (app/govern/envelopes-tab.tsx).
