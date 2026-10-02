/**
 * Agent Studio edits the operator's own workflows, writes as the operator, and shows its runs.
 *
 * Three findings from boss-1:
 *   - Flows listed every workflow_definitions row, BOSS's 32 system workflows included, with a
 *     raw JSON editor and "Publish version" — one paste could replace the governed coding
 *     pipeline that reviews every build.
 *   - Every Studio write hard-coded its actor ('vp_agents', 'ceo'); with API auth on, the
 *     console key's principal is `ceo`, so Create agent failed with 403 after leaving an orphan
 *     role behind.
 *   - Agent and panel runs had no visible result anywhere.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

const ACTIONS = codeOnly("app/agent-studio-actions.ts");
const UI = codeOnly("app/agent-studio-ui.tsx");

describe("workflows go through the customer's door", () => {
  it("publish and create use the product endpoints, never the generic one", () => {
    expect(ACTIONS).toContain('"/v1/delivery-workflows/save"');
    expect(ACTIONS).toContain('"/v1/delivery-workflows/create"');
    expect(ACTIONS).not.toMatch(/\/v1\/workflows\/\$\{/);
    expect(ACTIONS).not.toContain('post("/v1/workflows"');
  });

  it("Flows says what it holds and where BOSS's own workflows are changed", () => {
    expect(UI).toContain('data-testid="as-flows-scope"');
    // REWRITTEN: this pinned "fork it in Workflow &gt; Catalogue", advice that could not be
    // followed: the catalogue lists product templates, not BOSS's own workflows.
    expect(UI).toContain("are not editable from the console");
    expect(UI).not.toMatch(/to change one, fork it/);
  });

  it("the API only hands Studio product workflows, and refuses publishes over BOSS's", () => {
    const api = readFileSync("../api/app/main.py", "utf8");
    const catalog = api.slice(api.indexOf('@app.get("/v1/agent-studio/catalog")'),
                              api.indexOf('@app.post("/v1/agent-studio/catalog-smoke")'));
    expect(catalog).toContain("namespace=workflow_runtime.PRODUCT_NAMESPACE");
    expect(api).toContain("workflow_runtime.refuse_operator_edit_of_boss_workflow(");
  });
});

describe("Studio writes as the signed-in operator", () => {
  it("no actor is hard-coded in the Studio actions", () => {
    expect(ACTIONS).not.toMatch(/actor:\s*"(vp_agents|ceo|operator)"/);
  });

  // REWRITTEN AT INTEGRATION. The console-a and agents streams fixed the same 403 ("'ceo' is
  // not allowed to act as 'vp_agents'") two ways; the agents stream's is kept: the actor is
  // REQUIRED (no "executive_console" default to fall back to), and the wizard builds its
  // submission with the operator in it. tests/agent-writes-carry-the-signed-in-actor.test.ts
  // enforces the class across every agent writer.
  it("every exported Studio action takes the actor, and none may leave it out", () => {
    const exported = [...ACTIONS.matchAll(/export async function (\w+)\(([^)]*)\)/g)];
    expect(exported.length).toBeGreaterThanOrEqual(10);
    for (const [, name, params] of exported) {
      if (name === "createStudioAgent") continue; // carries it in the submission, below
      expect(params, name).toMatch(/\bactor: string/);
    }
  });

  it("the wizard's own submission is made as the operator", () => {
    expect(ACTIONS).toContain("const actor = submission.actor;");
    expect(codeOnly("app/agent-creation-wizard.tsx"))
      .toContain("buildAgentWizardSubmission(projectId, draft, actor)");
  });

  it("both pages hand the operator to Studio and to Agent work's wizard", () => {
    // The Community tree has one page: the private repo's page-community.tsx IS this page.tsx
  // (the export's page_export_rewrite), so the console-a stream's two-page loop reads one here.
  for (const page of ["app/page.tsx"]) {
      const src = readFileSync(page, "utf8");
      expect(src).toMatch(/<AgentStudio[\s\S]{0,200}actor=\{activeActorId\}/);
      expect(src).toMatch(/<WorkforceArea[^>]*actor=\{activeActorId\}/);
    }
    expect(codeOnly("app/workforce-ui.tsx"))
      .toMatch(/<AgentCreationWizard[^>]*actor=\{actor\}/);
  });
});

describe("what happened to a run", () => {
  it("lists the selected agent's or panel's recent runs with their outcome", () => {
    expect(UI).toContain("<StudioRuns selection={selection} feed={feed} />");
    expect(UI).toContain('data-testid="as-run-row"');
    expect(UI).toContain("Finished — no summary was recorded.");
  });

  it("a started workflow run links to itself in Workflow", () => {
    expect(UI).toContain('data-testid="as-open-run"');
    expect(UI).toContain("?area=delivery_workflows&wfr=");
  });
});

describe("plain words", () => {
  it("no storage-engine jargon, no 'objects'", () => {
    expect(UI).not.toContain("Redis cursors");
    expect(UI).not.toMatch(/\{" "\}\s*objects/);
  });

  it("a Definition link for a role with no agent prefills the wizard", () => {
    expect(UI).toContain("initialRoleKey={requestedRole || undefined}");
    const wizard = codeOnly("app/agent-creation-wizard.tsx");
    expect(wizard).toContain("roles.find((r) => r.roleKey === initialRoleKey)");
  });
});
