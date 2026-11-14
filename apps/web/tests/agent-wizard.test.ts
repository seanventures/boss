import { describe, expect, it } from "vitest";

import {
  adoptCreatedAgent,
  applyAgentWizardPattern,
  buildAgentWizardSubmission,
  createInitialAgentWizardDraft,
  parseWizardList,
  parseWizardLines,
  slugifyAgentKey,
  validateAgentWizardDraft,
  NEW_ROLE,
} from "../lib/agent-wizard";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/** New agent (app/agent-quick-create.tsx) is the one door; the wizard's runtime fields are its
 *  Advanced section and its submission is still built by lib/agent-wizard (tested below). */
const DIALOG = () => readFileSync(resolve(__dirname, "../app/agent-quick-create.tsx"), "utf8");

describe("Agent creation (New agent, with the wizard's submission)", () => {
  it("offers the starter set explicitly, saying it starts nothing", () => {
    const actions = readFileSync(resolve(__dirname, "../app/agent-studio-actions.ts"), "utf8");
    expect(DIALOG()).toContain('data-testid="aq-seed"');
    expect(DIALOG()).toContain("Adds the reviewed starter agents and panels. Starts nothing, spends nothing.");
    expect(DIALOG()).toContain('callAction(seedStudioCatalog, undefined, "agent:create")(projectId, actor)');
    expect(actions).toContain('post("/v1/agent-studio/catalog/seed"');
    expect(actions).toContain("role_review authority");
  });

  it("normalizes human names and deduplicates reference lists", () => {
    expect(slugifyAgentKey("  Release & Risk Sentinel  ")).toBe(
      "release_risk_sentinel",
    );
    expect(parseWizardList("agent.status, artifact.ready\nagent.status")).toEqual([
      "agent.status",
      "artifact.ready",
    ]);
    expect(parseWizardLines("Keep policy, budget, and receipts intact.\nProve the result.")).toEqual([
      "Keep policy, budget, and receipts intact.",
      "Prove the result.",
    ]);
  });

  it("applies an operating pattern without replacing chosen identity", () => {
    const draft = {
      ...createInitialAgentWizardDraft("software_engineer"),
      name: "Release Sentinel",
      agentKey: "release_sentinel",
    };

    const patterned = applyAgentWizardPattern(draft, "reviewer");

    expect(patterned.name).toBe("Release Sentinel");
    expect(patterned.agentKey).toBe("release_sentinel");
    expect(patterned.pattern).toBe("reviewer");
    expect(patterned.rulesText).toContain("reproducible evidence");
    expect(patterned.artifactType).toBe("review_report");
  });

  // REWRITTEN: this test asserted `actor: "vp_agents"` and a free-text `requested_integrations`
  // list. Both were the defect — the literal actor 403'd on every authed install (a 'ceo' key may
  // not act as 'vp_agents'), and the integrations field was text nothing read. The submission
  // now carries the signed-in actor everywhere and GRANTS the chosen skills and MCP servers.
  it("compiles the form into canonical definition and immutable version payloads", () => {
    const draft = {
      ...applyAgentWizardPattern(
        createInitialAgentWizardDraft("software_engineer", "executive_console"),
        "builder",
      ),
      name: "Backend Builder",
      agentKey: "backend-builder",
      selectionMode: "pinned" as const,
      pinnedModel: "openai/gpt-current",
      preferredCapabilitiesText: "code, reasoning",
      requestedSkills: ["model.generate_code", "github.create_pr_sandbox"],
      mcpServers: [{ serverId: "mcp_1", name: "mattermost", tools: "post, read_channel" }],
    };

    const payload = buildAgentWizardSubmission("boss-dogfood", draft, "executive_console");

    expect(payload.actor).toBe("executive_console");
    expect(payload.definition).toMatchObject({
      project_id: "boss-dogfood",
      agent_key: "backend_builder",
      name: "Backend Builder",
      owner: "executive_console",
      actor: "executive_console",
    });
    expect(payload.version).toMatchObject({
      project_id: "boss-dogfood",
      role_key: "software_engineer",
      model_policy: {
        execution_mode: "deterministic_brief",
        selection_mode: "pinned",
        pinned_model: "openai/gpt-current",
        preferred_capabilities: ["code", "reasoning"],
      },
      requested_skills: ["github.create_pr_sandbox", "model.generate_code"],
      requested_integrations: ["mattermost"],
      scopes: { project: "current", data_boundary: "internal" },
      provenance: { source: "agent_studio_wizard", pattern: "builder" },
      actor: "executive_console",
    });
    expect(payload.version.output_contract).toMatchObject({
      artifact_type: "implementation_plan",
    });
    // An EXISTING agent is given what the profile uses that it does not hold — the grant.
    expect(payload.agent).toEqual({
      roleKey: "software_engineer",
      grant: {
        project_id: "boss-dogfood",
        actor: "executive_console",
        grant_skills: ["github.create_pr_sandbox", "model.generate_code"],
        grant_mcp_servers: [{ server_id: "mcp_1", tools: ["post", "read_channel"] }],
      },
    });
  });

  it("a new agent is CREATED holding what was ticked, never registered bare", () => {
    // The wizard registered a role with the skills written into spec.allowed_skills, which the
    // runtime never reads, so the agent it made held nothing and was refused by every step.
    const draft = {
      ...createInitialAgentWizardDraft(NEW_ROLE, "ceo"),
      name: "Release Auditor",
      agentKey: "release_auditor",
      description: "Checks every release against the change log.",
      requestedSkills: ["model.generate_text"],
      mcpServers: [{ serverId: "mcp_9", name: "github", tools: "" }],
    };
    const payload = buildAgentWizardSubmission("p", draft, "ceo");
    expect(payload.agent).toEqual({
      roleKey: "release_auditor",
      create: {
        project_id: "p",
        actor: "ceo",
        role_key: "release_auditor",
        name: "Release Auditor",
        purpose: "Checks every release against the change log.",
        skills: ["model.generate_text"],
        mcp_servers: [{ server_id: "mcp_9", tools: [] }],
        // Added in review round 1: the wizard's rules are the agent's must_not boundaries.
        boundaries: { must: [], must_not: [] },
      },
    });
    expect(payload.version.role_key).toBe("release_auditor");
  });

  it("the wizard's rules become the new agent's must_not boundaries", () => {
    // The old role registration wrote the rules to spec.boundaries.must_not; the first cut of
    // POST /v1/agents dropped them, so "never bypass policy" bound nothing.
    const draft = {
      ...createInitialAgentWizardDraft(NEW_ROLE, "ceo"),
      name: "Release Auditor", agentKey: "release_auditor",
      rulesText: "Never bypass policy.\nNever merge your own change.",
    };
    const create = buildAgentWizardSubmission("p", draft, "ceo").agent.create;
    expect(create?.boundaries).toEqual({
      must: [], must_not: ["Never bypass policy.", "Never merge your own change."],
    });
  });

  it("a retry after the profile failed targets the agent it already made", () => {
    // POST /v1/agents refuses an existing key, and "+ A new role" stayed selected, so pressing
    // Create again after a profile failure 409'd on the agent this same wizard had just made.
    const draft = {
      ...createInitialAgentWizardDraft(NEW_ROLE, "ceo"),
      name: "Release Auditor", agentKey: "taken_key", newRoleName: "Release Auditor",
      requestedSkills: ["model.generate_text"],
    };
    const retry = buildAgentWizardSubmission("p", adoptCreatedAgent(draft, "release_auditor"), "ceo");
    expect(retry.agent.create).toBeUndefined();
    expect(retry.agent.roleKey).toBe("release_auditor");
    expect(retry.agent.grant?.grant_skills).toEqual(["model.generate_text"]);
    expect(retry.version.role_key).toBe("release_auditor");
    const actions = readFileSync(resolve(__dirname, "../app/agent-studio-actions.ts"), "utf8");
    expect(actions).toContain("agent_created: submission.agent.roleKey");
    // New agent keeps the agent the failed attempt made, and the next press adopts it.
    expect(DIALOG()).toContain("const createdKey = result.item?.agent_created;");
    expect(DIALOG()).toContain("made === key ? adoptCreatedAgent(fresh, made) : fresh");
  });

  it("live-model Studio runs are allowed: every Studio run is a governed agent run", () => {
    // AGENT_PLATFORM D13: the templated deterministic brief is retired and live_model is no
    // longer blocked, so the wizard no longer refuses it.
    const issues = validateAgentWizardDraft({
      ...createInitialAgentWizardDraft("software_engineer", "ceo"),
      executionMode: "live_model" as const,
      preferredCapabilitiesText: "reasoning",
    });
    const runtime = issues.filter((i) => i.field === "runtime").map((i) => i.message).join(" ");
    expect(runtime).not.toContain("not available in this release");
  });

  it("blocks incomplete and unbounded blueprints before server creation", () => {
    const draft = {
      ...createInitialAgentWizardDraft(),
      executionMode: "live_model" as const,
      maxSteps: 0,
      timeoutSeconds: 4,
    };

    const issues = validateAgentWizardDraft(draft);

    expect(issues.map((issue) => issue.field)).toEqual(
      expect.arrayContaining([
        "name",
        "agentKey",
        "roleKey",
        "systemPrompt",
        "jobPrompt",
        "rulesText",
        "runtime",
        "maxSteps",
        "timeoutSeconds",
      ]),
    );
  });
});
