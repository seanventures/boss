/**
 * The console edits only the operator's own workflows, writes as the operator, and shows runs.
 *
 * Three findings from boss-1:
 *   - Agent Studio's Flows listed every workflow_definitions row, BOSS's 32 system workflows
 *     included, with a raw JSON editor and "Publish version" — one paste could replace the
 *     governed coding pipeline that reviews every build.
 *   - Every Studio write hard-coded its actor ('vp_agents', 'ceo'); with API auth on, the
 *     console key's principal is `ceo`, so Create agent failed with 403.
 *   - Agent and panel runs had no visible result anywhere.
 *
 * REWRITTEN for Workflow Studio. Flows is gone: the one JSON editor is the Workflow editor's,
 * which saves through POST /v1/delivery-workflows/publish — a standard is saved as the
 * operator's copy and a system workflow is refused (tests/save-flow.test.tsx and the API's
 * publish tests). What is pinned here: nothing in the console writes a workflow any other way,
 * Studio's remaining actions write as the operator, and their runs are shown.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

const ACTIONS = codeOnly("app/agent-studio-actions.ts");
const PROFILE = codeOnly("app/agent-profile-ui.tsx");

describe("workflows go through the customer's door, and only that one", () => {
  it("Studio's actions no longer create, publish, validate or run a workflow", () => {
    for (const gone of ["createStudioWorkflow", "publishStudioWorkflowVersion",
                        "validateStudioWorkflow", "runStudioWorkflow"]) {
      expect(ACTIONS).not.toContain(gone);
    }
    expect(ACTIONS).not.toContain("/v1/delivery-workflows/");
    expect(ACTIONS).not.toContain('"/v1/workflow-runs"');
    expect(ACTIONS).not.toMatch(/\/v1\/workflows\b/);
  });

  it("the API still refuses an operator publish over BOSS's own workflows", () => {
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

  it("every exported Studio action takes the actor, and none may leave it out", () => {
    const exported = [...ACTIONS.matchAll(/export async function (\w+)\(([^)]*)\)/g)];
    expect(exported.length).toBeGreaterThanOrEqual(5);
    for (const [, name, params] of exported) {
      if (name === "createStudioAgent") continue; // carries it in the submission, below
      expect(params, name).toMatch(/\bactor: string/);
    }
  });

  it("New agent's profile submission is made as the operator", () => {
    expect(ACTIONS).toContain("const actor = submission.actor;");
    expect(codeOnly("app/agent-quick-create.tsx"))
      .toContain("buildAgentWizardSubmission(projectId, draft, actor)");
  });
});

describe("what happened to a run", () => {
  it("an agent's Profile and each panel list their recent runs with their outcome", () => {
    expect(PROFILE).toContain("feed.agentRuns.filter((r) => r.agent_key === agent.agent_key)");
    expect(PROFILE).toContain("feed.panelRuns.filter((r) => r.panel_key === panel.panel_key)");
    expect(PROFILE).toContain('data-testid="wf-ag-runs"');
  });

  it("a refused panel run draws the checklist, like every other start", () => {
    expect(PROFILE).toContain("asReadiness(next.readiness)");
    expect(PROFILE).toContain("<StartRefusal");
  });
});
