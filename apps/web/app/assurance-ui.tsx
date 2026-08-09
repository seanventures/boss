import type { AssuranceFeed } from "../lib/assurance";
import React from "react";

function text(item: Record<string, unknown>, key: string, fallback = "—"): string {
  const value = item[key];
  return typeof value === "string" && value ? value : fallback;
}

function Status({ value }: { value: string }) {
  const normalized = value.toLowerCase();
  const color = ["passed", "healthy", "accepted", "current", "active"].includes(
    normalized,
  )
    ? "var(--green)"
    : ["blocked", "refuted", "breached", "failed"].includes(normalized)
      ? "var(--red)"
      : "var(--amber)";
  return (
    <span
      style={{
        color,
        border: `1px solid color-mix(in srgb, ${color} 42%, transparent)`,
        borderRadius: 999,
        padding: "2px 8px",
        fontSize: 11,
        fontWeight: 700,
        textTransform: "uppercase",
      }}
    >
      {value || "unknown"}
    </span>
  );
}

function CountCard({ label, value, detail }: { label: string; value: number; detail: string }) {
  return (
    <article className="stack-card">
      <span className="eyebrow">{label}</span>
      <strong style={{ display: "block", fontSize: 32, marginTop: 5 }}>{value}</strong>
      <p className="detail" style={{ marginBottom: 0 }}>{detail}</p>
    </article>
  );
}

export function AssuranceCenter({ feed }: { feed: AssuranceFeed }) {
  const { overview, modelUsage } = feed;
  const openRegressions = overview.metrics.filter(
    (item) => text(item, "health_status", "unknown") === "breached",
  ).length;
  const verifierFailures = overview.verification_runs.filter((item) =>
    ["refuted", "failed"].includes(text(item, "outcome", text(item, "status", ""))),
  ).length;
  const totalCost = Number(modelUsage.totals.cost_usd_micros ?? 0) / 1_000_000;

  return (
    <div className="stack-grid">
      <div className="section-head">
        <div>
          <span className="eyebrow">Assurance Center</span>
          <h2>Ground truth is measured, fenced, and independently verified.</h2>
          <p className="detail">
            Regression alarms, settled conclusions, proof-bearing claims, surface ownership,
            and standing verifier runs share one auditable control plane.
          </p>
        </div>
        <Status value={feed.apiReachable ? "active" : "unavailable"} />
      </div>

      <div style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))" }}>
        <CountCard label="Measured contracts" value={overview.metrics.length} detail={`${openRegressions} currently breached`} />
        <CountCard label="Assurance cases" value={overview.cases.length} detail="Claims linked to reproducible evidence" />
        <CountCard label="Owned surfaces" value={overview.surfaces.length} detail="Single deployer with version fencing" />
        <CountCard label="Verifier runs" value={overview.verification_runs.length} detail={`${verifierFailures} refuted or failed`} />
      </div>

      <div style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(310px, 1fr))" }}>
        <article className="stack-card">
          <h3 style={{ marginTop: 0 }}>Assurance cases</h3>
          {overview.cases.length === 0 ? <p className="detail">No cases recorded yet.</p> : (
            <div style={{ display: "grid", gap: 8 }}>
              {overview.cases.slice(0, 6).map((item) => (
                <div key={text(item, "id")} style={{ display: "flex", gap: 8, justifyContent: "space-between" }}>
                  <span>{text(item, "title")}</span>
                  <Status value={text(item, "status", "draft")} />
                </div>
              ))}
            </div>
          )}
        </article>

        <article className="stack-card">
          <h3 style={{ marginTop: 0 }}>Settled conclusions</h3>
          {overview.settled_conclusions.length === 0 ? <p className="detail">No closed questions recorded yet.</p> : (
            <div style={{ display: "grid", gap: 8 }}>
              {overview.settled_conclusions.slice(0, 6).map((item) => (
                <div key={text(item, "id")}>
                  <strong>{text(item, "conclusion_key")}</strong>
                  <p className="detail" style={{ margin: "3px 0 0" }}>{text(item, "conclusion")}</p>
                </div>
              ))}
            </div>
          )}
        </article>

        <article className="stack-card">
          <h3 style={{ marginTop: 0 }}>Model usage · 30 days</h3>
          <p className="detail">
            {Number(modelUsage.totals.calls ?? 0).toLocaleString()} calls ·{" "}
            {Number(modelUsage.totals.total_tokens ?? 0).toLocaleString()} tokens ·{" "}
            {totalCost.toLocaleString("en-US", { style: "currency", currency: "USD" })}
          </p>
          {modelUsage.items.slice(0, 6).map((item) => (
            <div key={`${item.engine}:${item.model ?? "default"}`} style={{ display: "flex", justifyContent: "space-between", gap: 10, marginTop: 7 }}>
              <span>{item.engine} / {item.model ?? "default"}</span>
              <span className="detail">{item.total_tokens.toLocaleString()} tok</span>
            </div>
          ))}
        </article>
      </div>
    </div>
  );
}
