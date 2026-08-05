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

export function DeliveryPipeline({ initial }: { initial?: Pipeline | null }) {
  const data = initial;

  if (!data) return <div className="detail">Loading delivery pipeline…</div>;
  if (data.error) {
    return <div className="detail" style={{ color: "#f85149" }}>Delivery: {data.error}</div>;
  }

  return (
    <div style={{ display: "grid", gap: 18 }}>
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
                <tr key={t.id} style={{ borderTop: "1px solid rgba(255,255,255,.07)" }}>
                  <td>{t.app_name}</td>
                  <td><span className="cap-badge tone-blue">{t.environment}</span></td>
                  <td className="detail">{t.repo}</td>
                  <td>{t.target_kind}</td>
                  <td className="detail">{t.target_ref}</td>
                  <td style={{ color: t.status === "active" ? "#3fb950" : "#8b949e" }}>{t.status}</td>
                </tr>
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
