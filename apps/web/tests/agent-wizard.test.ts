import { describe, expect, it } from "vitest";

import {
  applyAgentWizardPattern,
  buildAgentWizardSubmission,
  createInitialAgentWizardDraft,
  parseWizardList,
  parseWizardLines,
  slugifyAgentKey,
  validateAgentWizardDraft,
} from "../lib/agent-wizard";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

describe("Agent Studio creation wizard", () => {
  it("offers an explicit starter catalog when a project library is empty", () => {
    const studio = readFileSync(resolve(__dirname, "../app/agent-studio-ui.tsx"), "utf8");
    const actions = readFileSync(resolve(__dirname, "../app/agent-studio-actions.ts"), "utf8");

    expect(studio).toContain("Install BOSS starter catalog");
    expect(studio).toContain("The reviewed starter catalog is project-scoped and idempotent.");
    expect(studio).toContain("as-first-run");
    expect(studio).toContain("This project has no governed agents yet");
    expect(studio).toContain("POST /v1/agent-studio/catalog/seed");
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

  it("compiles the form into canonical definition and immutable version payloads", () => {
    const draft = {
      ...applyAgentWizardPattern(
        createInitialAgentWizardDraft("software_engineer"),
        "builder",
      ),
      name: "Backend Builder",
      agentKey: "backend-builder",
      executionMode: "live_model" as const,
      selectionMode: "pinned" as const,
      pinnedModel: "openai/gpt-current",
      preferredCapabilitiesText: "code, reasoning",
      requestedSkills: ["github.create_pr_sandbox", "model.generate_code"],
      requestedIntegrationsText: "github\nplane",
    };

    const payload = buildAgentWizardSubmission("boss-dogfood", draft);

    expect(payload.definition).toMatchObject({
      project_id: "boss-dogfood",
      agent_key: "backend_builder",
      name: "Backend Builder",
      actor: "vp_agents",
    });
    expect(payload.version).toMatchObject({
      project_id: "boss-dogfood",
      role_key: "software_engineer",
      model_policy: {
        execution_mode: "live_model",
        selection_mode: "pinned",
        pinned_model: "openai/gpt-current",
        preferred_capabilities: ["code", "reasoning"],
      },
      requested_skills: ["github.create_pr_sandbox", "model.generate_code"],
      requested_integrations: ["github", "plane"],
      scopes: { project: "current", data_boundary: "internal" },
      provenance: { source: "agent_studio_wizard", pattern: "builder" },
    });
    expect(payload.version.output_contract).toMatchObject({
      artifact_type: "implementation_plan",
    });
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
