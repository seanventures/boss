"use client";

import React from "react";
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
  Robot,
  ShieldCheck,
  Sparkle,
  UsersThree,
  Warning,
} from "@phosphor-icons/react";
import { useEffect, useMemo, useRef, useState, useTransition } from "react";
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

import { callAction } from "../lib/call-action";
import { StartRefusal, asReadiness, type PendingRefusal } from "./workflow-readiness-ui";
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
  if (["not_configured", "unknown"].includes(status)) {
    return "quiet";
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
  actor,
}: {
  feed: AgentStudioFeed;
  roles: RoleFeedItem[];
  skills: SkillFeedItem[];
  /** The signed-in actor every Studio write is made as. It was the literal 'vp_agents', which
   *  a 'ceo' API key may not act as, so every Studio write 403'd on an authed install. */
  actor: string;
}) {
  const initial: Selection | undefined = feed.agents[0]
    ? { kind: "agent", item: feed.agents[0] }
    : feed.panels[0]
      ? { kind: "panel", item: feed.panels[0] }
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
  const [requestedRole, setRequestedRole] = useState("");
  const [objective, setObjective] = useState("");
  const [workflowSpec, setWorkflowSpec] = useState(
    JSON.stringify(initialWorkflowVersion?.spec ?? STARTER_WORKFLOW, null, 2),
  );
  const [newWorkflowKey, setNewWorkflowKey] = useState("");
  const [newWorkflowName, setNewWorkflowName] = useState("");
  const [newWorkflowDescription, setNewWorkflowDescription] = useState("");
  const [result, setResult] = useState<StudioActionResult | undefined>();
  const [refusal, setRefusal] = useState<PendingRefusal | null>(null);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [pending, startTransition] = useTransition();
  const router = useRouter();
  const linkedRole = useRef("");

  useEffect(() => {
    const selectLinkedRole = () => {
      const role = new URL(window.location.href).searchParams.get("studio_role")?.trim();
      if (!role) {
        if (linkedRole.current) {
          linkedRole.current = "";
          setRequestedRole("");
          setQuery("");
          setSelection(initial);
        }
        return;
      }
      linkedRole.current = role;
      const match = feed.agents.find((agent) =>
        agent.active_version?.role_key === role || agent.agent_key === role);
      setMode("agents");
      setRequestedRole(match ? "" : role);
      setQuery(match ? "" : role);
      setSelection(match ? { kind: "agent", item: match } : undefined);
    };
    selectLinkedRole();
    window.addEventListener("popstate", selectLinkedRole);
    return () => window.removeEventListener("popstate", selectLinkedRole);
  }, [feed.agents]);

  const clearLinkedRole = () => {
    if (!linkedRole.current) return;
    linkedRole.current = "";
    const url = new URL(window.location.href);
    url.searchParams.delete("studio_role");
    window.history.replaceState(null, "", url.toString());
  };

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
    clearLinkedRole();
    setMode(next);
    setQuery("");
    setRequestedRole("");
    setResult(undefined);
    if (next === "agents" && feed.agents[0]) {
      setSelection({ kind: "agent", item: feed.agents[0] });
    } else if (next === "panels" && feed.panels[0]) {
      setSelection({ kind: "panel", item: feed.panels[0] });
    } else if (next === "workflows" && feed.workflows[0]) {
      selectWorkflow(feed.workflows[0]);
    } else {
      setSelection(undefined);
    }
  };

  const execute = (kind: "test" | "run", startAnyway: string[] = []) => {
    if (!selection || !activeVersion?.id) return;
    setResult(undefined);
    setRefusal(null);
    startTransition(async () => {
      const next =
        kind === "test" && selection.kind === "agent"
          ? await callAction(testStudioAgent)(feed.projectId, activeVersion.id, actor)
          : selection.kind === "workflow"
            ? await callAction(runStudioWorkflow)(feed.projectId, activeVersion.id, objective, actor,
                                                  startAnyway)
          : selection.kind === "panel"
            ? await callAction(runStudioPanel)(feed.projectId, activeVersion.id, objective, actor)
            : await callAction(runStudioAgent)(feed.projectId, activeVersion.id, objective, actor);
      // A Run refused for what the project lacks gets the same checklist and "Run anyway" as
      // every other door — it showed "Agent Studio returned 409." (review of the readiness
      // change).
      const readiness = !next.ok ? asReadiness(next.readiness) : null;
      if (readiness) {
        setRefusal({ label: "Run", message: next.message, readiness,
                     retry: (keys) => execute(kind, keys) });
        return;
      }
      setResult(next);
    });
  };

  const runCatalogSmoke = () => {
    setResult(undefined);
    startTransition(async () => {
      setResult(await callAction(runStudioCatalogSmoke)(feed.projectId, actor));
    });
  };

  const installStarterCatalog = () => {
    setResult(undefined);
    startTransition(async () => {
      const seeded = await callAction(seedStudioCatalog)(feed.projectId, actor);
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
          ? await callAction(validateStudioWorkflow)(feed.projectId, spec, actor)
          : await callAction(publishStudioWorkflowVersion)(
              feed.projectId,
              selection.item.id,
              spec,
              actor,
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
      const validated = await callAction(validateStudioWorkflow)(feed.projectId, spec, actor);
      if (!validated.ok) {
        setResult(validated);
        return;
      }
      const created = await callAction(createStudioWorkflow)(
        feed.projectId,
        newWorkflowKey.trim(),
        newWorkflowName.trim(),
        newWorkflowDescription.trim(),
        spec,
        actor,
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

  if (libraryEmpty) {
    return (
      <div className="as-shell">
        <header className="as-header">
          <div>
            <span className="as-kicker">Agent Studio</span>
            <h2>Add your first agents</h2>
            <p>
              Start with the built-in catalog or create one agent for this project.
            </p>
          </div>
        </header>
        <div className="as-first-run" role="region" aria-label="Agent Studio first-run setup">
          <div>
            <p>
              The reviewed starter catalog adds agent definitions, panels, and workflows to this project.
              It does not start a run or spend budget.
            </p>
            {requestedRole ? (
              <p data-testid="as-missing-definition">
                No agent definition for {compact(requestedRole)}. This role has no Studio definition.
              </p>
            ) : null}
          </div>
          <button className="as-button primary" disabled={pending} onClick={installStarterCatalog}>
            <Sparkle /> {pending ? "Installing starter catalog…" : "Install BOSS starter catalog"}
          </button>
          <button
            className="as-button secondary"
            disabled={pending}
            onClick={() => setWizardOpen(true)}
          >
            <Plus /> Create an agent
          </button>
          {result && (
            <div className={`as-action-result ${result.ok ? "ok" : "bad"}`} role="status">
              {result.ok ? <CheckCircle /> : <Warning />}
              <span>{result.message}</span>
            </div>
          )}
        </div>
        {wizardOpen &&
          createPortal(
            <AgentCreationWizard
              projectId={feed.projectId}
              actor={actor}
              roles={roles}
              skills={skills}
              onClose={() => setWizardOpen(false)}
            />,
            document.body,
          )}
      </div>
    );
  }

  return (
    <div className="as-shell">
      <header className="as-header">
        <div>
          <span className="as-kicker">Agent Studio</span>
          <h2>Build and run agents</h2>
          <p>
            Define an agent, test it, then run a task. Panels and workflows combine agents.
          </p>
        </div>
        <button className="as-button secondary" disabled={pending} onClick={runCatalogSmoke}>
          <Flask /> {pending ? "Running catalog checks…" : `Test all ${feed.agents.length} agent${feed.agents.length === 1 ? "" : "s"}`}
        </button>
      </header>

      <div className="as-metrics" aria-label="Agent Studio summary">
        <div><span>Agents</span><b>{feed.agents.length}</b><small>available definitions</small></div>
        <div><span>Panels</span><b>{feed.panels.length}</b><small>agent groups</small></div>
        <div><span>Workflows</span><b>{feed.workflows.length}</b><small>reusable steps</small></div>
        <div><span>Active runs</span><b>{activeRuns}</b><small>working or waiting</small></div>
        <div><span>Messages</span><b>{liveMessages}</b><small>{feed.a2a.channels.length} channels</small></div>
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
              onChange={(event) => {
                clearLinkedRole(); setQuery(event.target.value); setRequestedRole("");
              }}
              placeholder="Find an agent, panel, or workflow"
            />
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
                    onClick={() => {
                      clearLinkedRole(); setSelection({ kind: "agent", item: agent }); setRequestedRole("");
                    }}
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
                        ? "Workflow steps"
                        : "Agent definition"}
                  </span>
                  <h3>{selection.item.name}</h3>
                </div>
                <div className="as-version">
                  <GitBranch /> v{activeVersion?.version ?? "—"} · {activeVersion?.id.slice(0, 12)}
                </div>
              </div>

              <div className={`as-topology ${selection.kind}`}>
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
                  <span><BracketsCurly /> Task</span>
                  <small>Runs are recorded with evidence.</small>
                </div>
                <textarea
                  value={objective}
                  onChange={(event) => setObjective(event.target.value)}
                  placeholder="Describe the task for this agent"
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
                {refusal ? (
                  <StartRefusal refusal={refusal} projectId={feed.projectId} busy={pending}
                                onDismiss={() => setRefusal(null)} />
                ) : null}
              </div>
            </>
          ) : (
            <div className="as-empty">
              <b>{requestedRole
                ? `No agent definition for ${compact(requestedRole)}.`
                : "No Studio object is selected."}</b>
              <span>{requestedRole
                ? "This role can have recorded work without a Studio definition. Create and validate one here."
                : "Install the reviewed BOSS starter catalog or commission a scoped agent to begin."}</span>
              {requestedRole && (
                <button className="as-button primary" onClick={() => setWizardOpen(true)}>
                  <Plus /> Create agent
                </button>
              )}
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
            <small><Broadcast /> Messaging</small>
          </div>
          <div className="as-transport">
            <div>
              <span className={`as-beacon ${tone(feed.a2a.health.status)}`} />
              <b>Transport</b>
            </div>
            <span>{feed.a2a.health.status === "not_configured"
              ? "Not configured"
              : `${feed.a2a.health.transport?.latency_ms ?? "—"} ms`}</span>
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
            actor={actor}
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
  if (!version) {
    return <p className="as-agent-summary-empty">No active version. This agent cannot run yet.</p>;
  }
  const skills = version.requested_skills ?? [];
  return (
    <dl className="as-agent-summary">
      {agent.description ? <div><dt>Purpose</dt><dd>{agent.description}</dd></div> : null}
      <div><dt>Role</dt><dd>{compact(version.role_key)}</dd></div>
      <div><dt>Execution</dt><dd>{compact(version.model_policy.execution_mode ?? "deterministic_brief")}</dd></div>
      <div><dt>Requested skills</dt><dd>{skills.length ? skills.map(compact).join(", ") : "None"}</dd></div>
      <div><dt>Data boundary</dt><dd>{compact(version.scopes.data_boundary)}</dd></div>
      <div><dt>Validation</dt><dd>{version.validation.valid ? "Passed" : "Pending"}</dd></div>
      <div><dt>Active runs</dt><dd>{agent.active_run_count}</dd></div>
    </dl>
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
          // WHAT THIS STEP ACTUALLY RUNS, said rather than stringified.
          //
          // `String(value)` on a binding printed `inputs: [object Object]` on every node — the
          // most prominent line on the card, carrying no information at all. Bindings are not a
          // flat map to be dumped: `custom_step` and `capability` NAME the work, `inputs` is
          // wiring that belongs in the editor, and `agent_work` means there is no fixed skill
          // at all. So each is read for what it is.
          const runs = binding.agent_work
            ? "works from your instructions"
            : String(binding.custom_step || binding.capability || "") || "runtime-scoped binding";
          const inputCount = Object.keys(
            (binding.inputs ?? {}) as Record<string, unknown>).length;
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
                {/* WHO RUNS IT, above what it runs. A graph of a governed workflow that does not
                    name the agent answering for each step is missing the thing the governance is
                    about. Empty on an agent step is a decision — BOSS picks per run — and says
                    so rather than rendering blank. */}
                <span className="as-flow-agent">
                  {step.captain
                    ? String(step.captain)
                    : binding.agent_work
                      ? "agent chosen per run"
                      : compact(step.kind)}
                </span>
                <em>{runs}</em>
                {inputCount ? (
                  <span className="as-flow-inputs">
                    {inputCount} input{inputCount === 1 ? "" : "s"} wired
                  </span>
                ) : null}
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
