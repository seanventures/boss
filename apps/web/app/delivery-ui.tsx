"use client";

import { Spine, type SpineNode } from "./journey-spine-ui";

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

import React, { useCallback, useEffect, useState } from "react";

import { formatDateTime } from "../lib/datetime";
import { getDeliveryPipelineAction } from "./actions";
import {
  FleetPanel, OnboardWizard, SecretsPanel, TargetControls, type SecretsView,
} from "./delivery-manage";
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
  /** Read by `deployedUrl` so a link and its health probe cannot disagree about the scheme. */
  health_checks?: Array<{ scheme?: string; path?: string }> | null;
};
export type Pipeline = {
  project_id: string; stages: Stage[]; targets: Target[]; configured: boolean;
  /** From BOSS_ENABLE_SANDBOX_DEPLOY — fail-closed when false/undefined. */
  sandbox_deploy_enabled?: boolean;
  sandbox_merge_enabled?: boolean;
  error?: string;
};

/**
 * The URL a deployed app is actually reachable on.
 *
 * The scheme is READ, never assumed. This file hardcoded `https://` while `deploy-ui.tsx`
 * hardcoded `http://` — two files with two opinions about one address, so the console offered
 * a TLS link to a plain-HTTP app and the click failed with a protocol error that looked like
 * the deployment was broken. The target already declares its scheme for the health probe;
 * the link uses the same answer.
 */
export function deployedUrl(
  domain?: string | null,
  target?: { health_checks?: Array<{ scheme?: string }> | null } | null,
): string | undefined {
  if (!domain) return undefined;
  if (/^https?:\/\//i.test(domain)) return domain;
  const declared = target?.health_checks?.[0]?.scheme;
  // No declaration: a bare host:port is a plain address, not a certificate. Guessing https
  // there is the guess that produced this bug.
  const scheme = declared || (/:\d+$/.test(domain) ? "http" : "https");
  return `${scheme}://${domain}`;
}

const tone = (s: string | null) => {
  if (!s) return "#8b949e";
  if (["succeeded", "deployed", "healthy"].includes(s)) return "#3fb950";
  if (["failed", "rolled_back", "unhealthy"].includes(s)) return "#f85149";
  if (["created", "running", "in_progress"].includes(s)) return "#d29922";
  return "#8b949e";
};

/** Absolute UTC timestamp for deploy/check facts (hydration-safe). */
function sinceLabel(iso: string | null): string {
  if (!iso) return "—";
  return formatDateTime(iso, "—");
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

/** True when there is nothing to probe — empty registry, not an outage. */
export function isHealthUnconfigured(health: Health | null | undefined): boolean {
  if (!health || health.error) return false;
  return (health.targets?.length ?? 0) === 0 || (health.summary?.total ?? 0) === 0;
}

export function DeploymentHealth({ health }: { health?: Health | null }) {
  if (!health) return null;
  if (health.error) {
    return <div className="detail" style={{ color: "#f85149" }}>Health: {health.error}</div>;
  }
  const { summary, targets } = health;

  // Zero targets is not a fleet status. Rendering "● not_deployed · 0 up · 0 down" looks
  // like an operations dashboard for a running system; it is not — nothing is registered.
  if (isHealthUnconfigured(health)) {
    return (
      <section className="ct-block" data-empty-state="no-health-targets">
        <div className="ct-block-title">Deployment health</div>
        <p className="empty-state" style={{ padding: "18px 0" }}>
          Nothing is registered to monitor. Health samples appear after you onboard a
          deployment target in CI/CD and ship an app to it.
        </p>
      </section>
    );
  }

  return (
    <section className="ct-block">
      <div className="ct-block-title">
        Health of deployed apps{" "}
        <span style={{ color: HEALTH_TONE[summary.overall], fontSize: 12 }}>
          ● {HEALTH_LABEL[summary.overall] ?? summary.overall}
        </span>
        <span className="detail" style={{ fontSize: 12, marginLeft: 8 }}>
          {summary.up} up · {summary.degraded} degraded · {summary.down} down
          {summary.unknown ? ` · ${summary.unknown} unknown` : ""}
          {summary.not_deployed ? ` · ${summary.not_deployed} never deployed` : ""}
        </span>
      </div>

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
                  : t.status_since ? `${t.status} since ${sinceLabel(t.status_since)}` : "never probed"}
              </span>
            </div>

            <div style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 12 }}>
              {/* null uptime is "no data", never 0% — an unprobed target has not had an outage. */}
              <Fact label="uptime 24h" value={t.uptime_24h == null ? "no data" : `${t.uptime_24h}%`} />
              <Fact label="uptime 7d" value={t.uptime_7d == null ? "no data" : `${t.uptime_7d}%`} />
              <Fact label="latency" value={t.last_latency_ms == null ? "—" : `${t.last_latency_ms}ms`} />
              <Fact label="p95" value={t.p95_ms == null ? "—" : `${t.p95_ms}ms`} />
              <Fact label="build" value={t.deployed_commit ? t.deployed_commit.slice(0, 12) : "—"} mono />
              <Fact label="deployed" value={sinceLabel(t.deployed_at)} />
              <Fact label="checked" value={sinceLabel(t.last_checked)} />
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
                    {formatDateTime(i.started_at, "—")} ·{" "}
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

/** Fail-closed sandbox auto-deploy notice — only when the flag is known to be off. */
export function SandboxDeployFlagNotice({ enabled }: { enabled?: boolean }) {
  if (enabled !== false) return null;
  return (
    <p className="detail" data-testid="sandbox-deploy-disabled"
       style={{ color: "#d29922", margin: 0 }}>
      Automatic sandbox deploy is off (
      <code>BOSS_ENABLE_SANDBOX_DEPLOY=false</code>
      ). That is intentional fail-closed configuration — merge-triggered sandbox
      deploys will not run until the flag is enabled. Manual onboarding of a
      target and host still works when you choose to set one up.
    </p>
  );
}


/**
 * One card per deployable app, on the same card as everything else in the console.
 *
 * Delivery was a TABLE: app / environment / repo / kind / ref / status, with the controls
 * folded into a second row underneath each entry. It answered "what is registered" and left
 * "what happened to it" to a different section further down, so telling whether an app was
 * actually live meant reading two places and joining them by name.
 *
 * The card says the same things in the order they are asked: what this app is, how far the
 * last change got, what you can do about it, and where it lives.
 */
function DeliveryTargetCard({ target, stage, shipped, onChanged }: {
  target: Target;
  stage?: Stage;
  shipped: boolean;
  onChanged: () => void;
}) {
  // A stage only exists for a deployment that came from a code task with a PR. An operator
  // pressing Deploy creates a deployment_run and no stage, so this read null and the card
  // said "never deployed" about an app answering 200 on the host. The target's own history is
  // the truth about whether it has ever deployed; the stage is only richer when it exists.
  const last = (target as { last_deployment?: {
    status?: string; finished_at?: string; health_check_results?: { healthy?: boolean };
  } | null }).last_deployment ?? null;
  const deploy = stage?.deploy_status ?? last?.status ?? null;
  const healthy = stage?.health_check_results?.healthy ?? last?.health_check_results?.healthy;
  const domain = (target.domains && target.domains[0]) || (stage?.target_domains || [])[0];

  const done = (ok: boolean, now = false): "done" | "now" | "todo" | "blocked" =>
    ok ? "done" : now ? "now" : "todo";

  const nodes: SpineNode[] = [
    {
      key: "change",
      label: "Change",
      sub: stage?.pull_request_number ? `PR #${stage.pull_request_number} ↗` : "none yet",
      state: done(Boolean(stage?.pull_request_url)),
      href: stage?.pull_request_url ?? undefined,
      title: stage?.pull_request_url
        ? "The pull request this deployment came from"
        : "Nothing has been proposed for this app yet",
    },
    {
      key: "merge",
      label: "Merged",
      sub: stage?.commit_ref ? stage.commit_ref.slice(0, 7) : "not merged",
      state: done(Boolean(stage?.commit_ref)),
      title: stage?.commit_ref
        ? `Merged as ${stage.commit_ref}`
        : "The change has not been merged, so there is nothing to deploy",
    },
    {
      key: "deploy",
      label: "Deploy",
      // A deploy that FAILED must not read the same as one that never ran: the table showed
      // both as an empty cell, which is how a broken app looked untouched.
      sub: deploy ?? "never",
      state: deploy === "succeeded" || deploy === "deployed"
        ? "done"
        : deploy === "failed" || deploy === "rolled_back"
          ? "blocked"
          : deploy ? "now" : "todo",
      title: stage?.failure_reason
        ? `Deploy failed: ${stage.failure_reason}`
        : deploy
          ? `Last deploy ${deploy}${stage?.finished_at ? ` — took ${took(stage.started_at, stage.finished_at)}` : ""}`
          : "This app has never been deployed",
    },
    {
      key: "live",
      label: "Live",
      sub: healthy === true ? "open app ↗" : healthy === false ? "unhealthy" : "not checked",
      state: healthy === true ? "done" : healthy === false ? "blocked" : "todo",
      href: healthy === true ? deployedUrl(domain, target) : undefined,
      title: healthy === true
        ? "Health checks passed — open the running app"
        : healthy === false
          ? "The app is deployed but its health check is failing"
          : "No health check has run for this app",
    },
  ];

  return (
    <article className="mr-meeting-card">
      <div className="mr-meeting-card-head">
        <span className="cap-badge tone-blue">{target.environment}</span>
        <span className={`cap-badge tone-${
          healthy === true ? "green" : deploy === "failed" ? "amber" : "blue"}`}>
          {healthy === true ? "live" : deploy === "failed" ? "deploy failed"
            : deploy ? deploy : "never deployed"}
        </span>
        <span className="mr-meeting-date">
          {stage?.finished_at ? sinceLabel(stage.finished_at)
            : last?.finished_at ? sinceLabel(last.finished_at) : target.status}
        </span>
      </div>
      <span className="mr-meeting-title">{target.app_name}</span>
      <p className="detail" style={{ margin: "2px 0 0" }}>
        {target.repo} · {target.target_kind} · {target.target_ref}
        {target.auto_deploy ? " · deploys on merge" : ""}
        {target.paused ? " · paused" : ""}
      </p>
      <Spine nodes={nodes} large label={`Delivery progress for ${target.app_name}`} />
      <div className="ct-actions">
        <TargetControls target={target as never} shipped={shipped} onChanged={onChanged} />
      </div>
      <div className="ct-card-foot">
        <span className="detail">{target.repo_owner ? `${target.repo_owner}/` : ""}{target.repo}</span>
        {domain ? (
          <a className="detail" href={deployedUrl(domain, target)} target="_blank" rel="noreferrer">
            {domain} ↗
          </a>
        ) : <span className="detail">no domain yet</span>}
      </div>
    </article>
  );
}

export function DeliveryPipeline({ initial, secrets, deployments }: {
  initial?: Pipeline | null; secrets?: SecretsView | null; deployments?: any;
}) {
  // The panel owns its data.
  //
  // `router.refresh()` alone was not enough: the server DID re-render (the API log shows the
  // pipeline re-fetched immediately after a delete) but the client kept the old rows, so a
  // purged target sat in the table until someone reloaded the page by hand. Holding the data
  // in state and re-asking after a mutation does not depend on that reconciliation landing.
  const [data, setData] = useState(initial);
  useEffect(() => { setData(initial); }, [initial]);
  const reload = useCallback(async () => {
    const fresh = await getDeliveryPipelineAction();
    if (fresh && !fresh.error) setData(fresh);
  }, []);

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

  // Zero targets: setup path only. Do not render Deploy wizard, empty recent-deployments
  // chrome, or a targets table that reads like a live fleet — that is the "full machinery
  // while live has 0 targets" shell the audit called out.
  if (!data.configured) {
    return (
      <div style={{ display: "grid", gap: 18 }} data-empty-state="no-targets">
        <section className="ct-block">
          <div className="ct-block-title">Nothing deploys yet</div>
          <p className="detail" style={{ marginTop: 0 }}>
            Start with a machine: register a host, let BOSS onboard it, then point an app at
            it. Merged PRs deploy nowhere until an app has a target.
          </p>
          <SandboxDeployFlagNotice enabled={data.sandbox_deploy_enabled} />
        </section>

        {/* THE FLEET BELONGS HERE, and its absence was a deadlock rather than a cosmetic gap.
            The Hosts wizard was added only to the CONFIGURED branch — so it appeared once a
            deployment target already existed. But a target names the host it deploys to, so
            the honest order is host first, app second: on a fresh install (exactly what a new
            org or a new BOSS instance is) the wizard we built for onboarding a VM could not
            be reached at all. An empty state must contain the thing that ends the empty
            state. */}
        <FleetPanel projectId={data.project_id} targets={(data.targets || []) as any} />

        <details className="dm-advanced">
          <summary className="detail">Advanced — credentials, per-app onboarding</summary>
          <div style={{ display: "grid", gap: 18, marginTop: 12 }}>
            <div><OnboardWizard secrets={secrets?.secrets ?? []} /></div>
            <SecretsPanel initial={secrets} />
          </div>
        </details>
      </div>
    );
  }

  return (
    <div style={{ display: "grid", gap: 18 }} data-empty-state="configured">
      <SandboxDeployFlagNotice enabled={data.sandbox_deploy_enabled} />

      {/* THIS SCREEN IS SETUP. The day-to-day loop is not here.
          Once an app is pointed at a verified host, approving a preview deploys it and the
          result appears on the app card — so the only reason to come back is to add a host,
          add an app, or look at what happened. Seven stacked panels made that impossible to
          see: two overlapping onboarding wizards, a raw credential table, and a manual deploy
          form all competed with the two things anyone actually needs. */}
      <FleetPanel projectId={data.project_id} targets={(data.targets || []) as any} />

      {/* ---- targets: one card per app, the console's card ---- */}
      <section className="ct-block">
        <div className="ct-block-title">Apps and where they deploy</div>
        <div className="mr-grid ct-tiles" style={{ marginTop: 10 }}>
          {data.targets.map((t) => (
            <DeliveryTargetCard
              key={t.id}
              target={t}
              // The most recent stage for THIS app. The table left this join to the reader.
              stage={(data.stages || [])
                .filter((st) => st.app_name === t.app_name)
                .sort((a, b) => (b.finished_at || b.started_at || "")
                  .localeCompare(a.finished_at || a.started_at || ""))[0]}
              shipped={everShipped.has(t.app_name)}
              onChanged={reload}
            />
          ))}
        </div>
      </section>

      {/* The pipeline table listed every code task run that had ever opened a PR — eighteen
          rows, nearly all with no deployment. A task that never deployed is not a deployment;
          it already appears under Code Tasks. Replaced by actual deployments, with their steps
          and logs one click away. */}
      <RecentDeployments deployments={deployments?.deployments} />

      {/* Everything below is reachable, not prominent. None of it is deleted: a credential you
          can only inspect through the API is a credential nobody can debug at 2am. But none of
          it belongs in the path of someone shipping a change either. */}
      <details className="dm-advanced">
        <summary className="detail">Advanced — credentials, manual deploy, per-app onboarding</summary>
        <div style={{ display: "grid", gap: 18, marginTop: 12 }}>
          {/* Still the right tool for the FIRST deploy of an app that merged before it had a
              target — BugBox is exactly that. Not the everyday path. */}
          <div><DeployWizard targets={(data.targets || []) as any} /></div>
          <div><OnboardWizard secrets={secrets?.secrets ?? []} /></div>
          <SecretsPanel initial={secrets} />
        </div>
      </details>
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
