"use client";

import {
  type PipelineData,
  type PipelineProfile,
  effectiveTier,
  stepsForTier,
} from "../lib/pipeline-profiles";
import { setPipelineDefaultAction } from "./actions";

function usd(micros?: number): string {
  return micros == null ? "—" : `$${(micros / 1_000_000).toFixed(4)}`;
}

function ProfileCard({
  p,
  projectId,
  isDefault,
}: {
  p: PipelineProfile;
  projectId: string;
  isDefault: boolean;
}) {
  const tier = effectiveTier(p);
  const steps = stepsForTier(tier);
  const costPct = Math.round((steps.length / 3) * 100); // relative cost by step count
  const tone = p.name === "lean" ? "#2e7d32" : p.name === "strict" ? "#b23c17" : "#1565c0";
  return (
    <div
      style={{
        border: `1px solid ${isDefault ? tone : "#d6dae0"}`,
        borderRadius: 10,
        padding: 14,
        background: isDefault ? "#f6fbff" : "#fff",
        minWidth: 240,
        flex: "1 1 240px",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <strong style={{ fontSize: 15, textTransform: "capitalize" }}>{p.name}</strong>
        {isDefault && (
          <span style={{ fontSize: 11, color: "#fff", background: tone, padding: "1px 7px", borderRadius: 10 }}>
            default
          </span>
        )}
        {p.custom && (
          <span style={{ fontSize: 11, color: "#5a4", border: "1px solid #cda", padding: "0 6px", borderRadius: 10 }}>
            custom
          </span>
        )}
      </div>
      <div style={{ color: "#5b6470", fontSize: 12, margin: "4px 0 10px" }}>{p.label}</div>

      <div style={{ fontSize: 12.5, lineHeight: 1.7 }}>
        <div>Risk tiers: <b>{p.tier_floor}–{p.tier_cap}</b></div>
        <div>Standard task → tier {tier}: <b>{steps.length} {steps.length === 1 ? "step" : "steps"}</b></div>
        <div style={{ color: "#5b6470" }}>{steps.join(" → ")}</div>
        <div>
          Models:{" "}
          {Object.keys(p.model).length
            ? Object.entries(p.model)
                .map(([k, v]) => `${k}=${v}`)
                .join(", ")
            : "default"}
        </div>
      </div>

      <div style={{ marginTop: 10 }}>
        <div style={{ fontSize: 11, color: "#5b6470" }}>relative cost</div>
        <div style={{ height: 8, background: "#eef1f4", borderRadius: 6, overflow: "hidden" }}>
          <div style={{ width: `${costPct}%`, height: "100%", background: tone }} />
        </div>
      </div>

      {!isDefault && (
        <form action={setPipelineDefaultAction} style={{ marginTop: 12 }}>
          <input type="hidden" name="project_id" value={projectId} />
          <input type="hidden" name="name" value={p.name} />
          <button
            type="submit"
            style={{ fontSize: 12.5, padding: "5px 12px", borderRadius: 7, border: "1px solid #c2c8cf", background: "#fff", cursor: "pointer" }}
          >
            Set as default
          </button>
        </form>
      )}
    </div>
  );
}

export function PipelineView({ data }: { data: PipelineData }) {
  if (!data.apiReachable) {
    return (
      <div style={{ padding: 16, color: "#8a4b2b" }}>
        The control-plane API is unreachable, so pipeline profiles can&apos;t be loaded.
      </div>
    );
  }
  return (
    <div style={{ padding: "6px 2px 24px" }}>
      <h2 style={{ margin: "0 0 4px" }}>Coding Pipeline — cost vs. rigor</h2>
      <p style={{ color: "#5b6470", margin: "0 0 16px", maxWidth: 760 }}>
        Pick how much of the governed pipeline runs. <b>Lean</b> is the lowest cost for prototypes;{" "}
        <b>strict</b> runs the full rigor for production. Effort scales with risk automatically —
        sensitive changes (auth, payments, secrets) always run at least standard rigor. Current
        default: <b style={{ textTransform: "capitalize" }}>{data.defaultProfile}</b>.
      </p>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
        {data.profiles.map((p) => (
          <ProfileCard key={p.name} p={p} projectId={data.projectId} isDefault={p.name === data.defaultProfile} />
        ))}
      </div>

      <h3 style={{ margin: "24px 0 8px" }}>Recent run costs</h3>
      {data.recentRuns.length === 0 ? (
        <p style={{ color: "#5b6470", fontSize: 13 }}>
          No governed runs yet. Start a code task and the per-step costs will appear here for
          tier-by-tier comparison.
        </p>
      ) : (
        <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 12.5 }}>
          <thead>
            <tr style={{ textAlign: "left", color: "#5b6470", borderBottom: "1px solid #e2e6ea" }}>
              <th style={{ padding: "6px 8px" }}>Task</th>
              <th style={{ padding: "6px 8px" }}>Profile</th>
              <th style={{ padding: "6px 8px" }}>Tier</th>
              <th style={{ padding: "6px 8px" }}>Steps</th>
              <th style={{ padding: "6px 8px" }}>Total</th>
              <th style={{ padding: "6px 8px" }}>Per step</th>
              <th style={{ padding: "6px 8px" }}>Result</th>
            </tr>
          </thead>
          <tbody>
            {data.recentRuns.map((r) => (
              <tr key={r.id} style={{ borderBottom: "1px solid #f0f2f4" }}>
                <td style={{ padding: "6px 8px", maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.title}</td>
                <td style={{ padding: "6px 8px", textTransform: "capitalize" }}>{r.profile ?? "—"}</td>
                <td style={{ padding: "6px 8px" }}>{r.risk_tier ?? "—"}</td>
                <td style={{ padding: "6px 8px" }}>{r.steps_run?.length ?? "—"}</td>
                <td style={{ padding: "6px 8px", fontVariantNumeric: "tabular-nums" }}>{usd(r.cost_usd_micros)}</td>
                <td style={{ padding: "6px 8px", color: "#5b6470" }}>
                  {r.stage_costs
                    ? Object.entries(r.stage_costs)
                        .filter(([, v]) => v)
                        .map(([k, v]) => `${k.replace("_", " ")} ${usd(v)}`)
                        .join(" · ")
                    : "—"}
                </td>
                <td style={{ padding: "6px 8px", color: r.status === "completed" ? "#2e7d32" : "#b23c17" }}>{r.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <p style={{ color: "#5b6470", fontSize: 12.5, marginTop: 18 }}>
        Custom profiles (your own tier ranges and per-step model routing) are available on BOSS
        Enterprise. Community edition uses the lean / balanced / strict presets and defaults new
        projects to lean.
      </p>
    </div>
  );
}
