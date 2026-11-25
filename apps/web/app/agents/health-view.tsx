"use client";

/**
 * AGENTS › HEALTH (A5, BOSSAPP-1054): alerts, deployment health + pause/resume, D7 deterministic
 * system services, worker scaling & bot clock tick.
 */
import React, { useCallback, useEffect, useState } from "react";
import { Pause, Play, Pulse, Warning } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { ReadFailure } from "../../components/locked-panel";
import {
  readHealth, readWorkerScaling, setDeploymentPaused, tickBots,
  type PlatformHealthData, type ReadResult, type WorkerScalingData,
} from "../../lib/agent-platform";
import { formatRelativeTime } from "../../lib/datetime";
import { usd } from "../../lib/money";
import { useNow } from "./live-util";
import "./agent-platform.css";

function openRunTrace(runId: string) {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set("area", "workforce");
    url.searchParams.set("view", "activity");
    url.searchParams.set("run", runId);
    window.history.pushState(null, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  } catch { /* non-browser */ }
}

export function HealthView({ projectId, actor }: { projectId: string; actor: string }) {
  const now = useNow(30_000);
  const [hours, setHours] = useState<24 | 168>(24);
  const [health, setHealth] = useState<ReadResult<PlatformHealthData> | null>(null);
  const [scaling, setScaling] = useState<ReadResult<WorkerScalingData> | null>(null);
  const [busy, setBusy] = useState("");
  const [said, setSaid] = useState<{ text: string; ok: boolean } | null>(null);

  const load = useCallback(() => {
    void readHealth(projectId, hours).then(setHealth);
    void readWorkerScaling(projectId).then(setScaling);
  }, [projectId, hours]);

  useEffect(() => { load(); }, [load]);

  const togglePause = async (roleKey: string, env: string, paused: boolean) => {
    setBusy(`${roleKey}:${env}`); setSaid(null);
    const res = await setDeploymentPaused(projectId, actor, roleKey, env, !paused, paused ? "" : "Paused from Health console");
    setBusy("");
    setSaid({ ok: res.ok, text: res.ok ? `${roleKey} (${env}) ${paused ? "resumed" : "paused"}.` : res.error });
    if (res.ok) load();
  };

  const runClock = async () => {
    setBusy("clock"); setSaid(null);
    const res = await tickBots(projectId, 50);
    setBusy("");
    if (res.ok) {
      const fired = Array.isArray(res.data.fired) ? res.data.fired.length : 0;
      setSaid({ ok: true, text: `Bot clock ticked (${fired} fired).` });
      load();
    } else {
      setSaid({ ok: false, text: res.error });
    }
  };

  const data = health?.ok ? health.data : null;
  const sc = scaling?.ok ? scaling.data : null;

  return (
    <div className="ap-wrap" data-testid="ap-health-view">
      <div className="ap-head">
        <div>
          <h2 className="ap-title">Agent platform health</h2>
          <p className="ap-sub">Deployment reliability, system daemons (D7), and worker capacity.</p>
        </div>
        <span className="ap-grow" />
        <button type="button" className="ap-chip-btn" aria-pressed={hours === 24} onClick={() => setHours(24)} data-testid="ap-health-24h">24h</button>
        <button type="button" className="ap-chip-btn" aria-pressed={hours === 168} onClick={() => setHours(168)} data-testid="ap-health-7d">7d</button>
        <GatedButton permission="system:operate" projectId={projectId} className="ap-btn" disabled={busy === "clock"}
                     onClick={() => void runClock()} data-testid="ap-bot-clock-btn">
          <Pulse size={14} aria-hidden /> {busy === "clock" ? "Running…" : "Run bot clock"}
        </GatedButton>
      </div>

      {said ? <p className={said.ok ? "ap-ok" : "ap-err"} role="status" data-testid="ap-health-said">{said.text}</p> : null}

      {health && !health.ok ? (
        <ReadFailure problem={{ status: health.status, text: health.error }} onRetry={load} testId="ap-health-err" />
      ) : null}

      {data ? (
        <>
          {data.alerts.map((al) => (
            <div key={`${al.deployment_id}-${al.since}`} className="ap-alert" data-testid={`ap-alert-${al.agent_key}`}>
              <Warning size={16} aria-hidden />
              <div>
                <b>{al.title}</b> <span className="ap-sub">· {al.detail}</span>
              </div>
              <span className="ap-grow" />
              {al.trace_run_id ? (
                <button type="button" className="ap-link-btn" onClick={() => openRunTrace(al.trace_run_id!)}>
                  View run trace →
                </button>
              ) : null}
            </div>
          ))}

          <section className="ap-card" aria-labelledby="ap-hd-h">
            <h3 id="ap-hd-h" className="ap-k">Deployments health</h3>
            {!data.deployments.length ? (
              <p className="ap-sub">No deployed agents in this project yet.</p>
            ) : (
              <div className="ap-table-wrap">
                <table className="ap-table" data-testid="ap-health-deployments">
                  <thead>
                    <tr><th>Deployment</th><th>State</th><th>Runs</th><th>OK / p95</th><th>Cost</th><th>Control</th></tr>
                  </thead>
                  <tbody>
                    {data.deployments.map((d) => (
                      <tr key={d.deployment_id} data-testid={`ap-hrow-${d.agent_key}-${d.environment}`}>
                        <td>
                          <b>{d.name}</b> <span className="ap-pill">{d.environment} v{d.version}</span>
                          {d.last_failure ? <div className="ap-sub">{d.last_failure.cause}: {d.last_failure.error}</div> : null}
                        </td>
                        <td>
                          <span className="ap-pill" data-tone={d.state === "healthy" ? "green" : d.state === "failing" || d.paused ? "red" : d.state === "stuck" ? "amber" : undefined}>
                            {d.paused ? "paused" : d.label || d.state}
                          </span>
                        </td>
                        <td className="ap-mono">{d.runs}{d.stuck_runs ? ` (${d.stuck_runs} stuck)` : ""}</td>
                        <td className="ap-mono">{d.ok_rate !== null ? `${Math.round(d.ok_rate * 100)}%` : "—"} · {d.p95_ms !== null ? `${Math.round(d.p95_ms)}ms` : "—"}</td>
                        <td className="ap-mono">{usd(d.cost_usd)}</td>
                        <td>
                          <GatedButton permission="agent:lifecycle" projectId={projectId} className="ap-btn"
                                       disabled={busy === `${d.agent_key}:${d.environment}`}
                                       onClick={() => void togglePause(d.agent_key, d.environment, d.paused)}
                                       data-testid={`ap-dep-pause-${d.agent_key}-${d.environment}`}>
                            {d.paused ? <><Play size={13} aria-hidden /> Resume</> : <><Pause size={13} aria-hidden /> Pause</>}
                          </GatedButton>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <div className="ap-grid2">
            <section className="ap-card" aria-labelledby="ap-sys-h" data-testid="ap-system-services">
              <h3 id="ap-sys-h" className="ap-k">System services (deterministic daemons · D7)</h3>
              <div className="ap-table-wrap">
                <table className="ap-table">
                  <thead><tr><th>Service</th><th>State</th><th>Last beat</th><th>Host</th></tr></thead>
                  <tbody>
                    {data.system_services.map((s) => (
                      <tr key={s.key}>
                        <td><b>{s.name}</b><div className="ap-mono ap-sub">{s.key}</div></td>
                        <td><span className="ap-pill" data-tone={s.state === "running" ? "green" : s.state === "failing" ? "red" : "amber"}>{s.state}</span></td>
                        <td className="ap-sub">{s.last_beat_at && now ? formatRelativeTime(s.last_beat_at, now, { empty: "—" }) : "—"}</td>
                        <td className="ap-mono">{s.host || s.runs_in}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>

            <section className="ap-card" aria-labelledby="ap-wrk-h" data-testid="ap-workers-scaling">
              <h3 id="ap-wrk-h" className="ap-k">Workers &amp; scaling signal</h3>
              {scaling && !scaling.ok ? (
                <ReadFailure problem={{ status: scaling.status, text: scaling.error }} onRetry={load} testId="ap-scaling-err" />
              ) : (
                <>
                  <div className="ap-row" style={{ marginBottom: "0.5rem" }}>
                    <span className="ap-pill" data-tone={(sc?.live_workers ?? data.workers.live_workers) > 0 ? "green" : "amber"}>
                      {sc?.live_workers ?? data.workers.live_workers} live workers
                    </span>
                    <span className="ap-pill">
                      {sc?.slots ?? data.workers.slots} slots · {sc?.busy_slots ?? data.workers.busy_slots} busy · {sc?.free_slots ?? data.workers.free_slots} free
                    </span>
                    <span className="ap-pill">{sc?.queued_runs ?? data.workers.queued_runs} queued</span>
                  </div>
                  <p className="ap-sub">{sc?.words ?? data.workers.words}</p>
                </>
              )}
            </section>
          </div>
        </>
      ) : null}
    </div>
  );
}
