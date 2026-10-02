"use client";

import type { AiSreCapability, AiSreFeed } from "../lib/ai-sre";

const STATUS_COLOR: Record<string, string> = {
  shipped: "#3fb950",
  partial: "#d29922",
  missing: "#8b949e",
};

function Pill({ text, color }: { text: string; color: string }) {
  return (
    <span
      style={{
        fontSize: 11,
        fontWeight: 600,
        padding: "2px 8px",
        borderRadius: 999,
        color,
        border: `1px solid ${color}55`,
        background: `${color}18`,
        textTransform: "uppercase",
        letterSpacing: "0.04em",
        whiteSpace: "nowrap",
      }}
    >
      {text}
    </span>
  );
}

function CapabilityRow({ item }: { item: AiSreCapability }) {
  const color = STATUS_COLOR[item.status] ?? "#8b949e";
  return (
    <tr>
      <td style={{ padding: "9px 10px", verticalAlign: "top" }}>
        <div style={{ fontWeight: 600, color: "var(--ink)", fontSize: 13 }}>
          {item.awesome}
        </div>
        <div
          style={{
            fontSize: 11,
            color: "var(--muted)",
            fontFamily: "var(--mono, monospace)",
            marginTop: 2,
          }}
        >
          {item.id}
        </div>
      </td>
      <td style={{ padding: "9px 10px", verticalAlign: "top" }}>
        <Pill text={item.status} color={color} />
      </td>
      <td style={{ padding: "9px 10px", verticalAlign: "top", color: "var(--ink-soft)", fontSize: 12.5 }}>
        {item.gap || "—"}
      </td>
    </tr>
  );
}

export function AiSreHome({ feed }: { feed: AiSreFeed }) {
  const ledger = feed.ledger;
  const counts = ledger?.counts;
  return (
    <div className="stack-grid">
      <article className="stack-card">
        <h3 style={{ margin: 0 }}>AI SRE capability ledger</h3>
        <p className="detail">
          awesome-ai-sre categories mapped onto BOSS. Missing stays missing.
          This is a coverage ledger, not a live scoreboard — no invented
          incident counts or RCA confidence.
        </p>
        {!feed.apiReachable || !ledger ? (
          <p className="detail">
            The BOSS API did not answer for the AI SRE ledger. Reload the page; if it stays
            empty, check that the API is running in Repo and model.
          </p>
        ) : (
          <>
            <div
              style={{
                display: "flex",
                gap: 10,
                flexWrap: "wrap",
                alignItems: "center",
                margin: "8px 0 12px",
              }}
            >
              <Pill text={ledger.public_ga ? "public GA" : "not public GA"} color={ledger.public_ga ? "#3fb950" : "#8b949e"} />
              <span style={{ fontSize: 12.5, color: "var(--ink-soft)" }}>
                shipped <b style={{ color: "var(--ink)" }}>{counts?.shipped ?? 0}</b>
                {" · "}
                partial <b style={{ color: "var(--ink)" }}>{counts?.partial ?? 0}</b>
                {" · "}
                missing <b style={{ color: "var(--ink)" }}>{counts?.missing ?? 0}</b>
              </span>
            </div>
            <div style={{ overflowX: "auto" }}>
              <table
                aria-label="AI SRE categories with honest status and remaining gap"
                style={{
                  width: "100%",
                  borderCollapse: "collapse",
                  fontSize: 13,
                }}
              >
                <thead>
                  <tr
                    style={{
                      textAlign: "left",
                      color: "var(--muted)",
                      fontSize: 11,
                      letterSpacing: "0.04em",
                      textTransform: "uppercase",
                    }}
                  >
                    <th style={{ padding: "6px 10px", fontWeight: 600 }}>Category</th>
                    <th style={{ padding: "6px 10px", fontWeight: 600 }}>Status</th>
                    <th style={{ padding: "6px 10px", fontWeight: 600 }}>Gap</th>
                  </tr>
                </thead>
                <tbody>
                  {ledger.capabilities.map((item) => (
                    <CapabilityRow key={item.id} item={item} />
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </article>
    </div>
  );
}
