"use client";

import {
  ArrowLeft,
  ArrowRight,
  Check,
  CheckCircle,
  CirclesThreePlus,
  Cpu,
  Key,
  LockKey,
  PaperPlaneTilt,
  Robot,
  ShieldCheck,
  Sparkle,
  Warning,
  Wrench,
  X,
} from "@phosphor-icons/react";
import type { ReactNode } from "react";
import { useEffect, useMemo, useState, useTransition } from "react";
import type { RoleFeedItem } from "../lib/roles";
import type { SkillFeedItem } from "../lib/skills";
import {
  applyAgentWizardPattern,
  buildAgentWizardSubmission,
  createInitialAgentWizardDraft,
  parseWizardList,
  parseWizardLines,
  slugifyAgentKey,
  validateAgentWizardDraft,
  type AgentWizardDraft,
  type AgentWizardIssue,
  type AgentWizardPattern,
  NEW_ROLE,
} from "../lib/agent-wizard";
import { createStudioAgent, type StudioActionResult } from "./agent-studio-actions";
import { loadGrantable, type GrantableServer } from "../lib/agents";

import { callAction } from "../lib/call-action";
type WizardProps = {
  projectId: string;
  /** The signed-in actor. Every write this wizard makes is made as them. */
  actor: string;
  roles: RoleFeedItem[];
  skills: SkillFeedItem[];
  onClose: () => void;
};

const STEPS = [
  { title: "Identity", caption: "Role and purpose" },
  { title: "Runtime", caption: "Model and prompts" },
  { title: "Authority", caption: "Rules and tools" },
  { title: "Contract", caption: "Scope and A2A" },
  { title: "Compile", caption: "Review and publish" },
] as const;

const PATTERNS: Array<{
  id: AgentWizardPattern;
  label: string;
  roleCandidates: string[];
}> = [
  { id: "blank", label: "Blank", roleCandidates: [] },
  {
    id: "builder",
    label: "Builder",
    roleCandidates: ["software_engineer", "full_stack_engineer"],
  },
  {
    id: "reviewer",
    label: "Reviewer",
    roleCandidates: ["design_critic", "qa_engineer", "security_engineer"],
  },
  { id: "operator", label: "Operator", roleCandidates: ["coo", "chief_operator"] },
];

const STEP_FIELDS: Array<Array<AgentWizardIssue["field"]>> = [
  ["name", "agentKey", "roleKey"],
  ["systemPrompt", "jobPrompt", "runtime"],
  ["rulesText"],
  [
    "artifactType",
    "maxSteps",
    "maxCalls",
    "timeoutSeconds",
    "maxRetries",
  ],
  [],
];

function displayKey(value: string): string {
  return value.replaceAll("_", " ");
}

function agentInitials(name: string): string {
  const initials = name
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();
  return initials || "∷";
}

export function AgentCreationWizard({
  projectId,
  actor,
  roles,
  skills,
  onClose,
}: WizardProps) {
  const [draft, setDraft] = useState(() =>
    createInitialAgentWizardDraft(roles[0]?.roleKey ?? "", actor),
  );
  // The project's registered MCP servers, so the Authority step offers real servers to GRANT
  // instead of a free-text "integrations" box nothing read.
  const [servers, setServers] = useState<GrantableServer[]>([]);
  const [serversFailed, setServersFailed] = useState("");
  useEffect(() => {
    let alive = true;
    void loadGrantable(projectId).then((r) => {
      if (!alive) return;
      if (r.ok) setServers(r.data.mcp_servers); else setServersFailed(r.error);
    });
    return () => { alive = false; };
  }, [projectId]);
  const [step, setStep] = useState(0);
  const [furthestStep, setFurthestStep] = useState(0);
  const [agentKeyEdited, setAgentKeyEdited] = useState(false);
  const [issues, setIssues] = useState<AgentWizardIssue[]>([]);
  const [result, setResult] = useState<StudioActionResult>();
  const [completed, setCompleted] = useState<StudioActionResult>();
  const [pending, startTransition] = useTransition();

  const role = roles.find((item) => item.roleKey === draft.roleKey);
  const selectedSkills = skills.filter((skill) =>
    draft.requestedSkills.includes(skill.id),
  );
  const allIssues = useMemo(() => validateAgentWizardDraft(draft), [draft]);
  const fieldIssues = useMemo(
    () =>
      new Map(
        issues.map((issue) => [issue.field, issue.message] as const),
      ),
    [issues],
  );
  const outputSections = parseWizardList(draft.outputSectionsText);
  const publishTopics = parseWizardList(draft.a2aPublishText);
  const subscribeTopics = parseWizardList(draft.a2aSubscribeText);

  const update = <KeyName extends keyof AgentWizardDraft>(
    field: KeyName,
    value: AgentWizardDraft[KeyName],
  ) => {
    setDraft((current) => ({ ...current, [field]: value }));
    setIssues((current) => current.filter((issue) => issue.field !== field));
    setResult(undefined);
  };

  const choosePattern = (pattern: AgentWizardPattern) => {
    setDraft((current) => {
      const next = applyAgentWizardPattern(current, pattern);
      const candidates =
        PATTERNS.find((item) => item.id === pattern)?.roleCandidates ?? [];
      const matchedRole = roles.find((item) =>
        candidates.includes(item.roleKey),
      );
      return matchedRole ? { ...next, roleKey: matchedRole.roleKey } : next;
    });
    setIssues([]);
    setResult(undefined);
  };

  const goNext = () => {
    const nextIssues = validateAgentWizardDraft(draft).filter((issue) =>
      STEP_FIELDS[step]?.includes(issue.field),
    );
    if (nextIssues.length) {
      setIssues(nextIssues);
      return;
    }
    const nextStep = Math.min(STEPS.length - 1, step + 1);
    setStep(nextStep);
    setFurthestStep((current) => Math.max(current, nextStep));
    setIssues([]);
  };

  const submit = (publish: boolean) => {
    const nextIssues = validateAgentWizardDraft(draft);
    if (nextIssues.length) {
      const firstStep = STEP_FIELDS.findIndex((fields) =>
        nextIssues.some((issue) => fields.includes(issue.field)),
      );
      setIssues(nextIssues);
      setStep(firstStep >= 0 ? firstStep : 4);
      setResult({
        ok: false,
        message: `Resolve ${nextIssues.length} compile issue${nextIssues.length === 1 ? "" : "s"} before creating this agent.`,
      });
      return;
    }
    setResult(undefined);
    startTransition(async () => {
      const next = await callAction(createStudioAgent)(
        buildAgentWizardSubmission(projectId, draft, actor),
        publish,
      );
      setResult(next);
      if (next.ok) setCompleted(next);
    });
  };

  const numberUpdate = (
    field: "maxSteps" | "maxCalls" | "timeoutSeconds" | "maxRetries",
    value: string,
  ) => update(field, Number.parseInt(value || "0", 10));

  if (completed) {
    return (
      <div className="aw-backdrop">
        <section
          className="aw-dialog aw-complete"
          role="dialog"
          aria-modal="true"
          aria-labelledby="aw-complete-title"
          onKeyDown={(event) => {
            if (event.key === "Escape") onClose();
          }}
        >
          <span className="aw-complete-orb"><Check weight="bold" /></span>
          <span className="aw-kicker">Commissioning complete</span>
          <h2 id="aw-complete-title">{draft.name} is in the Studio</h2>
          <p>{completed.message}</p>
          <dl>
            <div>
              <dt>Definition</dt>
              <dd>{String(completed.item?.definition_id ?? "recorded")}</dd>
            </div>
            <div>
              <dt>Version</dt>
              <dd>{String(completed.item?.version_id ?? "recorded")}</dd>
            </div>
            <div>
              <dt>State</dt>
              <dd>{String(completed.item?.status ?? "draft")}</dd>
            </div>
          </dl>
          <button className="as-button primary" onClick={onClose} autoFocus>
            Return to Studio <ArrowRight />
          </button>
        </section>
      </div>
    );
  }

  return (
    <div className="aw-backdrop">
      <section
        className="aw-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="aw-title"
        onKeyDown={(event) => {
          if (event.key === "Escape" && !pending) onClose();
        }}
      >
        <header className="aw-header">
          <div>
            <span className="aw-kicker"><Sparkle weight="fill" /> Agent Studio / commission</span>
            <h2 id="aw-title">Create a governed agent</h2>
            <p>Define the mind, then make its authority machine-checkable.</p>
          </div>
          <button
            className="aw-close"
            onClick={onClose}
            disabled={pending}
            aria-label="Close agent creation wizard"
          >
            <X />
          </button>
        </header>

        <div className="aw-progress" aria-label="Agent creation progress">
          {STEPS.map((item, index) => (
            <button
              key={item.title}
              className={index === step ? "active" : index < step ? "complete" : ""}
              disabled={index > furthestStep || pending}
              onClick={() => {
                setStep(index);
                setIssues([]);
              }}
              aria-current={index === step ? "step" : undefined}
            >
              <span>{index < step ? <Check /> : String(index + 1).padStart(2, "0")}</span>
              <b>{item.title}</b>
              <small>{item.caption}</small>
            </button>
          ))}
        </div>

        <div className="aw-workspace">
          <form
            className="aw-form"
            onSubmit={(event) => event.preventDefault()}
          >
            <div className="aw-step" key={step}>
              {step === 0 && (
                <>
                  <StepHeading
                    index="01"
                    title="Give it a job, then an identity."
                    description="Start from a restrained operating pattern or define the agent from first principles."
                  />
                  <div className="aw-patterns" aria-label="Starter pattern">
                    {PATTERNS.map((pattern) => (
                      <button
                        type="button"
                        key={pattern.id}
                        className={draft.pattern === pattern.id ? "active" : ""}
                        onClick={() => choosePattern(pattern.id)}
                      >
                        {pattern.label}
                      </button>
                    ))}
                  </div>
                  <div className="aw-field-row">
                    <Field label="Agent name" error={fieldIssues.get("name")}>
                      <input
                        value={draft.name}
                        onChange={(event) => {
                          const name = event.target.value;
                          update("name", name);
                          if (!agentKeyEdited) {
                            update("agentKey", slugifyAgentKey(name));
                          }
                        }}
                        placeholder="Release Sentinel"
                        autoFocus
                      />
                    </Field>
                    <Field label="Agent key" error={fieldIssues.get("agentKey")}>
                      <div className="aw-prefixed-input">
                        <span>agent/</span>
                        <input
                          value={draft.agentKey}
                          onChange={(event) => {
                            setAgentKeyEdited(true);
                            update("agentKey", slugifyAgentKey(event.target.value));
                          }}
                          placeholder="release_sentinel"
                        />
                      </div>
                    </Field>
                  </div>
                  <Field label="Purpose">
                    <textarea
                      value={draft.description}
                      onChange={(event) => update("description", event.target.value)}
                      placeholder="What durable responsibility does this agent own?"
                      rows={3}
                    />
                  </Field>
                  <div className="aw-field-row">
                    <Field label="Authority role" error={fieldIssues.get("roleKey")}>
                      <select
                        value={draft.roleKey}
                        onChange={(event) => update("roleKey", event.target.value)}
                      >
                        <option value="">Choose an installed RoleSpec</option>
                        {/* MAKE ONE. Every option here was a role that already existed, so this
                            wizard could only ever produce an agent_definitions row pointing at
                            somebody else's role — and the Agents page, which lists `roles`,
                            would never show it. */}
                        <option value={NEW_ROLE}>+ A new role for this agent</option>
                        {roles.map((item) => (
                          <option key={item.id} value={item.roleKey}>
                            {item.displayName} · L{item.autonomyLevel}
                          </option>
                        ))}
                      </select>
                    </Field>
                    {draft.roleKey === NEW_ROLE ? (
                      <Field label="New role name">
                        <input
                          value={draft.newRoleName}
                          placeholder={draft.name || "Release Auditor"}
                          onChange={(event) => update("newRoleName", event.target.value)}
                        />
                      </Field>
                    ) : null}
                    <Field label="Definition owner">
                      <input
                        value={draft.owner}
                        onChange={(event) => update("owner", event.target.value)}
                        placeholder={actor}
                      />
                    </Field>
                  </div>
                </>
              )}

              {step === 1 && (
                <>
                  <StepHeading
                    index="02"
                    title="Choose how it thinks."
                    description="Deterministic agents are ready now. Live-model execution stays behind provider, budget, and policy gates."
                  />
                  <div className="aw-runtime-switch">
                    <button
                      type="button"
                      className={draft.executionMode === "deterministic_brief" ? "active" : ""}
                      onClick={() => update("executionMode", "deterministic_brief")}
                    >
                      <Cpu />
                      <span><b>Deterministic brief</b><small>Side-effect-free, reproducible runtime</small></span>
                    </button>
                    {/* NOT OFFERED AS IF IT WORKED. Every Studio run of a live_model version is
                        blocked in this release, so choosing it produced an agent whose every
                        run failed. Workflow steps given to the agent run on the project's
                        chat model regardless. */}
                    <button
                      type="button"
                      className={draft.executionMode === "live_model" ? "active" : ""}
                      disabled
                      title="Not available in this release"
                    >
                      <Sparkle />
                      <span><b>Live model</b><small>Not available in this release — workflow steps given to this agent already run on the project&apos;s chat model</small></span>
                    </button>
                  </div>
                  {draft.executionMode === "live_model" && (
                    <div className="aw-field-row">
                      <Field label="Selection policy" error={fieldIssues.get("runtime")}>
                        <select
                          value={draft.selectionMode}
                          onChange={(event) =>
                            update(
                              "selectionMode",
                              event.target.value as AgentWizardDraft["selectionMode"],
                            )
                          }
                        >
                          <option value="router">Policy router</option>
                          <option value="pinned">Pinned model</option>
                          <option value="local">Local provider only</option>
                        </select>
                      </Field>
                      {draft.selectionMode === "pinned" ? (
                        <Field label="Pinned model identifier" error={fieldIssues.get("runtime")}>
                          <input
                            value={draft.pinnedModel}
                            onChange={(event) => update("pinnedModel", event.target.value)}
                            placeholder="provider/model"
                          />
                        </Field>
                      ) : (
                        <Field label="Preferred capabilities" error={fieldIssues.get("runtime")}>
                          <input
                            value={draft.preferredCapabilitiesText}
                            onChange={(event) =>
                              update("preferredCapabilitiesText", event.target.value)
                            }
                            placeholder="reasoning, code, vision"
                          />
                        </Field>
                      )}
                    </div>
                  )}
                  <Field label="System prompt" error={fieldIssues.get("systemPrompt")}>
                    <textarea
                      value={draft.systemPrompt}
                      onChange={(event) => update("systemPrompt", event.target.value)}
                      placeholder="The durable identity and operating posture…"
                      rows={5}
                    />
                  </Field>
                  <Field label="Job prompt" error={fieldIssues.get("jobPrompt")}>
                    <textarea
                      value={draft.jobPrompt}
                      onChange={(event) => update("jobPrompt", event.target.value)}
                      placeholder="How to interpret and execute each assigned objective…"
                      rows={4}
                    />
                  </Field>
                  <details className="aw-advanced">
                    <summary>Optional developer and operator layers</summary>
                    <Field label="Developer prompt">
                      <textarea
                        value={draft.developerPrompt}
                        onChange={(event) => update("developerPrompt", event.target.value)}
                        rows={3}
                      />
                    </Field>
                    <Field label="Operator prompt">
                      <textarea
                        value={draft.operatorPrompt}
                        onChange={(event) => update("operatorPrompt", event.target.value)}
                        rows={3}
                      />
                    </Field>
                  </details>
                </>
              )}

              {step === 2 && (
                <>
                  <StepHeading
                    index="03"
                    title="Draw the authority envelope."
                    description="Rules constrain behavior. The skills and MCP servers ticked here are GIVEN to the agent when it is created — what it holds is what it can use."
                  />
                  <Field
                    label="Deterministic rules · one per line"
                    error={fieldIssues.get("rulesText")}
                  >
                    <textarea
                      value={draft.rulesText}
                      onChange={(event) => update("rulesText", event.target.value)}
                      placeholder={"Never bypass an approval gate.\nAttach evidence to completion claims."}
                      rows={6}
                    />
                  </Field>
                  <div className="aw-capability-head">
                    <div>
                      <b>Installed skills</b>
                      <span>{draft.requestedSkills.length} requested</span>
                    </div>
                    <small>Ticked skills are granted to the agent; publish then checks the profile against what it holds.</small>
                  </div>
                  <div className="aw-skill-list">
                    {skills.map((skill) => {
                      const selected = draft.requestedSkills.includes(skill.id);
                      return (
                        <label key={skill.id} className={selected ? "selected" : ""}>
                          <input
                            type="checkbox"
                            checked={selected}
                            onChange={() =>
                              update(
                                "requestedSkills",
                                selected
                                  ? draft.requestedSkills.filter((id) => id !== skill.id)
                                  : [...draft.requestedSkills, skill.id],
                              )
                            }
                          />
                          <span><Wrench /></span>
                          <div>
                            <b>{displayKey(skill.id)}</b>
                            <small>{skill.description}</small>
                          </div>
                          <em>R{skill.riskTier}</em>
                        </label>
                      );
                    })}
                    {skills.length === 0 && (
                      <p className="aw-empty-line">No installed skills are available to request.</p>
                    )}
                  </div>
                  <div className="aw-capability-head">
                    <div>
                      <b>MCP servers</b>
                      <span>{draft.mcpServers.length} given</span>
                    </div>
                    <small>Leave tools empty to give every tool the server offers.</small>
                  </div>
                  <div className="aw-skill-list aq-list" data-testid="aw-mcp-list">
                    {servers.map((server) => {
                      const chosen = draft.mcpServers.find((m) => m.serverId === server.mcp_server_id);
                      return (
                        <label key={server.mcp_server_id} className={chosen ? "selected" : ""}>
                          <input
                            type="checkbox"
                            checked={!!chosen}
                            data-testid={`aw-mcp-${server.mcp_server_id}`}
                            onChange={() =>
                              update(
                                "mcpServers",
                                chosen
                                  ? draft.mcpServers.filter((m) => m.serverId !== server.mcp_server_id)
                                  : [...draft.mcpServers,
                                     { serverId: server.mcp_server_id, name: server.server, tools: "" }],
                              )
                            }
                          />
                          <div>
                            <b>{server.server}</b>
                            <small>{server.description || server.url}{server.enabled ? "" : " · disabled in this project"}</small>
                            {chosen ? (
                              <input
                                className="aq-tools"
                                placeholder="Only these tools (comma-separated) — empty for all"
                                value={chosen.tools}
                                onClick={(event) => event.preventDefault()}
                                onChange={(event) =>
                                  update(
                                    "mcpServers",
                                    draft.mcpServers.map((m) =>
                                      m.serverId === server.mcp_server_id
                                        ? { ...m, tools: event.target.value } : m),
                                  )
                                }
                              />
                            ) : null}
                          </div>
                        </label>
                      );
                    })}
                    {!servers.length && (
                      <p className="aw-empty-line">
                        {serversFailed
                          ? `Could not read this project's MCP servers — ${serversFailed}.`
                          : "No MCP servers are registered. Register one on the Skills and MCP page to give an agent its tools."}
                      </p>
                    )}
                  </div>
                </>
              )}

              {step === 3 && (
                <>
                  <StepHeading
                    index="04"
                    title="Make the contract observable."
                    description="Bound data, runtime, output, and coordination so operators can predict and audit this agent."
                  />
                  <div className="aw-field-row">
                    <Field label="Data boundary">
                      <select
                        value={draft.dataBoundary}
                        onChange={(event) =>
                          update(
                            "dataBoundary",
                            event.target.value as AgentWizardDraft["dataBoundary"],
                          )
                        }
                      >
                        <option value="public">Public</option>
                        <option value="internal">Internal</option>
                        <option value="confidential">Confidential</option>
                        <option value="restricted">Restricted</option>
                      </select>
                    </Field>
                    <Field label="Artifact type" error={fieldIssues.get("artifactType")}>
                      <input
                        value={draft.artifactType}
                        onChange={(event) => update("artifactType", event.target.value)}
                        placeholder="implementation_plan"
                      />
                    </Field>
                  </div>
                  <Field label="Output sections · one per line">
                    <textarea
                      value={draft.outputSectionsText}
                      onChange={(event) => update("outputSectionsText", event.target.value)}
                      rows={4}
                    />
                  </Field>
                  <div className="aw-limit-grid">
                    <Field label="Max steps" error={fieldIssues.get("maxSteps")}>
                      <input
                        type="number"
                        min={1}
                        max={50}
                        value={draft.maxSteps}
                        onChange={(event) => numberUpdate("maxSteps", event.target.value)}
                      />
                    </Field>
                    <Field label="Max calls" error={fieldIssues.get("maxCalls")}>
                      <input
                        type="number"
                        min={1}
                        max={100}
                        value={draft.maxCalls}
                        onChange={(event) => numberUpdate("maxCalls", event.target.value)}
                      />
                    </Field>
                    <Field label="Timeout · sec" error={fieldIssues.get("timeoutSeconds")}>
                      <input
                        type="number"
                        min={5}
                        max={3600}
                        value={draft.timeoutSeconds}
                        onChange={(event) =>
                          numberUpdate("timeoutSeconds", event.target.value)
                        }
                      />
                    </Field>
                    <Field label="Retries" error={fieldIssues.get("maxRetries")}>
                      <input
                        type="number"
                        min={0}
                        max={10}
                        value={draft.maxRetries}
                        onChange={(event) => numberUpdate("maxRetries", event.target.value)}
                      />
                    </Field>
                  </div>
                  <div className="aw-a2a-grid">
                    <Field label="A2A publish topics">
                      <textarea
                        value={draft.a2aPublishText}
                        onChange={(event) => update("a2aPublishText", event.target.value)}
                        rows={4}
                      />
                    </Field>
                    <Field label="A2A subscribe topics">
                      <textarea
                        value={draft.a2aSubscribeText}
                        onChange={(event) => update("a2aSubscribeText", event.target.value)}
                        rows={4}
                      />
                    </Field>
                  </div>
                </>
              )}

              {step === 4 && (
                <>
                  <StepHeading
                    index="05"
                    title="Compile the agent."
                    description="BOSS creates a stable definition and immutable draft. Publishing happens only after the authority envelope validates."
                  />
                  <div className="aw-compile-status">
                    <span className={allIssues.length ? "bad" : "ok"}>
                      {allIssues.length ? <Warning /> : <CheckCircle />}
                    </span>
                    <div>
                      <b>
                        {allIssues.length
                          ? `${allIssues.length} compile issue${allIssues.length === 1 ? "" : "s"}`
                          : "Blueprint ready to commission"}
                      </b>
                      <p>
                        {allIssues.length
                          ? "Return to the highlighted stage and complete the missing contract."
                          : "The server will still verify RoleSpec permissions, installed capabilities, and all runtime bounds."}
                      </p>
                    </div>
                  </div>
                  {allIssues.length > 0 && (
                    <ul className="aw-issue-list">
                      {allIssues.map((issue) => (
                        <li key={`${issue.field}:${issue.message}`}>{issue.message}</li>
                      ))}
                    </ul>
                  )}
                  <div className="aw-review-ledger">
                    <ReviewLine label="Identity" value={`${draft.name || "Unnamed"} · agent/${slugifyAgentKey(draft.agentKey) || "missing"}`} />
                    <ReviewLine label="Authority" value={`${role?.displayName ?? "No role"} · ${draft.dataBoundary}`} />
                    <ReviewLine label="Runtime" value={`${displayKey(draft.executionMode)} · ${displayKey(draft.selectionMode)}`} />
                    <ReviewLine label="Capabilities" value={`${draft.requestedSkills.length} skills · ${draft.mcpServers.length} MCP servers`} />
                    <ReviewLine label="A2A" value={`${publishTopics.length} publish · ${subscribeTopics.length} subscribe`} />
                    <ReviewLine label="Output" value={`${draft.artifactType || "missing"} · ${outputSections.length} sections`} />
                  </div>
                  <div className="aw-publish-note">
                    <ShieldCheck />
                    <p>
                      Publish activates version 1 only after deterministic validation. Live model
                      runs separately require an eligible provider, policy approval, and a budget reservation.
                    </p>
                  </div>
                  {result && (
                    <div
                      className={`as-action-result ${result.ok ? "ok" : "bad"}`}
                      role="status"
                    >
                      {result.ok ? <CheckCircle /> : <Warning />}
                      <span>{result.message}</span>
                    </div>
                  )}
                </>
              )}
            </div>
          </form>

          <aside className="aw-blueprint" aria-label="Live agent blueprint">
            <div className="aw-blueprint-head">
              <span>Live blueprint</span>
              <small>{allIssues.length ? `${allIssues.length} open` : "ready"}</small>
            </div>
            <div className="aw-agent-mark">
              <span>{agentInitials(draft.name)}</span>
              <div>
                <b>{draft.name || "Untitled agent"}</b>
                <small>agent/{slugifyAgentKey(draft.agentKey) || "pending"}</small>
              </div>
            </div>
            <div className="aw-blueprint-chain">
              <BlueprintNode
                icon={<ShieldCheck />}
                label="Authority"
                value={role?.displayName ?? "Choose a RoleSpec"}
                meta={role ? `Autonomy L${role.autonomyLevel} · ${draft.dataBoundary}` : "Unbound"}
              />
              <span className="aw-chain-line" />
              <BlueprintNode
                icon={<Cpu />}
                label="Runtime"
                value={displayKey(draft.executionMode)}
                meta={draft.executionMode === "live_model" ? displayKey(draft.selectionMode) : "Reproducible"}
              />
              <span className="aw-chain-line" />
              <BlueprintNode
                icon={<Wrench />}
                label="Capability"
                value={`${selectedSkills.length} skill${selectedSkills.length === 1 ? "" : "s"} given`}
                meta={`${draft.mcpServers.length} MCP servers`}
              />
              <span className="aw-chain-line" />
              <BlueprintNode
                icon={<PaperPlaneTilt />}
                label="Coordination"
                value={`${publishTopics.length} out · ${subscribeTopics.length} in`}
                meta="Durable A2A policy"
              />
            </div>
            <div className="aw-envelope">
              <span><LockKey /> Authority envelope</span>
              <dl>
                <div><dt>Rules</dt><dd>{parseWizardLines(draft.rulesText).length}</dd></div>
                <div><dt>Steps</dt><dd>{draft.maxSteps}</dd></div>
                <div><dt>Calls</dt><dd>{draft.maxCalls}</dd></div>
                <div><dt>Timeout</dt><dd>{draft.timeoutSeconds}s</dd></div>
              </dl>
            </div>
            <div className="aw-blueprint-proof">
              <Key />
              <span>No credentials enter this blueprint.</span>
            </div>
          </aside>
        </div>

        <footer className="aw-footer">
          <span>
            Step {step + 1} of {STEPS.length} · {STEPS[step].caption}
          </span>
          <div>
            {step > 0 && (
              <button
                className="as-button secondary"
                onClick={() => {
                  setStep((current) => current - 1);
                  setIssues([]);
                }}
                disabled={pending}
              >
                <ArrowLeft /> Back
              </button>
            )}
            {step < STEPS.length - 1 ? (
              <button className="as-button primary" onClick={goNext}>
                Continue <ArrowRight />
              </button>
            ) : (
              <>
                <button
                  className="as-button secondary"
                  onClick={() => submit(false)}
                  disabled={pending || allIssues.length > 0}
                >
                  {pending ? "Creating…" : "Save draft"}
                </button>
                <button
                  className="as-button primary"
                  onClick={() => submit(true)}
                  disabled={pending || allIssues.length > 0}
                >
                  <CirclesThreePlus />
                  {pending ? "Compiling…" : "Validate + publish"}
                </button>
              </>
            )}
          </div>
        </footer>
      </section>
    </div>
  );
}

function StepHeading({
  index,
  title,
  description,
}: {
  index: string;
  title: string;
  description: string;
}) {
  return (
    <div className="aw-step-heading">
      <span>{index}</span>
      <div>
        <h3>{title}</h3>
        <p>{description}</p>
      </div>
    </div>
  );
}

function Field({
  label,
  error,
  children,
}: {
  label: string;
  error?: string;
  children: ReactNode;
}) {
  return (
    <label className={`aw-field ${error ? "error" : ""}`}>
      <span>{label}</span>
      {children}
      {error && <small role="alert">{error}</small>}
    </label>
  );
}

function BlueprintNode({
  icon,
  label,
  value,
  meta,
}: {
  icon: ReactNode;
  label: string;
  value: string;
  meta: string;
}) {
  return (
    <div className="aw-blueprint-node">
      <span>{icon}</span>
      <div>
        <small>{label}</small>
        <b>{value}</b>
        <em>{meta}</em>
      </div>
    </div>
  );
}

function ReviewLine({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span>{label}</span>
      <b>{value}</b>
      <Check />
    </div>
  );
}
