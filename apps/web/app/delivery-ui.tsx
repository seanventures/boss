"use client";

/**
 * CI/CD pipeline view — code task → PR → deployment → health.
 *
 * Ordered as delivery actually flows, so a reader can see where a change STOPPED. BOSS had
 * the whole delivery apparatus (rollout engine, promotion, telemetry, nine tables) and every
 * table was empty because nothing observed a merge; this panel exists to make that kind of
 * gap visible rather than invisible.
 *
 * Two deliberate choices:
 *   - when no deployment target is configured it says SO, rather than rendering an empty
 *     table that reads like a failure;
 *   - a stage with no deployment shows "—", never a fabricated "pending", because a merge
 *     that triggered nothing is a real and common state.
 */

import { useState, useTransition } from "react";

import { executeDeploymentAction, redeployAction } from "./actions";
import { MonitoringPanel, OnboardWizard, SecretsPanel, TargetControls,
         type Observability, type SecretsView } from "./delivery-manage";

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
function liveUrl(s: Stage): string | null {
  const checked = s.health_check_results?.checks?.find((c) => c.url)?.url;
  if (checked) return checked;
  const domain = (s.target_domains || [])[0];
  return domain ? `http://${domain}/` : null;
}

/** Deploy controls. Kept beside the status so "it failed" and "run it again" are one glance apart. */
function DeployActions({ stage }: { stage: Stage }) {
  const [pending, start] = useTransition();
  const [result, setResult] = useState<string | null>(null);
  const id = stage.deployment_run_id;
  if (!id) return null;

  const run = (fn: (i: string) => Promise<any>, label: string) => () =>
    start(async () => {
      setResult(`${label}…`);
      const out = await fn(id);
      setResult(out?.error ? `${label} failed: ${String(out.error).slice(0, 140)}`
        : `${label}: ${out?.status ?? "done"}${out?.healthy === false ? " (unhealthy)" : ""}`);
    });

  const btn: React.CSSProperties = {
    fontSize: 12, padding: "3px 10px", borderRadius: 6, cursor: pending ? "wait" : "pointer",
    border: "1px solid rgba(255,255,255,.18)", background: "transparent", color: "inherit",
  };

  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 10, flexWrap: "wrap" }}>
      <button style={btn} disabled={pending} onClick={run(redeployAction, "Redeploy")}
              title="Ship this target again as a NEW deployment run, keeping this run's history">
        Redeploy
      </button>
      {/* Retrying the same row only makes sense for one that did not succeed. */}
      {stage.deploy_status !== "succeeded" && (
        <button style={btn} disabled={pending} onClick={run(executeDeploymentAction, "Retry")}
                title="Retry THIS deployment run — for one that failed or was blocked">
          Retry this run
        </button>
      )}
      {result && <span className="detail" style={{ fontSize: 12 }}>{result}</span>}
    </div>
  );
}

type HealthTarget = {
  target_id: string; app_name: string; environment: string; url: string | null;
  status: "up" | "degraded" | "down" | "unknown"; status_since: string | null;
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
  summary: { up: number; degraded: number; down: number; unknown: number; overall: string; total: number };
  error?: string;
};

const HEALTH_TONE: Record<string, string> = {
  up: "#3fb950", degraded: "#d29922", down: "#f85149", unknown: "#8b949e",
};

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
                <span style={{ color: HEALTH_TONE[t.status], fontWeight: 600 }}>● {t.status}</span>
                <strong style={{ fontSize: 13 }}>{t.app_name}</strong>
                <span className="cap-badge tone-blue">{t.environment}</span>
              </div>
              {t.url && (
                <a href={`http://${t.url}/`} target="_blank" rel="noreferrer"
                   style={{ fontSize: 12, fontFamily: "monospace" }}>{t.url} ↗</a>
              )}
              <span className="detail" style={{ fontSize: 11 }}>
                {t.status_since ? `${t.status} for ${since(t.status_since)}` : "never probed"}
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

export function DeliveryPipeline({ initial, health, secrets, observability }: {
  initial?: Pipeline | null; health?: Health | null; secrets?: SecretsView | null;
  observability?: Observability | null;
}) {
  const data = initial;

  if (!data) return <div className="detail">Loading delivery pipeline…</div>;
  if (data.error) {
    return <div className="detail" style={{ color: "#f85149" }}>Delivery: {data.error}</div>;
  }

  return (
    <div style={{ display: "grid", gap: 18 }}>
      {/* Health first: "is it working right now" outranks "did the deploy succeed". */}
      <DeploymentHealth health={health} />

      {/* Monitoring sits with delivery on purpose: "what did I ship" and "how is it doing"
          are the same question ten minutes apart. */}
      <MonitoringPanel initial={observability}
                       hostTargetId={data.targets.find((t) => t.status === "active")?.id} />

      {/* Onboarding and credentials: the steps that used to be SQL and ssh-keygen. */}
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
                    <TargetControls target={t} />
                  </td>
                </tr>
                </>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {/* ---- the pipeline itself, in delivery order ---- */}
      <section className="ct-block">
        <div className="ct-block-title">Pipeline — code task → PR → deployment</div>
        <div style={{ display: "grid", gap: 10 }}>
          {data.stages.length === 0 && (
            <div className="detail">No code task has opened a PR yet.</div>
          )}
          {data.stages.map((s) => (
            <div key={s.run_id} style={{
              border: "1px solid rgba(255,255,255,.09)", borderRadius: 8, padding: 12,
            }}>
              <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                <span className={`cap-badge tone-${s.run_status === "completed" ? "green" : "muted"}`}>
                  {s.run_status}
                </span>
                <strong style={{ fontSize: 13 }}>{s.title?.slice(0, 66)}</strong>
              </div>

              <div style={{ display: "flex", gap: 16, marginTop: 10, flexWrap: "wrap",
                            alignItems: "center", fontSize: 13 }}>
                <Step label="code task" value={s.run_id.slice(0, 20)} />
                <Arrow />
                <Step
                  label="pull request"
                  value={s.pull_request_number ? `#${s.pull_request_number}` : "—"}
                  href={s.pull_request_url ?? undefined}
                />
                <Arrow />
                {/* A merge that triggered no deployment is a real state, shown as "—" rather
                    than an invented "pending". */}
                <Step
                  label="deployment"
                  value={s.deploy_status ?? "—"}
                  color={tone(s.deploy_status)}
                  sub={s.environment ? `${s.environment} · ${s.target_kind ?? ""}` : undefined}
                />
                <Arrow />
                <Step
                  label="health"
                  value={s.finished_at ? (s.failure_reason ? "failed" : "ok") : "—"}
                  color={s.failure_reason ? "#f85149" : s.finished_at ? "#3fb950" : "#8b949e"}
                />
              </div>

              {/* What is running, which build, since when — the questions a status word alone
                  cannot answer. Absent facts render as "—" rather than being omitted, so the
                  gap is visible instead of looking like it was never asked. */}
              {s.deployment_run_id && (
                <div style={{
                  display: "flex", gap: 18, marginTop: 10, flexWrap: "wrap", fontSize: 12,
                  borderTop: "1px solid rgba(255,255,255,.07)", paddingTop: 10,
                }}>
                  <Fact label="build" value={s.commit_ref ? s.commit_ref.slice(0, 12) : "—"} mono />
                  <Fact label="deployed" value={s.finished_at ? `${since(s.finished_at)} ago` : "—"} />
                  <Fact label="took" value={took(s.started_at, s.finished_at) || "—"} />
                  <Fact
                    label="serving"
                    value={liveUrl(s) ? "open ↗" : "—"}
                    href={liveUrl(s) ?? undefined}
                  />
                  <Fact label="deployment" value={s.deployment_run_id.slice(0, 18)} mono />
                </div>
              )}

              {s.deployment_run_id && <DeployActions stage={s} />}

              {s.failure_reason && (
                <div className="detail" style={{ color: "#f85149", marginTop: 8 }}>
                  {s.failure_reason}
                </div>
              )}
            </div>
          ))}
        </div>
      </section>
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
