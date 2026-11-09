"use client";

import type {
  OncallHolder,
  OncallPage,
  OncallSreStatus,
} from "../lib/oncall-sre";

const SEV_COLOR: Record<string, string> = {
  critical: "#f85149",
  warning: "#d29922",
  info: "#58a6ff",
};

const STATUS_COLOR: Record<string, string> = {
  open: "#f85149",
  acked: "#d29922",
  resolved: "#3fb950",
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
        textTransform: "none",
        letterSpacing: "0",
        whiteSpace: "nowrap",
      }}
    >
      {text}
    </span>
  );
}

function PageRow({ page }: { page: OncallPage }) {
  const sev = SEV_COLOR[page.severity] ?? "#8b949e";
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "9px 10px",
        borderLeft: `3px solid ${sev}`,
        background: "var(--panel-2, var(--boss-c-8123cd9e1f))",
        borderRadius: 8,
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <b
            style={{
              fontSize: 13,
              color: "var(--ink)",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {page.title}
          </b>
          {page.service ? (
            <span
              style={{
                fontSize: 11.5,
                color: "var(--muted)",
                fontFamily: "var(--mono, monospace)",
              }}
            >
              {page.service}
            </span>
          ) : null}
          {page.kg_node_key ? (
            <span style={{ fontSize: 11, color: "var(--muted)" }}>
              KG, {page.kg_node_key}
            </span>
          ) : null}
        </div>
        <div style={{ fontSize: 11.5, color: "var(--muted)", marginTop: 2 }}>
          {page.assignee ? `assignee ${page.assignee}, ` : ""}
          adapter {page.adapter ?? "—"}
          {page.reliability_incident_id
            ? `, rel ${page.reliability_incident_id.slice(0, 12)}…`
            : ""}
        </div>
      </div>
      <Pill text={page.status} color={STATUS_COLOR[page.status] ?? "#8b949e"} />
      <Pill text={page.severity} color={sev} />
    </div>
  );
}

function OncallList({ holders }: { holders: OncallHolder[] }) {
  if (!holders.length) {
    return <p className="detail">No schedules with active participants.</p>;
  }
  return (
    <div style={{ display: "grid", gap: 6 }}>
      {holders.map((h) => (
        <div
          key={`${h.schedule_id}-${h.layer}-${h.user_id}`}
          style={{
            display: "flex",
            gap: 10,
            alignItems: "center",
            padding: "8px 10px",
            borderRadius: 8,
            background: "var(--panel-2, var(--boss-c-8123cd9e1f))",
          }}
        >
          <div style={{ flex: 1 }}>
            <b style={{ fontSize: 13, color: "var(--ink)" }}>
              {h.display_name || h.user_id || "—"}
            </b>
            <div style={{ fontSize: 11.5, color: "var(--muted)" }}>
              {h.schedule_name}
              {h.layer ? `, ${h.layer}` : ""}
              {h.contact ? `, ${h.contact}` : ""}
            </div>
          </div>
          <Pill text="on-call" color="#3fb950" />
        </div>
      ))}
    </div>
  );
}

export function OncallSreHome({ status }: { status: OncallSreStatus }) {
  const pages = [...(status.pages_open || []), ...(status.pages_acked || [])];
  return (
    <div className="stack-grid">
      <article className="stack-card">
        <h3 style={{ margin: 0 }}>On-call control plane</h3>
        <p className="detail">
          Schedules, escalation policies, and pages that bridge reliability
          incidents into the on-call path. Notify adapters default to dry-run
          (receipted, <code>simulated=true</code>) until a token/webhook is
          configured.
        </p>
        {!status.apiReachable ? (
          <p className="detail">
            API unreachable, status will populate once the control plane responds.
          </p>
        ) : null}
      </article>

      <div
        style={{
          display: "grid",
          gap: 14,
          gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
        }}
      >
        <article className="stack-card">
          <h3 style={{ margin: 0 }}>Who is on-call</h3>
          <OncallList holders={status.current_oncall || []} />
        </article>

        <article className="stack-card">
          <h3 style={{ margin: 0 }}>Coverage</h3>
          <div
            style={{
              display: "grid",
              gap: 5,
              fontSize: 12.5,
              color: "var(--ink-soft)",
            }}
          >
            <div>
              Schedules:{" "}
              <b style={{ color: "var(--ink)" }}>
                {status.schedules?.length ?? 0}
              </b>
            </div>
            <div>
              Policies:{" "}
              <b style={{ color: "var(--ink)" }}>
                {status.policies?.length ?? 0}
              </b>
            </div>
            <div>
              Open pages:{" "}
              <b style={{ color: "var(--ink)" }}>{status.open_pages ?? 0}</b>
            </div>
            <div>
              Acked pages:{" "}
              <b style={{ color: "var(--ink)" }}>{status.acked_pages ?? 0}</b>
            </div>
          </div>
        </article>

        <article className="stack-card">
          <h3 style={{ margin: 0 }}>Adapters</h3>
          <p className="detail">
            Live only when the matching env token is present; otherwise dry-run.
          </p>
          <div style={{ display: "grid", gap: 4, fontSize: 12 }}>
            {(status.adapters || []).map((a) => (
              <div
                key={a.adapter}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  gap: 8,
                }}
              >
                <code>{a.adapter}</code>
                <Pill
                  text={a.live_capable ? "live" : "dry-run"}
                  color={a.live_capable ? "#3fb950" : "#8b949e"}
                />
              </div>
            ))}
          </div>
        </article>
      </div>

      <article className="stack-card">
        <h3 style={{ margin: 0 }}>Active pages</h3>
        <p className="detail">
          Open and acknowledged pages. Acknowledging or resolving a page is recorded against
          its incident; ask the on-call agent from the incident to do either.
        </p>
        {pages.length === 0 ? (
          <p className="detail">No open or acked pages.</p>
        ) : (
          <div style={{ display: "grid", gap: 6 }}>
            {pages.map((p) => (
              <PageRow key={p.id} page={p} />
            ))}
          </div>
        )}
      </article>

      {(status.schedules || []).length > 0 ? (
        <article className="stack-card">
          <h3 style={{ margin: 0 }}>Schedules</h3>
          <div style={{ display: "grid", gap: 6, fontSize: 12.5 }}>
            {status.schedules.map((s) => (
              <div key={s.id}>
                <b style={{ color: "var(--ink)" }}>{s.name}</b>
                <span style={{ color: "var(--muted)" }}>
                  {" "}
                 , {s.timezone}, {(s.layers || []).length} layer(s)
                  {s.enabled ? "" : ", disabled"}
                </span>
              </div>
            ))}
          </div>
        </article>
      ) : null}
    </div>
  );
}
