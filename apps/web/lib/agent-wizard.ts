export type AgentWizardPattern = "blank" | "builder" | "reviewer" | "operator";

export type AgentWizardDraft = {
  pattern: AgentWizardPattern;
  name: string;
  agentKey: string;
  description: string;
  owner: string;
  roleKey: string;
  /** Only used when roleKey is NEW_ROLE: the display name the new role gets. */
  newRoleName: string;
  executionMode: "deterministic_brief" | "live_model";
  selectionMode: "router" | "pinned" | "local";
  preferredCapabilitiesText: string;
  pinnedModel: string;
  systemPrompt: string;
  jobPrompt: string;
  developerPrompt: string;
  operatorPrompt: string;
  rulesText: string;
  requestedSkills: string[];
  /** MCP servers this agent is GIVEN (role_mcp_servers), with an optional comma-separated tool
   *  allowlist each. It was a free-text "integrations" field nothing ever read or enforced. */
  mcpServers: Array<{ serverId: string; name: string; tools: string }>;
  dataBoundary: "public" | "internal" | "confidential" | "restricted";
  maxSteps: number;
  maxCalls: number;
  timeoutSeconds: number;
  maxRetries: number;
  artifactType: string;
  outputSectionsText: string;
  a2aPublishText: string;
  a2aSubscribeText: string;
};

export type AgentWizardIssue = {
  field: keyof AgentWizardDraft | "runtime";
  message: string;
};

export type AgentWizardSubmission = {
  /** Who is making this agent: the signed-in actor, never a literal. Every write carries it. */
  actor: string;
  definition: Record<string, unknown>;
  version: Record<string, unknown>;
  /** THE AGENT ITSELF, written before its Studio profile (the one agent model: a role, what it
   *  holds, and optionally a profile).
   *
   *  `create` makes a new agent through POST /v1/agents with its skills and MCP servers GRANTED
   *  in the same transaction — the wizard used to register a bare role and write the chosen
   *  skills into two lists the runtime never reads, so the agent it made held nothing. `grant`
   *  gives an existing agent whatever the profile uses that it does not already hold. */
  agent: {
    roleKey: string;
    create?: Record<string, unknown>;
    grant?: Record<string, unknown>;
  };
};

/** The sentinel for "I am making a new role", not picking one that exists. */
export const NEW_ROLE = "__new__";

type AgentWizardPreset = Pick<
  AgentWizardDraft,
  | "description"
  | "systemPrompt"
  | "jobPrompt"
  | "rulesText"
  | "artifactType"
  | "outputSectionsText"
  | "a2aPublishText"
  | "a2aSubscribeText"
>;

const PRESETS: Record<Exclude<AgentWizardPattern, "blank">, AgentWizardPreset> = {
  builder: {
    description: "Builds a bounded implementation plan and produces reviewable change artifacts.",
    systemPrompt:
      "You are a disciplined implementation agent. Work from verified requirements, preserve existing behavior, and expose every uncertainty.",
    jobPrompt:
      "Turn the assigned objective into the smallest safe implementation with tests, evidence, and a clear handoff.",
    rulesText:
      "Never bypass policy, budget, or provider adapter boundaries.\nMake the smallest reviewable change.\nAttach validation evidence to every completion claim.",
    artifactType: "implementation_plan",
    outputSectionsText: "summary\nchanges\ntests\nrisks\nhandoff",
    a2aPublishText: "agent.status\nartifact.ready\njob.blocked",
    a2aSubscribeText: "job.assigned\nreview.feedback\npolicy.changed",
  },
  reviewer: {
    description: "Challenges claims, verifies evidence, and returns prioritized review findings.",
    systemPrompt:
      "You are an adversarial but constructive reviewer. Ground every finding in observable evidence and preserve dissent.",
    jobPrompt:
      "Review the assigned artifact, try to refute its claims, and return only actionable findings with severity and evidence.",
    rulesText:
      "Do not approve claims without reproducible evidence.\nSeparate observed defects from inferred risks.\nPrefer a small number of high-confidence findings.",
    artifactType: "review_report",
    outputSectionsText: "verdict\nfindings\nevidence\nresidual_risk",
    a2aPublishText: "review.completed\nreview.blocked",
    a2aSubscribeText: "artifact.ready\nreview.requested",
  },
  operator: {
    description: "Coordinates bounded jobs, dependencies, escalations, and evidence-backed status.",
    systemPrompt:
      "You are the job captain. Coordinate through governed workflows and A2A messages while keeping state, ownership, and blockers explicit.",
    jobPrompt:
      "Drive the assigned objective to its next verified state, coordinating specialists and escalating decisions that exceed authority.",
    rulesText:
      "Never hide a failed or stalled job.\nEscalate decisions outside the authority envelope.\nTreat Postgres records and receipts as canonical truth.",
    artifactType: "operations_brief",
    outputSectionsText: "status\nowners\ndependencies\nblockers\nnext_actions",
    a2aPublishText: "job.assigned\njob.status\ndecision.requested",
    a2aSubscribeText: "agent.status\nartifact.ready\njob.blocked\ndecision.resolved",
  },
};

export function parseWizardList(value: string): string[] {
  return Array.from(
    new Set(
      value
        .split(/[\n,]/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  );
}

export function parseWizardLines(value: string): string[] {
  return Array.from(
    new Set(
      value
        .split(/\n/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  );
}

export function slugifyAgentKey(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 80);
}

export function createInitialAgentWizardDraft(
  roleKey = "",
  owner = "",
): AgentWizardDraft {
  return {
    pattern: "blank",
    name: "",
    agentKey: "",
    description: "",
    // The definition's owner starts as whoever is making it (it was the literal 'vp_agents').
    owner,
    roleKey,
    newRoleName: "",
    executionMode: "deterministic_brief",
    selectionMode: "router",
    preferredCapabilitiesText: "",
    pinnedModel: "",
    systemPrompt: "",
    jobPrompt: "",
    developerPrompt: "",
    operatorPrompt: "",
    rulesText: "",
    requestedSkills: [],
    mcpServers: [],
    dataBoundary: "internal",
    maxSteps: 8,
    maxCalls: 8,
    timeoutSeconds: 300,
    maxRetries: 2,
    artifactType: "agent_brief",
    outputSectionsText: "summary\nevidence\nrisks\nnext_actions",
    a2aPublishText: "agent.status\nartifact.ready",
    a2aSubscribeText: "job.assigned\nreview.feedback",
  };
}

export function applyAgentWizardPattern(
  draft: AgentWizardDraft,
  pattern: AgentWizardPattern,
): AgentWizardDraft {
  if (pattern === "blank") {
    return {
      ...createInitialAgentWizardDraft(draft.roleKey, draft.owner),
      name: draft.name, agentKey: draft.agentKey,
    };
  }
  return {
    ...draft,
    ...PRESETS[pattern],
    pattern,
  };
}

export function validateAgentWizardDraft(
  draft: AgentWizardDraft,
): AgentWizardIssue[] {
  const issues: AgentWizardIssue[] = [];
  if (!draft.name.trim()) issues.push({ field: "name", message: "Agent name is required." });
  if (!slugifyAgentKey(draft.agentKey)) {
    issues.push({ field: "agentKey", message: "A machine-readable agent key is required." });
  }
  if (!draft.roleKey.trim()) {
    issues.push({ field: "roleKey", message: "Choose an installed RoleSpec." });
  }
  if (!draft.systemPrompt.trim()) {
    issues.push({ field: "systemPrompt", message: "System prompt is required." });
  }
  if (!draft.jobPrompt.trim()) {
    issues.push({ field: "jobPrompt", message: "Job prompt is required." });
  }
  if (parseWizardLines(draft.rulesText).length === 0) {
    issues.push({ field: "rulesText", message: "Add at least one deterministic rule." });
  }
  if (!draft.artifactType.trim()) {
    issues.push({ field: "artifactType", message: "Output artifact type is required." });
  }
  if (draft.executionMode === "live_model") {
    // SAID BEFORE PUBLISH, NOT AFTER. Every Studio run of a live_model version is blocked in
    // this release (agent_studio.start_agent_run: live_model_runtime_not_enabled), so offering
    // it produced an agent whose every run failed. A workflow step runs the agent on the
    // project's chat model whatever this says; that is where live work happens.
    issues.push({
      field: "runtime",
      message: "Live-model Studio runs are not available in this release — choose Deterministic "
        + "brief. Workflow steps given to this agent still run on the project's chat model.",
    });
  }
  if (
    draft.executionMode === "live_model" &&
    parseWizardList(draft.preferredCapabilitiesText).length === 0
  ) {
    issues.push({
      field: "runtime",
      message: "Live model agents need at least one preferred model capability.",
    });
  }
  if (draft.selectionMode === "pinned" && !draft.pinnedModel.trim()) {
    issues.push({
      field: "runtime",
      message: "Pinned selection requires a model identifier.",
    });
  }
  if (draft.maxSteps < 1 || draft.maxSteps > 50) {
    issues.push({ field: "maxSteps", message: "Max steps must be between 1 and 50." });
  }
  if (draft.maxCalls < 1 || draft.maxCalls > 100) {
    issues.push({ field: "maxCalls", message: "Max calls must be between 1 and 100." });
  }
  if (draft.timeoutSeconds < 5 || draft.timeoutSeconds > 3600) {
    issues.push({
      field: "timeoutSeconds",
      message: "Timeout must be between 5 and 3,600 seconds.",
    });
  }
  if (draft.maxRetries < 0 || draft.maxRetries > 10) {
    issues.push({ field: "maxRetries", message: "Retries must be between 0 and 10." });
  }
  return issues;
}

export function buildAgentWizardSubmission(
  projectId: string,
  draft: AgentWizardDraft,
  actor: string,
): AgentWizardSubmission {
  const preferredCapabilities = parseWizardList(draft.preferredCapabilitiesText);
  const modelPolicy: Record<string, unknown> = {
    execution_mode: draft.executionMode,
    selection_mode: draft.selectionMode,
    preferred_capabilities: preferredCapabilities,
  };
  if (draft.selectionMode === "pinned" && draft.pinnedModel.trim()) {
    modelPolicy.pinned_model = draft.pinnedModel.trim();
  }

  const makingRole = draft.roleKey === NEW_ROLE;
  const newRoleKey = slugifyAgentKey(draft.newRoleName || draft.name).replace(/-/g, "_");
  const roleKey = makingRole ? newRoleKey : draft.roleKey;
  const skills = [...draft.requestedSkills].sort();
  const mcpServers = draft.mcpServers.map((m) => ({
    server_id: m.serverId,
    tools: parseWizardList(m.tools),
  }));

  // THE GRANT IS THE PERMISSION. A new agent is created holding exactly what was ticked; an
  // existing one is given anything ticked that it does not hold. Either way the Studio
  // profile's `requested_skills` below is what the agent now HOLDS, so publish validation
  // (which accepts held skills) and the runtime (which reads only the grant) agree.
  const agent: AgentWizardSubmission["agent"] = makingRole
    ? {
      roleKey,
      create: {
        project_id: projectId,
        actor,
        role_key: newRoleKey,
        name: (draft.newRoleName || draft.name).trim(),
        purpose: draft.description.trim(),
        skills,
        mcp_servers: mcpServers,
      },
    }
    : {
      roleKey,
      ...(skills.length || mcpServers.length ? {
        grant: {
          project_id: projectId,
          actor,
          grant_skills: skills,
          grant_mcp_servers: mcpServers,
        },
      } : {}),
    };

  return {
    actor,
    agent,
    definition: {
      project_id: projectId,
      agent_key: slugifyAgentKey(draft.agentKey),
      name: draft.name.trim(),
      description: draft.description.trim(),
      owner: draft.owner.trim() || actor,
      actor,
    },
    version: {
      project_id: projectId,
      role_key: roleKey,
      model_policy: modelPolicy,
      prompts: {
        system: draft.systemPrompt.trim(),
        developer: draft.developerPrompt.trim(),
        job: draft.jobPrompt.trim(),
        operator: draft.operatorPrompt.trim(),
      },
      rules: parseWizardLines(draft.rulesText),
      requested_skills: skills,
      // The servers' names, for the profile's record. What the agent may USE is the grant.
      requested_integrations: draft.mcpServers.map((m) => m.name),
      scopes: {
        project: "current",
        data_boundary: draft.dataBoundary,
      },
      limits: {
        max_steps: draft.maxSteps,
        max_calls: draft.maxCalls,
        timeout_seconds: draft.timeoutSeconds,
        max_retries: draft.maxRetries,
      },
      output_contract: {
        artifact_type: draft.artifactType.trim(),
        sections: parseWizardList(draft.outputSectionsText),
      },
      a2a_policy: {
        publish: parseWizardList(draft.a2aPublishText),
        subscribe: parseWizardList(draft.a2aSubscribeText),
      },
      provenance: {
        source: "agent_studio_wizard",
        pattern: draft.pattern,
      },
      actor,
    },
  };
}
