"use client";

/**
 * AGENT PASSPORT › DEPLOYMENTS (A3, BOSSAPP-1054): dev/staging/prod environment cards,
 * draft deploy (dev/staging only; prod via promotion + Decision), promotion checks, rollback,
 * versions & history.
 */
import React, { useCallback, useEffect, useState } from "react";
import { ArrowCounterClockwise, ArrowUpRight, CheckCircle, RocketLaunch, WarningCircle } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { ReadFailure } from "../../components/locked-panel";
import {
  deployVersion, promoteVersion, readAgentDeployments, readPromotionChecks, rollbackDeployment,
  type AgentDeploymentsData, type PromotionChecksData, type ReadResult,
} from "../../lib/agent-platform";
import { formatDate } from "../../lib/datetime";
import { usd } from "../../lib/money";
import "./agent-platform.css";

export function PassportDeployments({ projectId, actor, roleKey }: {
  projectId: string; actor: string; roleKey: string;
}) {
  const [read, setRead] = useState<ReadResult<AgentDeploymentsData> | null>(null);
  const [checks, setChecks] = useState<PromotionChecksData | null>(null);
  const [busy, setBusy] = useState("");
  const [said, setSaid] = useState<{ text: string; ok: boolean; decisionId?: string } | null>(null);

  const load = useCallback(() => {
    void readAgentDeployments(projectId, roleKey).then(setRead);
  }, [projectId, roleKey]);

  useEffect(() => { load(); }, [load]);

  const deploy = async (env: "dev" | "staging", version: number) => {
    setBusy(`dep:${env}`); setSaid(null);
    const res = await deployVersion(projectId, actor, roleKey, { environment: env, version });
    setBusy("");
    setSaid({ ok: res.ok, text: res.ok ? `Deployed v${version} to ${env}.` : res.error });
    if (res.ok) load();
  };

  const inspectPromotion = async (fromEnv: string, toEnv: string) => {
    setBusy(`chk:${toEnv}`); setSaid(null);
    const res = await readPromotionChecks(projectId, roleKey, { from_environment: fromEnv, to_environment: toEnv });
    setBusy("");
    if (res.ok) setChecks(res.data);
    else setSaid({ ok: false, text: res.error });
  };

  const promote = async (fromEnv: string, toEnv: string, version?: number) => {
    setBusy(`pro:${toEnv}`); setSaid(null);
    const res = await promoteVersion(projectId, actor, roleKey, { from_environment: fromEnv, to_environment: toEnv, version });
    setBusy("");
    if (!res.ok) { setSaid({ ok: false, text: res.error }); return; }
    if (res.data.status === "awaiting_approval") {
      setSaid({
        ok: true,
        text: `Production promotion requested (Decision ${res.data.decision_id}).`,
        decisionId: res.data.decision_id,
      });
    } else {
      setSaid({ ok: true, text: `Promoted to ${toEnv}.` });
    }
    setChecks(null);
    load();
  };

  const rollback = async (env: string, toVersion?: number) => {
    setBusy(`rb:${env}`); setSaid(null);
    const res = await rollbackDeployment(projectId, actor, roleKey, { environment: env, to_version: toVersion });
    setBusy("");
    setSaid({ ok: res.ok, text: res.ok ? `Rolled back ${env}${toVersion ? ` to v${toVersion}` : ""}.` : res.error });
    if (res.ok) load();
  };

  const data = read?.ok ? read.data : null;

  return (
    <section className="ct-block ap-wrap" id="ag-sec-deployments" data-testid="ap-passport-deployments">
      <div className="ap-head">
        <h2 className="ap-title">Deployments</h2>
        <span className="ap-grow" />
        {data?.draft ? (
          <div className="ap-row" data-testid="ap-draft-banner">
            <span className="ap-pill" data-tone="amber">Draft v{data.draft.version}</span>
            <GatedButton permission="agent:deploy" projectId={projectId} className="ap-btn"
                         disabled={busy === "dep:dev"} onClick={() => void deploy("dev", data.draft!.version)}
                         data-testid="ap-deploy-dev">
              <RocketLaunch size={13} aria-hidden /> Deploy v{data.draft.version} to dev
            </GatedButton>
            <GatedButton permission="agent:deploy" projectId={projectId} className="ap-btn" data-kind="primary"
                         disabled={busy === "dep:staging"} onClick={() => void deploy("staging", data.draft!.version)}
                         data-testid="ap-deploy-staging">
              Deploy v{data.draft.version} to staging
            </GatedButton>
          </div>
        ) : null}
      </div>

      {said ? (
        <p className={said.ok ? "ap-ok" : "ap-err"} role="status" data-testid="ap-deployments-said">
          {said.text}
          {said.decisionId ? (
            <> <a href={`?area=decisions&decision=${encodeURIComponent(said.decisionId)}`}>Open Decision inbox →</a></>
          ) : null}
        </p>
      ) : null}

      {read && !read.ok ? (
        <ReadFailure problem={{ status: read.status, text: read.error }} onRetry={load} testId="ap-deployments-err" />
      ) : null}

      {data ? (
        <>
          <div className="ap-grid3">
            {(data.environments ?? []).map((e) => (
              <div key={e.environment} className="ap-card" data-testid={`ap-env-${e.environment}`}>
                <div className="ap-row">
                  <span className="ap-pill">{e.environment.toUpperCase()}</span>
                  <span className="ap-grow" />
                  <span className="ap-pill" data-tone={e.deployed ? "green" : undefined}>
                    {e.deployed ? `v${e.version} live` : "Not deployed"}
                  </span>
                </div>
                {e.deployed ? (
                  <div className="ap-wrap" style={{ marginTop: "0.5rem", gap: "0.35rem" }}>
                    <div className="ap-sub ap-mono">
                      Host: {e.host?.label || "local"} · {e.runs_7d ?? 0} runs (7d) · {usd(e.cost_7d_usd ?? 0)}
                    </div>
                    {e.in_flight ? <div className="ap-sub">{e.in_flight} run(s) in flight pinned to v{e.version}</div> : null}
                    <div className="ap-row">
                      {e.environment !== "prod" ? (
                        <button type="button" className="ap-btn"
                                onClick={() => void inspectPromotion(e.environment, e.environment === "dev" ? "staging" : "prod")}
                                data-testid={`ap-check-promote-${e.environment}`}>
                          <ArrowUpRight size={13} aria-hidden /> Check &amp; promote → {e.environment === "dev" ? "staging" : "prod"}
                        </button>
                      ) : null}
                      {e.previous_version ? (
                        <GatedButton permission="agent:deploy" projectId={projectId} className="ap-btn" data-kind="danger"
                                     disabled={busy === `rb:${e.environment}`}
                                     onClick={() => void rollback(e.environment, e.previous_version!)}
                                     data-testid={`ap-rollback-${e.environment}`}>
                          <ArrowCounterClockwise size={13} aria-hidden /> Rollback → v{e.previous_version}
                        </GatedButton>
                      ) : null}
                    </div>
                  </div>
                ) : (
                  <p className="ap-sub" style={{ marginTop: "0.5rem" }}>
                    {e.environment === "prod" ? "Production accepts versions via promotion from staging (with approval)." : "Deploy a draft version above."}
                  </p>
                )}
              </div>
            ))}
          </div>

          {checks ? (
            <div className="ap-card" data-testid="ap-promotion-checks">
              <div className="ap-row">
                <h3 className="ap-title">Promotion checks: {checks.from_environment} v{checks.version} → {checks.to_environment}</h3>
                <span className="ap-grow" />
                {checks.approval.required ? <span className="ap-pill" data-tone="amber">Approval required</span> : null}
              </div>
              <div className="ap-grid3" style={{ marginTop: "0.5rem" }}>
                {checks.checks.map((c) => (
                  <div key={c.key} className="ap-subcard">
                    <div className="ap-row">
                      {c.ok === false ? <WarningCircle size={14} aria-hidden /> : <CheckCircle size={14} aria-hidden />}
                      <b>{c.label}</b>
                    </div>
                    <div className="ap-sub">{c.detail}</div>
                  </div>
                ))}
              </div>
              <div className="ap-row" style={{ marginTop: "0.65rem" }}>
                <span className="ap-sub">{checks.in_flight.note}</span>
                <span className="ap-grow" />
                <button type="button" className="ap-btn" data-kind="ghost" onClick={() => setChecks(null)}>Close</button>
                <GatedButton permission="agent:deploy" projectId={projectId} className="ap-btn" data-kind="primary"
                             disabled={!checks.can_promote || busy === `pro:${checks.to_environment}`}
                             onClick={() => void promote(checks.from_environment, checks.to_environment, checks.version)}
                             data-testid="ap-promote-confirm">
                  {checks.to_environment === "prod" ? "Request prod promotion" : `Promote → ${checks.to_environment}`}
                </GatedButton>
              </div>
            </div>
          ) : null}

          {(data.history ?? []).length ? (
            <div className="ap-card">
              <h3 className="ap-k">Deployment history</h3>
              <div className="ap-table-wrap">
                <table className="ap-table" data-testid="ap-deployment-history">
                  <thead><tr><th>When</th><th>Env</th><th>Action</th><th>Version</th><th>Status</th><th>Actor</th></tr></thead>
                  <tbody>
                    {data.history.map((h) => (
                      <tr key={h.id}>
                        <td className="ap-sub">{formatDate(h.created_at, "—")}</td>
                        <td>{h.environment}</td>
                        <td>{h.action}</td>
                        <td className="ap-mono">{h.from_version ? `v${h.from_version} → ` : ""}v{h.to_version}</td>
                        <td><span className="ap-pill" data-tone={h.status === "applied" ? "green" : "amber"}>{h.status}</span></td>
                        <td className="ap-sub">{h.actor}{h.decision_id ? ` (${h.decision_id})` : ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
