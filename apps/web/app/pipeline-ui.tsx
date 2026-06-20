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

// The cost story, in one line per profile: how many separate AI agents, and the tradeoff.
function blurbFor(name: string): string {
  if (name === "lean")
    return "One builder agent (plus a separate tester) on a low-cost model. No independent reviewer — fast and lowest cost, best for prototypes; less independent checking.";
  if (name === "balanced")
    return "A separate tester, builder, and a reviewer that tries to break the change — strong model where it matters. Independent checks catch more, at moderate cost. The everyday default.";
  if (name === "strict")
    return "A full panel of specialized agents — tester, builder, reviewer, security — all on the strong model, with extra iterations. Production-grade quality; highest cost.";
  return "Your chosen mix of agents, tiers, and per-step models.";
}

// Each step is a separate agent; cost ≈ number of agents × model strength.
function modelClass(p: PipelineProfile, step: string): string {
  return p.model[step] ?? p.model.default ?? "strong";
}
function costUnits(p: PipelineProfile, steps: string[]): number {
  return steps.reduce((a, s) => a + (modelClass(p, s) === "strong" ? 2 : 1), 0);
}
const MAX_COST_UNITS = 6; // 3 agents on the strong model (the strict standard-task case)

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
  const agents = steps.length;
  const anyStrong = steps.some((s) => modelClass(p, s) === "strong");
  const costPct = Math.min(100, Math.round((costUnits(p, steps) / MAX_COST_UNITS) * 100));
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
      <div style={{ color: "var(--muted)", fontSize: 12, margin: "5px 0 9px" }}>{p.label}</div>

      <div style={{ fontSize: 12.5, color: "var(--ink-soft)", lineHeight: 1.55, marginBottom: 12 }}>
        {blurbFor(p.name)}
      </div>

      <div style={{ fontSize: 12.5, lineHeight: 1.85, color: "var(--ink-soft)" }}>
        <div>
          A standard task runs{" "}
          <b style={{ color: "var(--ink)" }}>{agents} separate AI {agents === 1 ? "agent" : "agents"}</b>{" "}
          <span style={{ color: "var(--muted)" }}>(tier {tier})</span>
        </div>
        <div style={{ color: "var(--muted)" }}>{steps.join(" → ")}</div>
        <div style={{ color: "var(--muted)" }}>
          Model: <b style={{ color: "var(--ink-soft)" }}>{anyStrong ? "strong where it matters" : "low-cost"}</b> · risk tiers {p.tier_floor}–{p.tier_cap}
        </div>
      </div>

      <div style={{ marginTop: 12 }}>
        <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 4, display: "flex", justifyContent: "space-between" }}>
          <span>relative cost</span>
          <span>{agents} {agents === 1 ? "agent" : "agents"} × {anyStrong ? "strong" : "low-cost"} model</span>
        </div>
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
      <p style={{ color: "var(--muted)", margin: "0 0 18px", maxWidth: 820, fontSize: 13, lineHeight: 1.65 }}>
        Every step of the pipeline runs as a <b style={{ color: "var(--ink)" }}>separate AI agent</b>, and
        that&apos;s what drives the cost. <b style={{ color: "var(--ink)" }}>Lower-cost</b> profiles use{" "}
        <b style={{ color: "var(--ink)" }}>fewer agents</b> — one agent builds and tests on a low-cost
        model: quick results, less independent checking (good for prototypes).{" "}
        <b style={{ color: "var(--ink)" }}>Higher-rigor</b> profiles run{" "}
        <b style={{ color: "var(--ink)" }}>more specialized agents</b> — a separate tester, an independent
        reviewer, and a security pass that each try to break the others&apos; work, on the strong model and
        with extra iterations: production-grade quality, higher cost. Effort scales with risk
        automatically; sensitive changes (auth, payments, secrets) always run at least standard rigor.
        Current default:{" "}
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
