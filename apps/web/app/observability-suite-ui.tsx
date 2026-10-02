"use client";

import React, { useMemo, useState, useTransition } from "react";

import type {
  ObservabilitySuiteFeed,
  RecommendResult,
  HealthCheckResult,
} from "../lib/observability-suite";
import {
  healthCheckObservabilitySources,
  recommendObservabilityStack,
} from "../lib/observability-suite";

const ROLE_COLOR: Record<string, string> = {
  metrics: "#3fb950",
  logs: "#58a6ff",
  traces: "#a371f7",
  visualization: "#d29922",
  collector: "#79c0ff",
  alerting: "#f85149",
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

function GapBanner({ gaps }: { gaps: { id: string; severity: string; message: string }[] }) {
  if (!gaps.length) return null;
  return (
    <div style={{ display: "grid", gap: 8 }}>
      {gaps.map((g) => (
        <div
          key={g.id}
          style={{
            padding: "10px 12px",
            borderRadius: 8,
            border: "1px solid var(--line)",
            borderLeft: `3px solid ${g.severity === "warning" ? "#d29922" : "#58a6ff"}`,
            background: "var(--panel-2, rgba(128,128,128,0.04))",
            fontSize: 13,
            color: "var(--ink-soft)",
          }}
        >
          {g.message}
        </div>
      ))}
    </div>
  );
}

export function ObservabilitySuitePanel({ feed }: { feed: ObservabilitySuiteFeed }) {
  const [profile, setProfile] = useState("web");
  const [recommend, setRecommend] = useState<RecommendResult | null>(null);
  const [health, setHealth] = useState<HealthCheckResult | null>(null);
  const [pending, startTransition] = useTransition();

  const profiles = feed.catalog?.profiles ?? [];
  const components = feed.catalog?.items ?? feed.status?.recommended_oss?.items ?? [];
  const status = feed.status;

  const profileOptions = useMemo(() => {
    if (profiles.length) return profiles;
    return [
      { id: "minimal", title: "Minimal", description: "", component_ids: [] },
      { id: "web", title: "Web / API", description: "", component_ids: [] },
      { id: "microservices", title: "Microservices", description: "", component_ids: [] },
      { id: "k8s", title: "Kubernetes", description: "", component_ids: [] },
      { id: "enterprise", title: "Enterprise", description: "", component_ids: [] },
    ];
  }, [profiles]);

  if (!feed.apiReachable) {
    return (
      <section className="ct-block" data-empty-state="observability-suite-unreachable">
        <div className="ct-block-title">Observability suite</div>
        <p className="empty-state" style={{ padding: "12px 0" }}>
          Control plane unreachable — catalog and status could not be loaded.
        </p>
      </section>
    );
  }

  return (
    <div style={{ display: "grid", gap: 16 }} data-area="observability-suite">
      <section className="ct-block">
        <div className="ct-block-title">Observability suite</div>
        <p className="detail" style={{ marginTop: 4, marginBottom: 12 }}>
          Unified view of registered connectors, the BOSS-managed stack (OTel → Prometheus →
          Grafana), and the recommended OSS set (Prometheus, Grafana, Loki, Tempo/Jaeger,
          Collector, Alertmanager).
        </p>

        {status ? (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
              gap: 10,
              marginBottom: 12,
            }}
          >
            <Stat
              label="Connectors"
              value={String(status.connectors.count)}
              hint={`${status.connectors.enabled_count} enabled`}
            />
            <Stat
              label="Stack"
              value={status.stack.deployed ? "deployed" : "not deployed"}
              hint={`${status.stack.scrape_app_count} scrape apps`}
              ok={status.stack.deployed}
            />
            <Stat
              label="Kinds"
              value={status.connectors.kinds_present.join(", ") || "—"}
              hint="registered"
            />
          </div>
        ) : null}

        {status?.gaps?.length ? <GapBanner gaps={status.gaps} /> : null}

        {status?.connectors.registered?.length ? (
          <div style={{ marginTop: 14 }}>
            <div style={{ fontSize: 12, fontWeight: 600, color: "var(--muted)", marginBottom: 6 }}>
              Registered sources
            </div>
            <div style={{ display: "grid", gap: 6 }}>
              {status.connectors.registered.map((s) => (
                <div
                  key={s.id}
                  style={{
                    display: "flex",
                    gap: 10,
                    alignItems: "center",
                    padding: "8px 10px",
                    borderRadius: 8,
                    background: "var(--panel-2, rgba(128,128,128,0.04))",
                    fontSize: 13,
                  }}
                >
                  <Pill text={s.kind} color="#58a6ff" />
                  <b style={{ color: "var(--ink)" }}>{s.name}</b>
                  <span style={{ color: "var(--muted)", fontFamily: "var(--mono, monospace)", fontSize: 12 }}>
                    {s.base_url || "—"}
                  </span>
                  <span style={{ marginLeft: "auto", color: s.enabled ? "#3fb950" : "#8b949e", fontSize: 11 }}>
                    {s.enabled ? "enabled" : "disabled"}
                  </span>
                </div>
              ))}
            </div>
          </div>
        ) : status?.gaps?.some((gap) => gap.id === "empty_observability") ? (
          // SAID ONCE. The gap banner above already says there are no sources and no stack;
          // repeating it here made three near-identical "not deployed" lines on one screen.
          null
        ) : (
          <p className="detail" style={{ marginTop: 12 }}>
            No telemetry sources registered yet. Deploy the BOSS monitoring stack from Infra
            (Deploy monitoring stack, beside the deployment targets); it registers Prometheus,
            Loki and Grafana here when it comes up.
          </p>
        )}
      </section>

      <section className="ct-block">
        <div className="ct-block-title">Recommended OSS stack</div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))",
            gap: 10,
            marginTop: 10,
          }}
        >
          {components.map((c) => (
            <div
              key={c.id}
              style={{
                padding: 12,
                borderRadius: 10,
                border: "1px solid var(--line)",
                background: "var(--panel-2, rgba(128,128,128,0.03))",
              }}
            >
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
                <b style={{ fontSize: 13, color: "var(--ink)" }}>{c.name}</b>
                <Pill text={c.role} color={ROLE_COLOR[c.role] ?? "#8b949e"} />
              </div>
              <div style={{ fontSize: 12.5, color: "var(--ink-soft)", lineHeight: 1.4 }}>
                {c.purpose}
              </div>
              {/* No GitHub-star counts: popularity figures from a catalogue nobody refreshes,
                  shown on an operations screen as if they were a measurement (console walk,
                  2026-10-02). The purpose above says what the component is for. */}
              {c.deployed_by_boss_stack ? (
                <div style={{ fontSize: 11, color: "#3fb950", marginTop: 6, fontWeight: 600 }}>
                  Covered by BOSS stack deploy
                </div>
              ) : null}
            </div>
          ))}
        </div>
      </section>

      <section className="ct-block">
        <div className="ct-block-title">Recommend for workload</div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 10, alignItems: "center" }}>
          <select
            value={profile}
            onChange={(e) => setProfile(e.target.value)}
            style={{
              background: "var(--panel)",
              color: "var(--ink)",
              border: "1px solid var(--line)",
              borderRadius: 8,
              padding: "6px 10px",
              fontSize: 13,
            }}
          >
            {profileOptions.map((p) => (
              <option key={p.id} value={p.id}>
                {p.title || p.id}
              </option>
            ))}
          </select>
          <button
            type="button"
            disabled={pending}
            onClick={() =>
              startTransition(async () => {
                const res = await recommendObservabilityStack(profile);
                setRecommend(res);
              })
            }
            style={btnStyle}
          >
            Recommend
          </button>
          <button
            type="button"
            disabled={pending}
            onClick={() =>
              startTransition(async () => {
                const res = await healthCheckObservabilitySources(feed.projectId, {
                  dryRun: true,
                });
                setHealth(res);
              })
            }
            style={btnStyle}
          >
            Health-check (dry-run)
          </button>
        </div>

        {recommend ? (
          <div style={{ marginTop: 12, fontSize: 13 }}>
            <b style={{ color: "var(--ink)" }}>{recommend.title}</b>
            <div style={{ color: "var(--ink-soft)", marginTop: 4 }}>{recommend.description}</div>
            <div style={{ marginTop: 8, display: "flex", flexWrap: "wrap", gap: 6 }}>
              {recommend.component_ids.map((id) => (
                <Pill key={id} text={id} color="#58a6ff" />
              ))}
            </div>
            {recommend.add_alongside_boss_stack?.length ? (
              <div style={{ marginTop: 8, color: "var(--muted)", fontSize: 12 }}>
                Add alongside BOSS stack: {recommend.add_alongside_boss_stack.join(", ")}
              </div>
            ) : null}
          </div>
        ) : null}

        {health ? (
          <div style={{ marginTop: 12 }}>
            <div style={{ fontSize: 12, fontWeight: 600, color: "var(--muted)", marginBottom: 6 }}>
              Health-check · {health.ok_count}/{health.count} ok
              {health.dry_run ? " (dry-run)" : ""}
            </div>
            <div style={{ display: "grid", gap: 6 }}>
              {health.items.map((item, i) => (
                <div
                  key={item.id ?? i}
                  style={{
                    display: "flex",
                    gap: 8,
                    alignItems: "center",
                    fontSize: 12.5,
                    padding: "6px 8px",
                    borderRadius: 6,
                    background: "var(--panel-2, rgba(128,128,128,0.04))",
                  }}
                >
                  <Pill
                    text={item.status}
                    color={
                      item.status === "config_ok" || item.status === "reachable"
                        ? "#3fb950"
                        : item.status === "misconfigured" || item.status === "unreachable"
                          ? "#f85149"
                          : "#8b949e"
                    }
                  />
                  <span style={{ color: "var(--ink)" }}>{item.name}</span>
                  <span style={{ color: "var(--muted)" }}>{item.kind}</span>
                  {item.config_error || item.note ? (
                    <span style={{ color: "var(--muted)", marginLeft: "auto", fontSize: 11 }}>
                      {item.config_error || item.note}
                    </span>
                  ) : null}
                </div>
              ))}
            </div>
          </div>
        ) : null}
      </section>
    </div>
  );
}

function Stat({
  label,
  value,
  hint,
  ok,
}: {
  label: string;
  value: string;
  hint?: string;
  ok?: boolean;
}) {
  return (
    <div
      style={{
        padding: "10px 12px",
        borderRadius: 10,
        border: "1px solid var(--line)",
        background: "var(--panel-2, rgba(128,128,128,0.03))",
      }}
    >
      <div style={{ fontSize: 11, color: "var(--muted)", textTransform: "uppercase", letterSpacing: "0.04em" }}>
        {label}
      </div>
      <div
        style={{
          fontSize: 16,
          fontWeight: 700,
          color: ok === false ? "#d29922" : ok ? "#3fb950" : "var(--ink)",
          marginTop: 2,
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {value}
      </div>
      {hint ? <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 2 }}>{hint}</div> : null}
    </div>
  );
}

const btnStyle: React.CSSProperties = {
  background: "transparent",
  color: "var(--ink-soft)",
  border: "1px solid var(--line)",
  borderRadius: 8,
  padding: "6px 12px",
  fontSize: 12.5,
  fontWeight: 600,
  cursor: "pointer",
};
