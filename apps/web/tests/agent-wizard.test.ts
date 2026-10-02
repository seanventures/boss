import { describe, expect, it } from "vitest";

import {
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

describe("Agent Studio creation wizard", () => {
  it("offers an explicit starter catalog when a project library is empty", () => {
    const studio = readFileSync(resolve(__dirname, "../app/agent-studio-ui.tsx"), "utf8");
    const actions = readFileSync(resolve(__dirname, "../app/agent-studio-actions.ts"), "utf8");

    expect(studio).toContain("Install BOSS starter catalog");
    expect(studio).toContain("The reviewed starter catalog adds agent definitions, panels, and workflows to this project.");
    expect(studio).toContain("as-first-run");
    expect(studio).toContain("Add your first agents");
    expect(studio).toContain("It does not start a run or spend budget.");
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
      },
    });
    expect(payload.version.role_key).toBe("release_auditor");
  });

  it("live-model Studio runs are refused before publish, with what to do instead", () => {
    // Every Studio run of a live_model version is blocked in this release, so offering it
    // produced an agent whose every run failed.
    const issues = validateAgentWizardDraft({
      ...createInitialAgentWizardDraft("software_engineer", "ceo"),
      executionMode: "live_model" as const,
      preferredCapabilitiesText: "reasoning",
    });
    const runtime = issues.filter((i) => i.field === "runtime").map((i) => i.message).join(" ");
    expect(runtime).toContain("not available in this release");
    expect(runtime).toContain("Deterministic brief");
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
