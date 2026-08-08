"use client";

/**
 * The CI/CD area: deploy something, hold the credentials it needs, see what shipped.
 *
 * Reordered around what an operator came here to do. It used to open with "Pipeline — code
 * task → PR → deployment", a table of every code task run that had ever raised a PR — eighteen
 * rows, nearly all with no deployment attached. That is not a delivery report; it is the Code
 * Tasks list wearing one. Deploying, the actual verb of this page, was a text box at the
 * bottom of a target row.
 *
 * So: the wizard is first, `RecentDeployments` lists DEPLOYMENTS, and both hand off to the
 * live stepper in `deploy-ui.tsx` — which can exist at all only because a deployment now
 * records its steps while it runs instead of reporting nothing until it ends.
 *
 * Kept from the previous version: when no target is configured it says so rather than
 * rendering an empty table that reads like a failure.
 */

import { useState } from "react";

import { OnboardWizard, SecretsPanel, TargetControls, type SecretsView } from "./delivery-manage";
import { DeployWizard, RecentDeployments } from "./deploy-ui";

type Stage = {
  run_id: string; title: string; run_status: string;
  pull_request_url: string | null; pull_request_number: number | null;
  deployment_run_id: string | null; deploy_status: string | null;
  environment: string | null; app_name: string | null; target_kind: string | null;
  commit_ref: string | null; started_at: string | null; finished_at: string | null;
  failure_reason: string | null;
  health_check_results: { healthy?: boolean; checks?: { url?: string; status?: number; ok?: boolean }[] } | null;
  target_domains: string[] | null;
};
type Target = {
  id: string; app_name: string; environment: string; repo: string;
  target_kind: string; target_ref: string; status: string;
  // Unpacked from required_resources by the API so the branch picker and the plan line do not
  // each have to dig through it — and cannot each dig differently.
  repo_owner?: string | null; app_entry?: string | null; app_port?: string | null;
  domains?: string[] | null;
  auto_deploy?: boolean; paused?: boolean;
};
export type Pipeline = {
  project_id: string; stages: Stage[]; targets: Target[]; configured: boolean;
  error?: string;
};

const tone = (s: string | null) => {
  if (!s) return "#8b949e";
  if (["succeeded", "deployed", "healthy"].includes(s)) return "#3fb950";
  if (["failed", "rolled_back", "unhealthy"].includes(s)) return "#f85149";
  if (["created", "running", "in_progress"].includes(s)) return "#d29922";
  return "#8b949e";
};

/** "3h 12m" — how long ago, or "" when the timestamp is absent. */
function since(iso: string | null): string {
  if (!iso) return "";
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "";
  const m = Math.floor(ms / 60000);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

/** How long the deploy itself took — start to finish, not wall-clock since. */
function took(a: string | null, b: string | null): string {
  if (!a || !b) return "";
  const ms = new Date(b).getTime() - new Date(a).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "";
  return ms < 60000 ? `${Math.round(ms / 1000)}s` : `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
}

/** The URL the deployment is actually serving on, preferring what health CHECKED. */
type HealthTarget = {
  target_id: string; app_name: string; environment: string; url: string | null;
  // `not_deployed` is a registered destination with nothing shipped to it — a distinct
  // state from "unknown" (probed, no data) and from "down".
  status: "up" | "degraded" | "down" | "unknown" | "not_deployed";
  status_since: string | null;
  uptime_24h: number | null; uptime_7d: number | null;
  avg_ms: number | null; p95_ms: number | null;
  last_checked: string | null; last_latency_ms: number | null; last_error: string | null;
  sample_count: number;
  series: { at: string; ok: boolean; ms: number | null }[];
  incidents: { started_at: string; ended_at: string | null; duration_seconds: number | null; error: string | null }[];
  deployed_commit: string | null; deployed_at: string | null; deployment_run_id: string | null;
};
export type Health = {
  project_id: string; targets: HealthTarget[];
  summary: { up: number; degraded: number; down: number; unknown: number;
             not_deployed?: number; overall: string; total: number };
  error?: string;
};

const HEALTH_TONE: Record<string, string> = {
  up: "#3fb950", degraded: "#d29922", down: "#f85149", unknown: "#8b949e",
  // An empty slot, not an outage — grey like "unknown" but it says something different.
  not_deployed: "#8b949e",
};
const HEALTH_LABEL: Record<string, string> = { not_deployed: "never deployed" };

/** Latency over time as inline SVG. Failures are drawn, not dropped — a gap in a line reads
 *  as "no data", which is the opposite of what a failed probe means. */
function HealthGraph({ series }: { series: HealthTarget["series"] }) {
  const W = 260, H = 40;
  if (!series.length) return <span className="detail" style={{ fontSize: 11 }}>no samples yet</span>;
  const oks = series.filter((p) => p.ok && p.ms != null).map((p) => p.ms as number);
  const max = Math.max(1, ...oks);
  const dx = series.length > 1 ? W / (series.length - 1) : W;

  const line = series
    .map((p, i) => (p.ok && p.ms != null ? `${i === 0 ? "M" : "L"}${(i * dx).toFixed(1)},${(H - (p.ms / max) * (H - 6) - 3).toFixed(1)}` : null))
    .filter(Boolean).join(" ");

  return (
    <svg width={W} height={H} role="img" aria-label="latency history"
         style={{ display: "block", overflow: "visible" }}>
      {line && <path d={line} fill="none" stroke="#3fb950" strokeWidth="1.5" opacity={0.9} />}
      {series.map((p, i) => !p.ok && (
        <rect key={i} x={Math.max(0, i * dx - 1)} y={0} width={2} height={H} fill="#f85149" opacity={0.85} />
      ))}
    </svg>
  );
}

export function DeploymentHealth({ health }: { health?: Health | null }) {
  if (!health) return null;
  if (health.error) {
    return <div className="detail" style={{ color: "#f85149" }}>Health: {health.error}</div>;
  }
  const { summary, targets } = health;

  return (
    <section className="ct-block">
      <div className="ct-block-title">
        Health of deployed apps{" "}
        <span style={{ color: HEALTH_TONE[summary.overall], fontSize: 12 }}>
          ● {summary.overall}
        </span>
        <span className="detail" style={{ fontSize: 12, marginLeft: 8 }}>
          {summary.up} up · {summary.degraded} degraded · {summary.down} down
          {summary.unknown ? ` · ${summary.unknown} unknown` : ""}
          {summary.not_deployed ? ` · ${summary.not_deployed} never deployed` : ""}
        </span>
      </div>

      {targets.length === 0 && (
        <div className="detail">
          Nothing deployed yet. Once BOSS deploys an app, its health is sampled automatically.
        </div>
      )}

      <div style={{ display: "grid", gap: 12 }}>
        {targets.map((t) => (
          <div key={t.target_id} style={{
            border: "1px solid rgba(255,255,255,.09)", borderRadius: 8, padding: 12,
            display: "flex", gap: 18, flexWrap: "wrap", alignItems: "flex-start",
          }}>
            <div style={{ minWidth: 200, display: "grid", gap: 4 }}>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <span style={{ color: HEALTH_TONE[t.status], fontWeight: 600 }}>
                  ● {HEALTH_LABEL[t.status] ?? t.status}
                </span>
                <strong style={{ fontSize: 13 }}>{t.app_name}</strong>
                <span className="cap-badge tone-blue">{t.environment}</span>
              </div>
              {/* A target that has never been deployed has nothing at that address. Offering a
                  link to it produces a dead page and reads as a broken app — the same "control
                  that can only fail" pattern as a button that does nothing. Show the planned
                  address as text instead, so it is information rather than an invitation. */}
              {t.url && (t.status === "not_deployed"
                ? <span className="detail" style={{ fontSize: 12, fontFamily: "monospace" }}>
                    {t.url} (planned — not serving yet)
                  </span>
                : <a href={`http://${t.url}/`} target="_blank" rel="noreferrer"
                     style={{ fontSize: 12, fontFamily: "monospace" }}>{t.url} ↗</a>)}
              <span className="detail" style={{ fontSize: 11 }}>
                {t.status === "not_deployed"
                  ? "registered as a destination; nothing has been deployed here yet"
                  : t.status_since ? `${t.status} for ${since(t.status_since)}` : "never probed"}
              </span>
            </div>

            <div style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 12 }}>
              {/* null uptime is "no data", never 0% — an unprobed target has not had an outage. */}
              <Fact label="uptime 24h" value={t.uptime_24h == null ? "no data" : `${t.uptime_24h}%`} />
              <Fact label="uptime 7d" value={t.uptime_7d == null ? "no data" : `${t.uptime_7d}%`} />
              <Fact label="latency" value={t.last_latency_ms == null ? "—" : `${t.last_latency_ms}ms`} />
              <Fact label="p95" value={t.p95_ms == null ? "—" : `${t.p95_ms}ms`} />
              <Fact label="build" value={t.deployed_commit ? t.deployed_commit.slice(0, 12) : "—"} mono />
              <Fact label="deployed" value={t.deployed_at ? `${since(t.deployed_at)} ago` : "—"} />
              <Fact label="checked" value={t.last_checked ? `${since(t.last_checked)} ago` : "—"} />
            </div>

            <div style={{ display: "grid", gap: 3 }}>
              <span style={{ opacity: .55, fontSize: 11, fontFamily: "monospace" }}>
                latency · {t.sample_count} samples
              </span>
              <HealthGraph series={t.series} />
            </div>

            {t.last_error && (
              <div className="detail" style={{ color: "#f85149", fontSize: 12, width: "100%" }}>
                {t.last_error}
              </div>
            )}

            {t.incidents.length > 0 && (
              <div style={{ width: "100%", fontSize: 12 }}>
                <span style={{ opacity: .55, fontFamily: "monospace", fontSize: 11 }}>incidents</span>
                {t.incidents.map((i, n) => (
                  <div key={n} className="detail" style={{ fontSize: 12 }}>
                    {new Date(i.started_at).toLocaleString()} ·{" "}
                    {i.ended_at
                      ? `recovered after ${Math.max(1, Math.round((i.duration_seconds ?? 0) / 60))}m`
                      : "ONGOING"}
                    {i.error ? ` — ${i.error}` : ""}
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

export function DeliveryPipeline({ initial, secrets, deployments }: {
  initial?: Pipeline | null; secrets?: SecretsView | null; deployments?: any;
}) {
  const data = initial;
  // Has this target ever shipped? Only used to LABEL the button — an app already running is
  // being replaced, and calling that "Deploy" reads like it might do nothing, or like there is
  // a separate redeploy elsewhere. There isn't: it is one action either way.
  const everShipped = new Set<string>(
    ((deployments?.deployments || []) as { app_name?: string; status?: string }[])
      .filter((d) => d.status === "succeeded")
      .map((d) => d.app_name || ""),
  );

  if (!data) return <div className="detail">Loading delivery pipeline…</div>;
  if (data.error) {
    return <div className="detail" style={{ color: "#f85149" }}>Delivery: {data.error}</div>;
  }

  return (
    <div style={{ display: "grid", gap: 18 }}>
      <div><DeployWizard targets={(data.targets || []) as any} /></div>

      {/* Onboarding and credentials: the steps that used to be SQL and ssh-keygen.
          Health and the metrics stack now live in the Monitoring area — this page is about
          getting a change shipped, not about how it behaves afterwards. */}
      <div><OnboardWizard secrets={secrets?.secrets ?? []} /></div>
      <SecretsPanel initial={secrets} />

      {/* ---- targets: where anything could go at all ---- */}
      <section className="ct-block">
        <div className="ct-block-title">Deployment targets</div>
        {!data.configured ? (
          <div className="detail">
            No deployment target is registered, so a merged PR deploys nowhere. Register one
            (app, repo, environment, target kind) and merges to <code>staging</code> will
            deploy automatically; anything else stays behind a promotion decision.
          </div>
        ) : (
          <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
            <thead><tr style={{ textAlign: "left", opacity: .7 }}>
              <th>app</th><th>environment</th><th>repo</th><th>kind</th><th>ref</th><th>status</th>
            </tr></thead>
            <tbody>
              {data.targets.map((t) => (
                <>
                <tr key={t.id} style={{ borderTop: "1px solid rgba(255,255,255,.07)" }}>
                  <td>{t.app_name}</td>
                  <td><span className="cap-badge tone-blue">{t.environment}</span></td>
                  <td className="detail">{t.repo}</td>
                  <td>{t.target_kind}</td>
                  <td className="detail">{t.target_ref}</td>
                  <td style={{ color: t.status === "active" ? "#3fb950" : "#8b949e" }}>{t.status}</td>
                </tr>
                <tr key={`${t.id}-controls`}>
                  {/* Trigger controls sit with the target they act on, so "deploys on merge"
                      is read in the same glance as which host it deploys to. */}
                  <td colSpan={6} style={{ paddingBottom: 10 }}>
                    <TargetControls target={t} shipped={everShipped.has(t.app_name)} />
                  </td>
                </tr>
                </>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {/* The pipeline table listed every code task run that had ever opened a PR — eighteen
          rows, nearly all with no deployment. A task that never deployed is not a deployment;
          it already appears under Code Tasks. Replaced by actual deployments, with their steps
          and logs one click away. */}
      <RecentDeployments deployments={deployments?.deployments} />
    </div>
  );
}

function Step({ label, value, sub, color, href }: {
  label: string; value: string; sub?: string; color?: string; href?: string;
}) {
  const body = (
    <span style={{ color: color ?? undefined, fontWeight: 500 }}>{value}</span>
  );
  return (
    <span style={{ display: "grid", gap: 2, minWidth: 116 }}>
      <span style={{ opacity: .55, fontSize: 11, fontFamily: "monospace" }}>{label}</span>
      {href ? <a href={href} target="_blank" rel="noreferrer">{body}</a> : body}
      {sub && <span className="detail" style={{ fontSize: 11 }}>{sub}</span>}
    </span>
  );
}

const Arrow = () => <span style={{ opacity: .3 }}>→</span>;

function Fact({ label, value, mono, href }: {
  label: string; value: string; mono?: boolean; href?: string;
}) {
  const body = (
    <span style={{ fontFamily: mono ? "monospace" : undefined }}>{value}</span>
  );
  return (
    <span style={{ display: "grid", gap: 2 }}>
      <span style={{ opacity: .55, fontSize: 11, fontFamily: "monospace" }}>{label}</span>
      {href ? <a href={href} target="_blank" rel="noreferrer">{body}</a> : body}
    </span>
  );
}
