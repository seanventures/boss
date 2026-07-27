"use client";

import {
  ArrowRight,
  BracketsCurly,
  Broadcast,
  CheckCircle,
  CirclesThreePlus,
  Flask,
  GitBranch,
  LockKey,
  Play,
  Pulse,
  Robot,
  ShieldCheck,
  Sparkle,
  UsersThree,
  Warning,
} from "@phosphor-icons/react";
import { useMemo, useState, useTransition } from "react";
import type {
  AgentStudioFeed,
  StudioAgent,
  StudioPanel,
} from "../lib/agent-studio";
import {
  runStudioAgent,
  runStudioPanel,
  testStudioAgent,
  type StudioActionResult,
} from "./agent-studio-actions";

type Selection =
  | { kind: "agent"; item: StudioAgent }
  | { kind: "panel"; item: StudioPanel };

function initials(name: string): string {
  return name
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();
}

function compact(value: unknown): string {
  if (typeof value === "string") return value.replaceAll("_", " ");
  return "unknown";
}

function timeAgo(value: string): string {
  const delta = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(delta)) return "unknown";
  const minutes = Math.max(0, Math.floor(delta / 60000));
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

function tone(status: string): "ok" | "warn" | "bad" | "quiet" {
  if (["active", "published", "passed", "completed", "acknowledged", "healthy"].includes(status)) {
    return "ok";
  }
  if (["blocked", "failed", "deadlettered", "degraded", "unavailable"].includes(status)) {
    return "bad";
  }
  if (["queued", "running", "leased", "dispatching", "delivering", "waiting_member"].includes(status)) {
    return "warn";
  }
  return "quiet";
}

export function AgentStudio({
  feed,
}: {
  feed: AgentStudioFeed;
}) {
  const initial: Selection | undefined = feed.panels[0]
    ? { kind: "panel", item: feed.panels[0] }
    : feed.agents[0]
      ? { kind: "agent", item: feed.agents[0] }
      : undefined;
  const [mode, setMode] = useState<"agents" | "panels">(initial?.kind === "agent" ? "agents" : "panels");
  const [selection, setSelection] = useState<Selection | undefined>(initial);
  const [query, setQuery] = useState("");
  const [objective, setObjective] = useState(
    "Review the proposed change, identify evidence gaps, and return the safest high-leverage next move.",
  );
  const [result, setResult] = useState<StudioActionResult | undefined>();
  const [pending, startTransition] = useTransition();

  const filteredAgents = useMemo(() => {
    const q = query.toLowerCase().trim();
    return feed.agents.filter(
      (agent) =>
        !q ||
        agent.name.toLowerCase().includes(q) ||
        agent.agent_key.toLowerCase().includes(q) ||
        agent.active_version?.role_key.toLowerCase().includes(q),
    );
  }, [feed.agents, query]);

  const filteredPanels = useMemo(() => {
    const q = query.toLowerCase().trim();
    return feed.panels.filter(
      (panel) =>
        !q ||
        panel.name.toLowerCase().includes(q) ||
        panel.panel_key.toLowerCase().includes(q) ||
        panel.purpose.toLowerCase().includes(q),
    );
  }, [feed.panels, query]);

  const activeVersion =
    selection?.kind === "agent"
      ? selection.item.active_version
      : selection?.item.active_version;
  const activeRuns = feed.agentRuns.filter((run) =>
    ["queued", "leased", "running", "waiting_decision"].includes(run.status),
  ).length;
  const liveMessages = feed.a2a.messages.filter((message) =>
    ["queued", "dispatching", "published", "delivering", "partially_acknowledged"].includes(
      message.status,
    ),
  ).length;

  const execute = (kind: "test" | "run") => {
    if (!selection || !activeVersion?.id) return;
    setResult(undefined);
    startTransition(async () => {
      const next =
        kind === "test" && selection.kind === "agent"
          ? await testStudioAgent(feed.projectId, activeVersion.id)
          : selection.kind === "panel"
            ? await runStudioPanel(feed.projectId, activeVersion.id, objective)
            : await runStudioAgent(feed.projectId, activeVersion.id, objective);
      setResult(next);
    });
  };

  if (!feed.apiReachable) {
    return (
      <div className="as-unavailable">
        <Warning size={24} />
        <div>
          <strong>Agent Studio control plane is unavailable</strong>
          <p>{feed.blockedReason}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="as-shell">
      <header className="as-header">
        <div>
          <span className="as-kicker"><Sparkle weight="fill" /> Agent Studio / live system</span>
          <h2>Compose intelligence. Watch the work.</h2>
          <p>
            Versioned agents, executable panels, and every coordination message on one
            governed surface.
          </p>
        </div>
        <div className="as-header-signal" aria-label="A2A transport health">
          <span className={`as-beacon ${tone(feed.a2a.health.status)}`} />
          <div>
            <b>{feed.a2a.health.status}</b>
            <small>
              Redis {feed.a2a.health.transport?.configured ? "linked" : "not configured"} ·
              Postgres canonical
            </small>
          </div>
        </div>
      </header>

      <div className="as-metrics" aria-label="Agent Studio summary">
        <div><span>Published agents</span><b>{feed.agents.length}</b><small>immutable active versions</small></div>
        <div><span>Executable panels</span><b>{feed.panels.length}</b><small>attributed consolidation</small></div>
        <div><span>Active runs</span><b>{activeRuns}</b><small>leased or waiting</small></div>
        <div><span>Live A2A</span><b>{liveMessages}</b><small>{feed.a2a.channels.length} governed channels</small></div>
        <div><span>Proof runs</span><b>{feed.tests.length}</b><small>deterministic test bench</small></div>
      </div>

      <div className="as-workbench">
        <aside className="as-library" aria-label="Agent and panel library">
          <div className="as-pane-head">
            <span>Library</span>
            <small>{mode === "agents" ? filteredAgents.length : filteredPanels.length} objects</small>
          </div>
          <div className="as-mode-switch" role="tablist">
            <button
              className={mode === "agents" ? "active" : ""}
              onClick={() => setMode("agents")}
              role="tab"
              aria-selected={mode === "agents"}
            >
              <Robot /> Agents
            </button>
            <button
              className={mode === "panels" ? "active" : ""}
              onClick={() => setMode("panels")}
              role="tab"
              aria-selected={mode === "panels"}
            >
              <UsersThree /> Panels
            </button>
          </div>
          <label className="as-search">
            <span className="sr-only">Filter library</span>
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Filter by name or role…"
            />
            <span>⌘ K</span>
          </label>
          <div className="as-object-list">
            {mode === "agents"
              ? filteredAgents.map((agent) => (
                  <button
                    key={agent.id}
                    className={
                      selection?.kind === "agent" && selection.item.id === agent.id
                        ? "as-object active"
                        : "as-object"
                    }
                    onClick={() => setSelection({ kind: "agent", item: agent })}
                  >
                    <span className="as-mini-orb">{initials(agent.name)}</span>
                    <span>
                      <b>{agent.name}</b>
                      <small>{compact(agent.active_version?.role_key)}</small>
                    </span>
                    <i className={tone(agent.status)}>{agent.status}</i>
                  </button>
                ))
              : filteredPanels.map((panel) => (
                  <button
                    key={panel.id}
                    className={
                      selection?.kind === "panel" && selection.item.id === panel.id
                        ? "as-object active"
                        : "as-object"
                    }
                    onClick={() => setSelection({ kind: "panel", item: panel })}
                  >
                    <span className="as-mini-orb panel"><CirclesThreePlus /></span>
                    <span>
                      <b>{panel.name}</b>
                      <small>{compact(panel.active_version?.topology)}</small>
                    </span>
                    <i className={tone(panel.status)}>{panel.status}</i>
                  </button>
                ))}
          </div>
        </aside>

        <main className="as-canvas">
          {selection ? (
            <>
              <div className="as-canvas-head">
                <div>
                  <span>
                    {selection.kind === "panel" ? "Panel topology" : "Agent authority graph"}
                  </span>
                  <h3>{selection.item.name}</h3>
                </div>
                <div className="as-version">
                  <GitBranch /> v{activeVersion?.version ?? "—"} · {activeVersion?.id.slice(0, 12)}
                </div>
              </div>

              <div className={`as-topology ${selection.kind}`}>
                <div className="as-grid-lines" aria-hidden="true" />
                {selection.kind === "panel" ? (
                  <PanelTopology panel={selection.item} />
                ) : (
                  <AgentTopology agent={selection.item} />
                )}
              </div>

              <div className="as-command">
                <div className="as-command-label">
                  <span><BracketsCurly /> Run objective</span>
                  <small>Every execution becomes a leased job with evidence.</small>
                </div>
                <textarea
                  value={objective}
                  onChange={(event) => setObjective(event.target.value)}
                  rows={3}
                />
                <div className="as-command-actions">
                  {selection.kind === "agent" && (
                    <button
                      className="as-button secondary"
                      disabled={pending || !activeVersion}
                      onClick={() => execute("test")}
                    >
                      <Flask /> Compile + test
                    </button>
                  )}
                  <button
                    className="as-button primary"
                    disabled={pending || !activeVersion || !objective.trim()}
                    onClick={() => execute("run")}
                  >
                    <Play weight="fill" /> {pending ? "Dispatching…" : `Run ${selection.kind}`}
                  </button>
                  <span className="as-run-contract">
                    <LockKey /> policy · budget · lease · receipt
                  </span>
                </div>
                {result && (
                  <div className={`as-action-result ${result.ok ? "ok" : "bad"}`} role="status">
                    {result.ok ? <CheckCircle /> : <Warning />}
                    <span>{result.message}</span>
                  </div>
                )}
              </div>
            </>
          ) : (
            <div className="as-empty">Select an agent or panel to inspect its graph.</div>
          )}
        </main>

        <aside className="as-inspector" aria-label="A2A trace inspector">
          <div className="as-pane-head">
            <span>Live coordination</span>
            <small><Broadcast /> A2A fabric</small>
          </div>
          <div className="as-transport">
            <div>
              <span className={`as-beacon ${tone(feed.a2a.health.status)}`} />
              <b>Transport</b>
            </div>
            <span>{feed.a2a.health.transport?.latency_ms ?? "—"} ms</span>
          </div>
          <div className="as-channel-rail">
            {feed.a2a.channels.map((channel) => (
              <div key={channel.id}>
                <span>#{channel.channel_key}</span>
                <b>{channel.message_count}</b>
              </div>
            ))}
          </div>
          <div className="as-trace-title">
            <span>Message trace</span>
            <small>newest first</small>
          </div>
          <div className="as-trace">
            {feed.a2a.messages.slice(0, 12).map((message, index) => (
              <article key={message.id} className="as-message">
                <span className={`as-trace-dot ${tone(message.status)}`} />
                <div>
                  <div className="as-message-top">
                    <b>{message.message_kind.replaceAll("_", " ")}</b>
                    <time>{timeAgo(message.created_at)}</time>
                  </div>
                  <p>{message.sender_id} <ArrowRight /> {String(message.payload.objective ?? message.payload.summary ?? message.channel_key)}</p>
                  <small>
                    {message.channel_key} · {message.status} · trace {message.trace_id.slice(0, 8)}
                  </small>
                </div>
                {index === 0 && <i>LIVE</i>}
              </article>
            ))}
            {feed.a2a.messages.length === 0 && (
              <div className="as-trace-empty">
                <Broadcast />
                <b>No coordination traffic yet</b>
                <span>Queued runs and agent messages will appear here with lineage.</span>
              </div>
            )}
          </div>
          <div className="as-proof">
            <ShieldCheck />
            <div>
              <b>Durable audit</b>
              <span>Redis cursors accelerate delivery. Postgres owns truth, retry, ACK, and replay.</span>
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}

function AgentTopology({ agent }: { agent: StudioAgent }) {
  const version = agent.active_version;
  const skills = version?.requested_skills ?? [];
  return (
    <div className="as-agent-graph">
      <div className="as-authority-node role">
        <small>AUTHORITY</small>
        <b>{compact(version?.role_key)}</b>
        <span>RoleSpec</span>
      </div>
      <div className="as-link horizontal" />
      <div className="as-agent-core">
        <span className="as-core-ring ring-one" />
        <span className="as-core-ring ring-two" />
        <span className="as-core-orb">{initials(agent.name)}</span>
        <b>{agent.name}</b>
        <small>{compact(String(version?.model_policy.execution_mode ?? "deterministic_brief"))}</small>
      </div>
      <div className="as-link horizontal right" />
      <div className="as-authority-node tools">
        <small>EFFECTIVE TOOLS</small>
        <b>{skills.length}</b>
        <span>{skills.slice(0, 2).map(compact).join(" · ") || "No tools requested"}</span>
      </div>
      <div className="as-agent-floor">
        <span><LockKey /> {compact(version?.scopes.data_boundary)}</span>
        <span><Pulse /> {agent.active_run_count} active</span>
        <span><ShieldCheck /> validation {version?.validation.valid ? "passed" : "pending"}</span>
      </div>
    </div>
  );
}

function PanelTopology({ panel }: { panel: StudioPanel }) {
  const members = panel.active_version?.members ?? [];
  return (
    <div className="as-panel-graph">
      <svg className="as-panel-links" viewBox="0 0 800 360" preserveAspectRatio="none" aria-hidden="true">
        {members.map((_, index) => {
          const x = 80 + (index * 640) / Math.max(1, members.length - 1);
          return <path key={index} d={`M ${x} 80 C ${x} 185, 400 175, 400 260`} />;
        })}
      </svg>
      <div className="as-member-row">
        {members.map((member) => (
          <div className="as-member-node" key={member.member_key}>
            <span>{initials(member.agent_name)}</span>
            <b>{member.agent_name}</b>
            <small>{member.member_key} · {member.required ? "required" : `weight ${member.weight}`}</small>
            <em>{member.perspective}</em>
          </div>
        ))}
      </div>
      <div className="as-consolidator">
        <span><UsersThree /></span>
        <div>
          <small>CONSOLIDATION</small>
          <b>{compact(panel.active_version?.consolidation_policy.kind)}</b>
          <em>attribution + dissent preserved</em>
        </div>
      </div>
    </div>
  );
}
