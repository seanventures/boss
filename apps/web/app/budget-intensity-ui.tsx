"use client";

import { useState } from "react";
import { setBudgetIntensityAction } from "./actions";
import { SubmitButton } from "./submit-button";
import { ActionForm } from "./client-forms";

// Mirrors the backend mapping in budget.set_intensity so the preview matches what will apply.
function envelope(v: number) {
  const f = v / 100;
  return {
    daily: Math.round(5 + f * 495),
    concurrent: 1 + Math.round(f * 19),
    workers: 1 + Math.round(f * 29),
    tiers: v < 34 ? "low-cost only" : v < 67 ? "low-cost + standard" : "all tiers (incl. strong)",
  };
}

export function BudgetIntensitySlider({
  projectId,
  current,
}: {
  projectId: string;
  current: number | null;
}) {
  const [val, setVal] = useState<number>(current ?? 50);
  const e = envelope(val);
  return (
    <article className="stack-card">
      <h2>Operational Intensity</h2>
      <p className="detail">
        One CEO dial (0–100) sets the spend envelope and scaling limits — daily/monthly/burst caps,
        max concurrent jobs and workers, and which model tiers are allowed.
      </p>
      <ActionForm action={setBudgetIntensityAction} style={{ display: "grid", gap: 12, marginTop: 10 }}>
        <input type="hidden" name="project_id" value={projectId} />
        <input
          type="range"
          name="intensity"
          min={0}
          max={100}
          value={val}
          onChange={(ev) => setVal(Number(ev.target.value))}
          style={{ width: "100%" }}
        />
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 12.5, color: "var(--muted)" }}>
          <span>Conservative</span>
          <b style={{ color: "var(--ink)", fontSize: 16 }}>{val}</b>
          <span>Aggressive</span>
        </div>
        <p className="detail" style={{ margin: 0 }}>
          ≈ <b style={{ color: "var(--ink)" }}>${e.daily}/day</b> · {e.concurrent} concurrent jobs ·{" "}
          {e.workers} workers · {e.tiers}
        </p>
        <div>
          <SubmitButton
            type="submit"
            data-testid="budget-intensity-apply"
            style={{
              background: "var(--accent)", color: "var(--accent-ink)", border: "none",
              borderRadius: 8, padding: "7px 16px", fontSize: 13, fontWeight: 600, cursor: "pointer",
            }}
          >
            Apply intensity
          </SubmitButton>
        </div>
        {current != null && (
          <p className="detail" style={{ margin: 0, color: "var(--muted)" }}
             data-testid="budget-intensity-current">
            Current: {current}
          </p>
        )}
      </ActionForm>
    </article>
  );
}
