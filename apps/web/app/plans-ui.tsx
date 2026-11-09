"use client";

import { type PlanItem } from "../lib/plans";
import { dispatchPlanAction } from "./actions";
import { SubmitButton } from "./submit-button";
import { ActionForm } from "./client-forms";

export function PlansCard({ projectId, plans }: { projectId: string; plans: PlanItem[] }) {
  return (
    <article className="stack-card">
      <h3>Plans</h3>
      <p className="detail">
        Dispatch a plan to turn its phases into sequenced, executable tasks (Design → Build → Ship).
      </p>
      {plans.length === 0 ? (
        <p className="detail">No plans yet.</p>
      ) : (
        <div className="feed-list">
          {plans.map((p) => (
            <div className="feed-row" key={p.id}>
              <div>
                <h4>{p.title}</h4>
                <p className="detail">
                  {p.status}, {p.phaseCount} phase{p.phaseCount === 1 ? "" : "s"}
                  {p.objective ? `, ${p.objective}` : ""}
                </p>
              </div>
              {p.status === "dispatched" ? (
                <span className="detail" style={{ color: "var(--boss-c-3fb950)" }}>dispatched ✓</span>
              ) : (
                <ActionForm permission="work_item:manage" action={dispatchPlanAction}>
                  <input type="hidden" name="project_id" value={projectId} />
                  <input type="hidden" name="plan_id" value={p.id} />
                  <SubmitButton
                    type="submit"
                    style={{
                      background: "var(--accent)", color: "var(--accent-ink)", border: "none",
                      borderRadius: 8, padding: "6px 13px", fontSize: 12.5, fontWeight: 600, cursor: "pointer",
                    }}
                  >
                    Dispatch
                  </SubmitButton>
                </ActionForm>
              )}
            </div>
          ))}
        </div>
      )}
    </article>
  );
}
