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

function toneFor(name: string): string {
  if (name === "lean") return "#3fb950";
  if (name === "strict") return "#f85149";
  if (name === "balanced") return "#818cf8";
  return "#d29922"; // custom
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
  const costPct = Math.round((steps.length / 3) * 100);
  const tone = toneFor(p.name);
  return (
    <div
      style={{
        border: `1px solid ${isDefault ? tone : "var(--border)"}`,
        borderRadius: 12,
        padding: 16,
        background: "var(--surface-2)",
        color: "var(--ink)",
        flex: "1 1 250px",
        minWidth: 250,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, minHeight: 28 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <strong style={{ fontSize: 15, textTransform: "capitalize", color: "var(--ink)" }}>{p.name}</strong>
          {p.custom && (
            <span style={{ fontSize: 10.5, color: tone, border: `1px solid ${tone}`, padding: "0 7px", borderRadius: 999 }}>
              custom
            </span>
          )}
        </div>
        {isDefault ? (
          <span style={{ fontSize: 10.5, fontWeight: 700, color: "#0d1117", background: tone, padding: "3px 11px", borderRadius: 999, letterSpacing: 0.3 }}>
            DEFAULT
          </span>
        ) : (
          <form action={setPipelineDefaultAction}>
            <input type="hidden" name="project_id" value={projectId} />
            <input type="hidden" name="name" value={p.name} />
            <button
              type="submit"
              style={{
                fontSize: 11.5, fontWeight: 600, padding: "4px 12px", borderRadius: 999,
                border: "1px solid var(--accent-line)", background: "var(--accent-soft)",
                color: "var(--accent-text)", cursor: "pointer", whiteSpace: "nowrap",
              }}
            >
              Set default
            </button>
          </form>
        )}
      </div>
      <div style={{ color: "var(--muted)", fontSize: 12, margin: "5px 0 12px" }}>{p.label}</div>

      <div style={{ fontSize: 12.5, lineHeight: 1.8, color: "var(--ink-soft)" }}>
        <div>Risk tiers: <b style={{ color: "var(--ink)" }}>{p.tier_floor}–{p.tier_cap}</b></div>
        <div>Standard task → tier {tier}: <b style={{ color: "var(--ink)" }}>{steps.length} {steps.length === 1 ? "step" : "steps"}</b></div>
        <div style={{ color: "var(--muted)" }}>{steps.join(" → ")}</div>
        <div style={{ color: "var(--muted)" }}>
          Models:{" "}
          {Object.keys(p.model).length
            ? Object.entries(p.model).map(([k, v]) => `${k}=${v}`).join(", ")
            : "default"}
        </div>
      </div>

      <div style={{ marginTop: 12 }}>
        <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 4 }}>relative cost</div>
        <div style={{ height: 8, background: "var(--surface-3)", borderRadius: 6, overflow: "hidden" }}>
          <div style={{ width: `${costPct}%`, height: "100%", background: tone }} />
        </div>
      </div>

    </div>
  );
}

export function PipelineView({ data }: { data: PipelineData }) {
  if (!data.apiReachable) {
    return (
      <div style={{ padding: 16, color: "#f0a35e" }}>
        The control-plane API is unreachable, so pipeline profiles can&apos;t be loaded.
      </div>
    );
  }
  return (
    <div style={{ color: "var(--ink)" }}>
      <p style={{ color: "var(--muted)", margin: "0 0 16px", maxWidth: 760, fontSize: 13, lineHeight: 1.6 }}>
        Pick how much of the governed pipeline runs. <b style={{ color: "var(--ink)" }}>Lean</b> is the
        lowest cost for prototypes; <b style={{ color: "var(--ink)" }}>strict</b> runs the full rigor for
        production. Effort scales with risk automatically — sensitive changes (auth, payments, secrets)
        always run at least standard rigor. Current default:{" "}
        <b style={{ color: "var(--accent-text)", textTransform: "capitalize" }}>{data.defaultProfile}</b>.
      </p>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
        {data.profiles.map((p) => (
          <ProfileCard key={p.name} p={p} projectId={data.projectId} isDefault={p.name === data.defaultProfile} />
        ))}
      </div>

      <h3 style={{ margin: "26px 0 10px", color: "var(--ink)", fontSize: 15 }}>Recent run costs</h3>
      {data.recentRuns.length === 0 ? (
        <p style={{ color: "var(--muted)", fontSize: 13 }}>
          No governed runs yet. Start a code task and the per-step costs will appear here for
          tier-by-tier comparison.
        </p>
      ) : (
        <div style={{ border: "1px solid var(--border)", borderRadius: 12, overflow: "hidden" }}>
          <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 12.5, color: "var(--ink-soft)" }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--muted)", background: "var(--surface-2)" }}>
                <th style={{ padding: "8px 10px" }}>Task</th>
                <th style={{ padding: "8px 10px" }}>Profile</th>
                <th style={{ padding: "8px 10px" }}>Tier</th>
                <th style={{ padding: "8px 10px" }}>Steps</th>
                <th style={{ padding: "8px 10px" }}>Total</th>
                <th style={{ padding: "8px 10px" }}>Per step</th>
                <th style={{ padding: "8px 10px" }}>Result</th>
              </tr>
            </thead>
            <tbody>
              {data.recentRuns.map((r) => (
                <tr key={r.id} style={{ borderTop: "1px solid var(--border)" }}>
                  <td style={{ padding: "8px 10px", maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "var(--ink)" }}>{r.title}</td>
                  <td style={{ padding: "8px 10px", textTransform: "capitalize" }}>{r.profile ?? "—"}</td>
                  <td style={{ padding: "8px 10px" }}>{r.risk_tier ?? "—"}</td>
                  <td style={{ padding: "8px 10px" }}>{r.steps_run?.length ?? "—"}</td>
                  <td style={{ padding: "8px 10px", fontVariantNumeric: "tabular-nums", color: "var(--ink)" }}>{usd(r.cost_usd_micros)}</td>
                  <td style={{ padding: "8px 10px", color: "var(--muted)" }}>
                    {r.stage_costs
                      ? Object.entries(r.stage_costs).filter(([, v]) => v).map(([k, v]) => `${k.replace("_", " ")} ${usd(v)}`).join(" · ")
                      : "—"}
                  </td>
                  <td style={{ padding: "8px 10px", color: r.status === "completed" ? "#3fb950" : "#f85149" }}>{r.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p style={{ color: "var(--muted)", fontSize: 12.5, marginTop: 18, lineHeight: 1.6 }}>
        Custom profiles (your own tier ranges and per-step model routing) are available on BOSS
        Enterprise. Community edition uses the lean / balanced / strict presets and defaults new
        projects to lean.
      </p>
    </div>
  );
}
