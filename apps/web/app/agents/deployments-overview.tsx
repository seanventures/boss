"use client";

/**
 * AGENTS › DEPLOYMENTS (A1, BOSSAPP-1054): live agents, bots, 7d spend, filters, and MCP tools.
 */
import React, { useCallback, useEffect, useState } from "react";
import { Plus, Robot, Wrench } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { ReadFailure } from "../../components/locked-panel";
import {
  readMcpTools, readOverview, type McpTool, type OverviewData, type ReadResult,
} from "../../lib/agent-platform";
import { formatRelativeTime } from "../../lib/datetime";
import { usd } from "../../lib/money";
import { openAgent } from "../../lib/agent-world";
import { useNow } from "./live-util";
import "./agent-platform.css";

function openPassportTab(roleKey: string, tab: "deployments" | "triggers" | "endpoints") {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set("area", "workforce");
    url.searchParams.set("role", roleKey);
    url.searchParams.set("tab", tab);
    window.history.pushState(null, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  } catch {
    openAgent(roleKey);
  }
}

export function DeploymentsOverview({ projectId, actor: _actor, onCreateBot }: {
  projectId: string;
  actor: string;
  onCreateBot?: () => void;
}) {
  const now = useNow(30_000);
  const [kind, setKind] = useState<"" | "assistant" | "bot">("");
  const [env, setEnv] = useState<"" | "dev" | "staging" | "prod">("");
  const [overview, setOverview] = useState<ReadResult<OverviewData> | null>(null);
  const [mcp, setMcp] = useState<ReadResult<{ tools: McpTool[] }> | null>(null);

  const load = useCallback(() => {
    void readOverview(projectId, {
      ...(kind ? { kind } : {}),
      ...(env ? { environment: env } : {}),
    }).then(setOverview);
    void readMcpTools(projectId).then(setMcp);
  }, [projectId, kind, env]);

  useEffect(() => { load(); }, [load]);

  const data = overview?.ok ? overview.data : null;
  const tools = mcp?.ok ? mcp.data.tools : [];

  return (
    <div className="ap-wrap" data-testid="ap-deployments-overview">
      <div className="ap-head">
        <div>
          <h2 className="ap-title">Agent deployments</h2>
          <p className="ap-sub">Versioned assistants and triggered bots across dev, staging, and prod.</p>
        </div>
        <span className="ap-grow" />
        {onCreateBot ? (
          <GatedButton permission="agent:create" projectId={projectId} className="ap-btn" data-kind="primary"
                       onClick={onCreateBot} data-testid="ap-new-bot">
            <Plus size={14} aria-hidden /> New agent or bot
          </GatedButton>
        ) : null}
      </div>

      {overview && !overview.ok ? (
        <ReadFailure problem={{ status: overview.status, text: overview.error }} onRetry={load} testId="ap-overview-err" />
      ) : null}

      {data ? (
        <>
          <div className="ap-kpis" data-testid="ap-overview-kpis">
            <div className="ap-kpi"><span>Live agents</span><b>{data.summary.live_agents}</b></div>
            <div className="ap-kpi"><span>Bots running</span><b>{data.summary.bots_running}</b></div>
            <div className="ap-kpi"><span>Need attention</span><b>{data.summary.need_attention}</b></div>
            <div className="ap-kpi"><span>7d spend</span><b>{usd(data.summary.spend_7d_usd)}</b></div>
          </div>

          <div className="ap-row" role="group" aria-label="Filters">
            {(["", "assistant", "bot"] as const).map((k) => (
              <button key={k || "all"} type="button" className="ap-chip-btn" aria-pressed={kind === k}
                      onClick={() => setKind(k)} data-testid={`ap-filter-kind-${k || "all"}`}>
                {k === "" ? "All kinds" : k === "assistant" ? "Assistants" : "Bots"}
              </button>
            ))}
            <span className="ap-grow" />
            {(["", "dev", "staging", "prod"] as const).map((e) => (
              <button key={e || "all"} type="button" className="ap-chip-btn" aria-pressed={env === e}
                      onClick={() => setEnv(e)} data-testid={`ap-filter-env-${e || "all"}`}>
                {e === "" ? "All envs" : e}
              </button>
            ))}
          </div>

          <div className="ap-split">
            <section className="ap-card" aria-labelledby="ap-ov-agents-h">
              <h3 id="ap-ov-agents-h" className="ap-k">Agents &amp; Bots</h3>
              {!data.agents.length ? (
                <p className="ap-sub" data-testid="ap-overview-empty">No deployed agents match these filters yet.</p>
              ) : (
                <div className="ap-table-wrap">
                  <table className="ap-table" data-testid="ap-overview-table">
                    <thead>
                      <tr>
                        <th>Agent</th>
                        <th>Kind</th>
                        <th>Live</th>
                        <th>Health</th>
                        <th>7d spend</th>
                        <th>Last run</th>
                        <th>Manage</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.agents.map((a) => (
                        <tr key={a.key} data-testid={`ap-agent-row-${a.key}`}>
                          <td>
                            <b>{a.name}</b>
                            <div className="ap-mono ap-sub">{a.key}</div>
                          </td>
                          <td>
                            <span className="ap-pill" data-tone={a.kind === "bot" ? "accent" : undefined}>
                              <Robot size={12} aria-hidden /> {a.kind_detail || a.kind}
                            </span>
                          </td>
                          <td>
                            <div className="ap-row">
                              {a.live.length ? a.live.map((l) => (
                                <span key={`${a.key}-${l.environment}`} className="ap-pill"
                                      data-tone={l.environment === "prod" ? "green" : undefined}>
                                  {l.environment} v{l.version}
                                </span>
                              )) : <span className="ap-sub">Draft</span>}
                            </div>
                          </td>
                          <td>
                            <span className="ap-pill"
                                  data-tone={a.health.state === "healthy" ? "green" : a.health.state === "failing" || a.health.state === "paused" ? "red" : a.health.state === "stuck" ? "amber" : undefined}>
                              {a.health.label}
                            </span>
                          </td>
                          <td className="ap-mono">{usd(a.cost_7d_usd)}</td>
                          <td className="ap-sub">{a.last_run_at && now ? formatRelativeTime(a.last_run_at, now, { empty: "—" }) : "—"}</td>
                          <td>
                            <div className="ap-row">
                              <button type="button" className="ap-link-btn" onClick={() => openPassportTab(a.key, "deployments")}
                                      data-testid={`ap-open-dep-${a.key}`}>Deployments</button>
                              <button type="button" className="ap-link-btn" onClick={() => openPassportTab(a.key, "triggers")}
                                      data-testid={`ap-open-trg-${a.key}`}>Triggers</button>
                              <button type="button" className="ap-link-btn" onClick={() => openPassportTab(a.key, "endpoints")}
                                      data-testid={`ap-open-ep-${a.key}`}>Endpoints</button>
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            <section className="ap-card" aria-labelledby="ap-mcp-h" data-testid="ap-mcp-tools">
              <h3 id="ap-mcp-h" className="ap-k">MCP Tools</h3>
              <p className="ap-sub">Deployed agents exposed as MCP tools (<code className="ap-mono">agent_&lt;key&gt;</code>).</p>
              {mcp && !mcp.ok ? (
                <ReadFailure problem={{ status: mcp.status, text: mcp.error }} onRetry={load} testId="ap-mcp-err" />
              ) : !tools.length ? (
                <p className="ap-sub">No deployed agents exposed as MCP tools yet.</p>
              ) : (
                <div className="ap-wrap" style={{ marginTop: "0.5rem" }}>
                  {tools.map((t) => (
                    <div key={t.tool} className="ap-subcard ap-row" data-testid={`ap-mcp-tool-${t.agent_key}`}>
                      <Wrench size={14} aria-hidden />
                      <div>
                        <b className="ap-mono">{t.tool}</b>
                        <div className="ap-sub">{t.name}</div>
                      </div>
                      <span className="ap-grow" />
                      <span className="ap-pill" data-tone="green">v{t.version}</span>
                    </div>
                  ))}
                </div>
              )}
            </section>
          </div>
        </>
      ) : null}
    </div>
  );
}
