"use client";

import type {
  BudgetStatusCard,
  CostInsightsFeed,
  LlmSpendFeed,
} from "../lib/budget-status";

const SEV_COLOR: Record<string, string> = {
  high: "var(--red, #f2647a)",
  medium: "var(--amber, #f5b544)",
  low: "var(--muted, #8b949e)",
};

function usd(micros: number): string {
  return `$${(micros / 1e6).toFixed(2)}`;
}

export function CostInsightsCard({ feed }: { feed: CostInsightsFeed }) {
  return (
    <article className="stack-card">
      <h3 style={{ margin: 0 }}>Cost insights</h3>
      <p className="detail">
        Deterministic top cost-saves generated from the real spend ledger — each names the money
        and the one action that captures it.
      </p>
      {!feed.apiReachable ? (
        <p className="detail">API unreachable.</p>
      ) : feed.insights.length === 0 ? (
        <p className="detail">
          No material findings — spend below thresholds or already optimized.
        </p>
      ) : (
        <div style={{ display: "grid", gap: 10 }}>
          {feed.insights.map((i) => (
            <div
              key={i.kind}
              style={{
                borderLeft: `3px solid ${SEV_COLOR[i.severity] ?? SEV_COLOR.low}`,
                paddingLeft: 10,
              }}
            >
              <div style={{ display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
                <span
                  className="status-text"
                  style={{
                    color: SEV_COLOR[i.severity] ?? SEV_COLOR.low,
                    fontFamily: "var(--mono, monospace)",
                    fontSize: 11,
                    fontWeight: 700,
                    textTransform: "uppercase",
                    letterSpacing: "0.06em",
                  }}
                >
                  {i.severity} · {i.kind.replaceAll("_", " ")}
                </span>
                {i.potentialSavingUsdMicros > 0 ? (
                  <b style={{ fontSize: 12.5 }}>save ≈ {usd(i.potentialSavingUsdMicros)}</b>
                ) : null}
              </div>
              <p style={{ margin: "3px 0 2px", fontSize: 13, color: "var(--ink, #e8ecf2)" }}>
                {i.finding}
              </p>
              <p style={{ margin: 0, fontSize: 12.5, color: "var(--muted, #8b949e)" }}>
                → {i.action}
              </p>
            </div>
          ))}
        </div>
      )}
    </article>
  );
}

export function LlmSpendCard({ feed }: { feed: LlmSpendFeed }) {
  const maxSpend = Math.max(1, ...feed.byRole.map((r) => r.spendUsdMicros));
  return (
    <article className="stack-card">
      <h3 style={{ margin: 0 }}>Model spend by agent</h3>
      <p className="detail">
        Real LLM spend from llm_calls, segmented the way the cost question is asked — per
        agent role, per model, and cache leverage.
      </p>
      {!feed.apiReachable ? (
        <p className="detail">API unreachable.</p>
      ) : (
        <>
          <div style={{ display: "grid", gap: 6 }}>
            {feed.byRole.map((r) => (
              <div key={r.role} style={{ display: "grid", gap: 3 }}>
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12.5 }}>
                  <span style={{ fontFamily: "var(--mono, monospace)" }}>{r.role}</span>
                  <span>
                    <b>{usd(r.spendUsdMicros)}</b>
                    <span style={{ color: "var(--muted, #8b949e)" }}> · {r.calls} calls</span>
                  </span>
                </div>
                <div
                  aria-hidden
                  style={{
                    height: 3,
                    background: "var(--hair, #131d34)",
                    position: "relative",
                  }}
                >
                  <div
                    style={{
                      position: "absolute",
                      inset: 0,
                      width: `${Math.round((r.spendUsdMicros / maxSpend) * 100)}%`,
                      background: "var(--teal, #2ec5c5)",
                    }}
                  />
                </div>
              </div>
            ))}
            {feed.byRole.length === 0 ? <p className="detail">No model calls recorded.</p> : null}
          </div>
          {feed.cache ? (
            <p className="detail" style={{ marginTop: 10 }}>
              Cache:{" "}
              <b
                style={{
                  color:
                    (feed.cache.cacheHitRatio ?? 0) < 0.3
                      ? "var(--red, #f2647a)"
                      : "var(--green, #34d399)",
                }}
              >
                {feed.cache.cacheHitRatio == null
                  ? "n/a"
                  : `${Math.round(feed.cache.cacheHitRatio * 100)}% hit ratio`}
              </b>{" "}
              · {feed.cache.freshInputTokens.toLocaleString("en-US")} fresh ·{" "}
              {feed.cache.cacheReadTokens.toLocaleString("en-US")} cached reads
            </p>
          ) : null}
        </>
      )}
    </article>
  );
}

export function BudgetThresholdsCard({ status }: { status: BudgetStatusCard }) {
  const rows: Array<{ label: string; pct?: number | null; note: string; active: boolean }> = [
    {
      label: "SOFT CAP",
      pct: status.softCapPct,
      note: "yellow — heads-up",
      active: status.utilizationRatio >= (status.softCapPct ?? 1),
    },
    {
      label: "AUTO-LITE",
      pct: status.autoLiteModePct,
      note: "orange — build stages route low-cost (verification never degrades)",
      active: status.utilizationRatio >= (status.autoLiteModePct ?? 1),
    },
    {
      label: "FREEZE PROVISIONING",
      pct: status.freezeProvisioningPct,
      note: "red — no new scale-ups",
      active: status.utilizationRatio >= (status.freezeProvisioningPct ?? 1),
    },
    {
      label: "FREEZE TIER-2",
      pct: status.freezeTier2Pct,
      note: "tier-2+ risk work refused at reserve",
      active: Boolean(status.tier2Frozen),
    },
  ];
  return (
    <article className="stack-card">
      <h3 style={{ margin: 0 }}>Enforcement thresholds</h3>
      <p className="detail">
        Every knob here is enforced by the engine — utilization{" "}
        {Math.round(status.utilizationRatio * 100)}% of cap.
      </p>
      <div style={{ display: "grid", gap: 6 }}>
        {rows.map((r) => (
          <div
            key={r.label}
            style={{
              display: "flex",
              justifyContent: "space-between",
              gap: 10,
              fontSize: 12.5,
              borderBottom: "1px solid var(--hair, #131d34)",
              paddingBottom: 5,
            }}
          >
            <span
              style={{
                fontFamily: "var(--mono, monospace)",
                fontWeight: 700,
                letterSpacing: "0.06em",
                color: r.active ? "var(--red, #f2647a)" : "var(--muted, #8b949e)",
              }}
            >
              {r.active ? "● " : "○ "}
              {r.label}
              {r.pct != null ? ` ≥ ${Math.round(r.pct * 100)}%` : ""}
            </span>
            <span style={{ color: "var(--muted, #8b949e)", textAlign: "right" }}>{r.note}</span>
          </div>
        ))}
      </div>
    </article>
  );
}
