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
  Plus,
  Pulse,
  Robot,
  ShieldCheck,
  Sparkle,
  UsersThree,
  Warning,
} from "@phosphor-icons/react";
import { useMemo, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { createPortal } from "react-dom";
import type {
  AgentStudioFeed,
  StudioAgent,
  StudioPanel,
  StudioWorkflow,
  StudioWorkflowSpec,
} from "../lib/agent-studio";
import type { RoleFeedItem } from "../lib/roles";
import { ClientRelativeTime } from "./client-relative-time";
import type { SkillFeedItem } from "../lib/skills";
import {
  createStudioWorkflow,
  publishStudioWorkflowVersion,
  runStudioAgent,
  runStudioCatalogSmoke,
  runStudioPanel,
  runStudioWorkflow,
  seedStudioCatalog,
  testStudioAgent,
  validateStudioWorkflow,
  type StudioActionResult,
} from "./agent-studio-actions";
import { AgentCreationWizard } from "./agent-creation-wizard";

type Selection =
  | { kind: "agent"; item: StudioAgent }
  | { kind: "panel"; item: StudioPanel }
  | { kind: "workflow"; item: StudioWorkflow };

type StudioMode = "agents" | "panels" | "workflows";

const STARTER_WORKFLOW: StudioWorkflowSpec = {
  entry_step: "plan",
  input_schema: {
    type: "object",
    properties: { objective: { type: "string" } },
    required: ["objective"],
  },
  output_schema: { type: "object" },
  steps: [
    { id: "plan", kind: "task", next: ["verify"] },
    { id: "verify", kind: "task" },
  ],
};

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
  roles,
  skills,
}: {
  feed: AgentStudioFeed;
  roles: RoleFeedItem[];
  skills: SkillFeedItem[];
}) {
  const initial: Selection | undefined = feed.panels[0]
    ? { kind: "panel", item: feed.panels[0] }
    : feed.agents[0]
      ? { kind: "agent", item: feed.agents[0] }
      : feed.workflows[0]
        ? { kind: "workflow", item: feed.workflows[0] }
        : undefined;
  const initialWorkflowVersion =
    initial?.kind === "workflow"
      ? initial.item.versions.find(
          (version) => version.id === initial.item.active_version_id,
        ) ?? initial.item.versions[0]
      : undefined;
  const [mode, setMode] = useState<StudioMode>(
    initial ? `${initial.kind}s` as StudioMode : "agents",
  );
  const [selection, setSelection] = useState<Selection | undefined>(initial);
  const [query, setQuery] = useState("");
  const [objective, setObjective] = useState(
    "Review the proposed change, identify evidence gaps, and return the safest high-leverage next move.",
  );
  const [workflowSpec, setWorkflowSpec] = useState(
    JSON.stringify(initialWorkflowVersion?.spec ?? STARTER_WORKFLOW, null, 2),
  );
  const [newWorkflowKey, setNewWorkflowKey] = useState("");
  const [newWorkflowName, setNewWorkflowName] = useState("");
  const [newWorkflowDescription, setNewWorkflowDescription] = useState("");
  const [result, setResult] = useState<StudioActionResult | undefined>();
  const [wizardOpen, setWizardOpen] = useState(false);
  const [pending, startTransition] = useTransition();
  const router = useRouter();

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

  const filteredWorkflows = useMemo(() => {
    const q = query.toLowerCase().trim();
    return feed.workflows.filter(
      (workflow) =>
        !q ||
        workflow.name.toLowerCase().includes(q) ||
        workflow.workflow_key.toLowerCase().includes(q) ||
        workflow.description?.toLowerCase().includes(q),
    );
  }, [feed.workflows, query]);

  const selectedWorkflowVersion =
    selection?.kind === "workflow"
      ? selection.item.versions.find(
          (version) => version.id === selection.item.active_version_id,
        ) ?? selection.item.versions[0]
      : undefined;
  const workflowPreview = useMemo(() => {
    try {
      const candidate = JSON.parse(workflowSpec) as StudioWorkflowSpec;
      return candidate && Array.isArray(candidate.steps)
        ? candidate
        : selectedWorkflowVersion?.spec ?? STARTER_WORKFLOW;
    } catch {
      return selectedWorkflowVersion?.spec ?? STARTER_WORKFLOW;
    }
  }, [selectedWorkflowVersion?.spec, workflowSpec]);
  const activeVersion =
    selection?.kind === "agent"
      ? selection.item.active_version
      : selection?.kind === "panel"
        ? selection.item.active_version
        : selectedWorkflowVersion;
  const activeRuns = [...feed.agentRuns, ...feed.panelRuns, ...feed.workflowRuns].filter((run) =>
    ["queued", "validating", "leased", "running", "waiting", "waiting_decision"].includes(
      run.status,
    ),
  ).length;
  const liveMessages = feed.a2a.messages.filter((message) =>
    ["queued", "dispatching", "published", "delivering", "partially_acknowledged"].includes(
      message.status,
    ),
  ).length;

  const selectWorkflow = (workflow: StudioWorkflow) => {
    const version =
      workflow.versions.find((item) => item.id === workflow.active_version_id) ??
      workflow.versions[0];
    setSelection({ kind: "workflow", item: workflow });
    setWorkflowSpec(JSON.stringify(version?.spec ?? STARTER_WORKFLOW, null, 2));
    setResult(undefined);
  };

  const switchMode = (next: StudioMode) => {
    setMode(next);
    setResult(undefined);
    if (next === "agents" && feed.agents[0]) {
      setSelection({ kind: "agent", item: feed.agents[0] });
    } else if (next === "panels" && feed.panels[0]) {
      setSelection({ kind: "panel", item: feed.panels[0] });
    } else if (next === "workflows" && feed.workflows[0]) {
      selectWorkflow(feed.workflows[0]);
    }
  };

  const execute = (kind: "test" | "run") => {
    if (!selection || !activeVersion?.id) return;
    setResult(undefined);
    startTransition(async () => {
      const next =
        kind === "test" && selection.kind === "agent"
          ? await testStudioAgent(feed.projectId, activeVersion.id)
          : selection.kind === "workflow"
            ? await runStudioWorkflow(feed.projectId, activeVersion.id, objective)
          : selection.kind === "panel"
            ? await runStudioPanel(feed.projectId, activeVersion.id, objective)
            : await runStudioAgent(feed.projectId, activeVersion.id, objective);
      setResult(next);
    });
  };

  const runCatalogSmoke = () => {
    setResult(undefined);
    startTransition(async () => {
      setResult(await runStudioCatalogSmoke(feed.projectId));
    });
  };

  const installStarterCatalog = () => {
    setResult(undefined);
    startTransition(async () => {
      const seeded = await seedStudioCatalog(feed.projectId);
      setResult(seeded);
      if (seeded.ok) router.refresh();
    });
  };

  const parseWorkflowSpec = (): Record<string, unknown> | undefined => {
    try {
      const parsed = JSON.parse(workflowSpec) as unknown;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("Workflow spec must be a JSON object.");
      }
      return parsed as Record<string, unknown>;
    } catch (error) {
      setResult({
        ok: false,
        message:
          error instanceof Error ? error.message : "Workflow spec is not valid JSON.",
      });
      return undefined;
    }
  };

  const validateOrPublishWorkflow = (action: "validate" | "publish") => {
    const spec = parseWorkflowSpec();
    if (!spec || selection?.kind !== "workflow") return;
    setResult(undefined);
    startTransition(async () => {
      const next =
        action === "validate"
          ? await validateStudioWorkflow(feed.projectId, spec)
          : await publishStudioWorkflowVersion(
              feed.projectId,
              selection.item.id,
              spec,
            );
      setResult(next);
    });
  };

  const createWorkflow = () => {
    const spec = parseWorkflowSpec();
    if (!spec || !newWorkflowKey.trim() || !newWorkflowName.trim()) {
      if (spec) {
        setResult({
          ok: false,
          message: "Workflow key and name are required.",
        });
      }
      return;
    }
    setResult(undefined);
    startTransition(async () => {
      const validated = await validateStudioWorkflow(feed.projectId, spec);
      if (!validated.ok) {
        setResult(validated);
        return;
      }
      const created = await createStudioWorkflow(
        feed.projectId,
        newWorkflowKey.trim(),
        newWorkflowName.trim(),
        newWorkflowDescription.trim(),
        spec,
      );
      setResult(created);
      if (created.ok) {
        setNewWorkflowKey("");
        setNewWorkflowName("");
        setNewWorkflowDescription("");
      }
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

  const libraryEmpty =
    feed.agents.length === 0 && feed.panels.length === 0 && feed.workflows.length === 0;

  return (
    <div className="as-shell">
      <header className="as-header">
        <div>
          <span className="as-kicker"><Sparkle weight="fill" /> Agent Studio / live system</span>
          <h2>Compose intelligence. Watch the work.</h2>
          <p>
            Versioned agents, executable panels, reusable workflows, and every
            coordination message on one governed surface.
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
        {libraryEmpty ? (
          <button className="as-button primary" disabled={pending} onClick={installStarterCatalog}>
            <Sparkle /> {pending ? "Installing starter catalog…" : "Install BOSS starter catalog"}
          </button>
        ) : (
          <button className="as-button primary" disabled={pending} onClick={runCatalogSmoke}>
            <Flask /> {pending ? "Running catalog checks…" : `Smoke test all ${feed.agents.length} agents`}
          </button>
        )}
      </header>

      {libraryEmpty && (
        <div className="as-first-run" role="region" aria-label="Agent Studio first-run setup">
          <div>
            <b>This project has no governed agents yet</b>
            <p>
              Install the reviewed BOSS starter catalog (13 agents, 3 panels) to make Agent Studio
              usable. The setup is project-scoped, idempotent, role-review gated, and never
              dispatches work or spends budget.
            </p>
            <p className="detail">
              Endpoint: <code>POST /v1/agent-studio/catalog/seed</code>
            </p>
          </div>
          <button className="as-button primary" disabled={pending} onClick={installStarterCatalog}>
            <Sparkle /> {pending ? "Installing starter catalog…" : "Install BOSS starter catalog"}
          </button>
          {result && (
            <div className={`as-action-result ${result.ok ? "ok" : "bad"}`} role="status">
              {result.ok ? <CheckCircle /> : <Warning />}
              <span>{result.message}</span>
            </div>
          )}
        </div>
      )}

      <div className="as-metrics" aria-label="Agent Studio summary">
        <div><span>Published agents</span><b>{feed.agents.length}</b><small>immutable active versions</small></div>
        <div><span>Executable panels</span><b>{feed.panels.length}</b><small>attributed consolidation</small></div>
        <div><span>Workflows</span><b>{feed.workflows.length}</b><small>validated reusable graphs</small></div>
        <div><span>Active runs</span><b>{activeRuns}</b><small>leased or waiting</small></div>
        <div><span>Live A2A</span><b>{liveMessages}</b><small>{feed.a2a.channels.length} governed channels</small></div>
      </div>

      <div className="as-workbench">
        <aside className="as-library" aria-label="Agent, panel, and workflow library">
          <div className="as-pane-head">
            <span>Library</span>
            <small>
              {mode === "agents"
                ? filteredAgents.length
                : mode === "panels"
                  ? filteredPanels.length
                  : filteredWorkflows.length}{" "}
              objects
            </small>
          </div>
          <div className="as-mode-switch" role="tablist">
            <button
              className={mode === "agents" ? "active" : ""}
              onClick={() => switchMode("agents")}
              role="tab"
              aria-selected={mode === "agents"}
            >
              <Robot /> Agents
            </button>
            <button
              className={mode === "panels" ? "active" : ""}
              onClick={() => switchMode("panels")}
              role="tab"
              aria-selected={mode === "panels"}
            >
              <UsersThree /> Panels
            </button>
            <button
              className={mode === "workflows" ? "active" : ""}
              onClick={() => switchMode("workflows")}
              role="tab"
              aria-selected={mode === "workflows"}
            >
              <GitBranch /> Flows
            </button>
          </div>
          <label className="as-search">
            <span className="sr-only">Filter library</span>
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Filter the active library…"
            />
            <span>⌘ K</span>
          </label>
          <div className="as-object-list">
            {mode === "agents" ? (
              filteredAgents.length > 0 ? filteredAgents.map((agent) => (
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
                )) : (
                  <StudioEmptyState
                    kind="agents"
                    filtered={Boolean(query.trim())}
                    pending={pending}
                    onSeed={installStarterCatalog}
                  />
                )
            ) : mode === "panels" ? (
              filteredPanels.length > 0 ? filteredPanels.map((panel) => (
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
                )) : (
                  <StudioEmptyState
                    kind="panels"
                    filtered={Boolean(query.trim())}
                    pending={pending}
                    onSeed={installStarterCatalog}
                  />
                )
            ) : (
              filteredWorkflows.length > 0 ? filteredWorkflows.map((workflow) => (
                <button
                  key={workflow.id}
                  className={
                    selection?.kind === "workflow" &&
                    selection.item.id === workflow.id
                      ? "as-object active"
                      : "as-object"
                  }
                  onClick={() => selectWorkflow(workflow)}
                >
                  <span className="as-mini-orb workflow"><GitBranch /></span>
                  <span>
                    <b>{workflow.name}</b>
                    <small>{workflow.workflow_key}</small>
                  </span>
                  <i className={tone(workflow.status)}>{workflow.status}</i>
                </button>
              )) : (
                <StudioEmptyState
                  kind="workflows"
                  filtered={Boolean(query.trim())}
                  pending={pending}
                  onSeed={installStarterCatalog}
                />
              )
            )}
          </div>
          {mode === "agents" && (
            <button
              className="as-create-agent"
              onClick={() => setWizardOpen(true)}
            >
              <Plus />
              <span>
                <b>Create agent</b>
                <small>Guided, governed commissioning</small>
              </span>
              <ArrowRight />
            </button>
          )}
          {mode === "workflows" && (
            <details className="as-new-workflow">
              <summary><Plus /> New governed workflow</summary>
              <label>
                Key
                <input
                  value={newWorkflowKey}
                  onChange={(event) => setNewWorkflowKey(event.target.value)}
                  placeholder="release_readiness"
                />
              </label>
              <label>
                Name
                <input
                  value={newWorkflowName}
                  onChange={(event) => setNewWorkflowName(event.target.value)}
                  placeholder="Release readiness"
                />
              </label>
              <label>
                Purpose
                <input
                  value={newWorkflowDescription}
                  onChange={(event) => setNewWorkflowDescription(event.target.value)}
                  placeholder="What this workflow governs"
                />
              </label>
              <button
                className="as-button primary"
                disabled={pending}
                onClick={createWorkflow}
              >
                <Plus /> {pending ? "Validating…" : "Validate + publish"}
              </button>
            </details>
          )}
        </aside>

        <main className="as-canvas">
          {selection ? (
            <>
              <div className="as-canvas-head">
                <div>
                  <span>
                    {selection.kind === "panel"
                      ? "Panel topology"
                      : selection.kind === "workflow"
                        ? "Workflow graph / draft preview"
                        : "Agent authority graph"}
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
                ) : selection.kind === "workflow" ? (
                  <WorkflowTopology workflow={selection.item} spec={workflowPreview} />
                ) : (
                  <AgentTopology agent={selection.item} />
                )}
              </div>

              {selection.kind === "workflow" && (
                <div className="as-workflow-editor">
                  <div className="as-command-label">
                    <span><BracketsCurly /> Governed workflow spec</span>
                    <small>
                      Edit JSON, prove the graph, then publish an immutable version.
                    </small>
                  </div>
                  <textarea
                    aria-label="Workflow JSON specification"
                    value={workflowSpec}
                    onChange={(event) => setWorkflowSpec(event.target.value)}
                    rows={12}
                    spellCheck={false}
                  />
                  <div className="as-command-actions">
                    <button
                      className="as-button secondary"
                      disabled={pending}
                      onClick={() => validateOrPublishWorkflow("validate")}
                    >
                      <Flask /> Validate draft
                    </button>
                    <button
                      className="as-button primary"
                      disabled={pending}
                      onClick={() => validateOrPublishWorkflow("publish")}
                    >
                      <GitBranch /> Publish version
                    </button>
                    <span className="as-run-contract">
                      <ShieldCheck /> reachability · bindings · bounded loops
                    </span>
                  </div>
                </div>
              )}

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
                    <Play weight="fill" />{" "}
                    {pending ? "Dispatching…" : `Run ${selection.kind}`}
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
            <div className="as-empty">
              <b>No Studio object is selected.</b>
              <span>Install the reviewed BOSS starter catalog or commission a scoped agent to begin.</span>
              {feed.agents.length === 0 && (
                <button className="as-button primary" disabled={pending} onClick={installStarterCatalog}>
                  <Sparkle /> {pending ? "Installing starter catalog…" : "Install BOSS starter catalog"}
                </button>
              )}
            </div>
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
                    <ClientRelativeTime iso={message.created_at} style="short" empty="unknown" />
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
      {wizardOpen && (
        createPortal(
          <AgentCreationWizard
            projectId={feed.projectId}
            roles={roles}
            skills={skills}
            onClose={() => setWizardOpen(false)}
          />,
          document.body,
        )
      )}
    </div>
  );
}

function StudioEmptyState({
  kind,
  filtered,
  pending,
  onSeed,
}: {
  kind: "agents" | "panels" | "workflows";
  filtered: boolean;
  pending: boolean;
  onSeed: () => void;
}) {
  if (filtered) {
    return <p className="as-library-empty">No {kind} match this filter.</p>;
  }
  return (
    <div className="as-library-empty">
      <b>No {kind} installed yet.</b>
      <span>The reviewed starter catalog is project-scoped and idempotent.</span>
      <button className="as-button secondary" disabled={pending} onClick={onSeed}>
        <Sparkle /> {pending ? "Installing…" : "Install starter catalog"}
      </button>
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

function WorkflowTopology({
  workflow,
  spec,
}: {
  workflow: StudioWorkflow;
  spec: StudioWorkflowSpec;
}) {
  const steps = spec.steps ?? [];
  return (
    <div className="as-workflow-graph">
      <div className="as-flow-summary">
        <span><GitBranch /> {steps.length} steps</span>
        <span><Play /> entry {spec.entry_step}</span>
        <span><ShieldCheck /> immutable on publish</span>
      </div>
      <div className="as-flow-lane">
        {steps.map((step, index) => {
          const targets = Array.isArray(step.next)
            ? step.next
            : typeof step.next === "string"
              ? [step.next]
              : [];
          const binding = step.binding ?? {};
          const bindingLabel = Object.entries(binding)
            .slice(0, 2)
            .map(([key, value]) => `${compact(key)}: ${String(value)}`)
            .join(" · ");
          return (
            <div className="as-flow-segment" key={`${step.id}-${index}`}>
              <article
                className={
                  step.id === spec.entry_step
                    ? "as-flow-node entry"
                    : "as-flow-node"
                }
              >
                <small>{step.id === spec.entry_step ? "ENTRY" : `STEP ${index + 1}`}</small>
                <b>{step.id}</b>
                <span>{compact(step.kind)}</span>
                <em>{bindingLabel || "runtime-scoped binding"}</em>
                <footer>
                  {targets.length
                    ? `next → ${targets.join(", ")}`
                    : "terminal frontier"}
                </footer>
              </article>
              {index < steps.length - 1 && (
                <span className="as-flow-arrow" aria-hidden="true">
                  <ArrowRight />
                </span>
              )}
            </div>
          );
        })}
      </div>
      <div className="as-flow-foot">
        <span>{workflow.workflow_key}</span>
        <span>{workflow.version_count ?? workflow.versions.length} published version(s)</span>
        <span>policy snapshot captured per run</span>
      </div>
    </div>
  );
}
