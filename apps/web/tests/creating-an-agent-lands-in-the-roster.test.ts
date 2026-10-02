/**
 * An agent created by the wizard appears on the Agents page.
 *
 * It did not, and the reason is the four-models problem in its most concrete form. BOSS has
 * four places an "agent" can live:
 *
 *     Workforce roster   `roles`                          <- what the operator looks at
 *     Agent Studio       `agent_definitions`/`_versions`  <- what the wizard wrote
 *     Focused agents     `focused_agents`
 *     Mission roster     a read-only view
 *
 * The only creation wizard in BOSS wrote the second and referenced an existing role_key from
 * the first. So "create an agent" could never produce one the roster lists — and the roster had
 * no create button at all, which is what the operator noticed.
 *
 * Registering the role first converges them, and the assignment model can then assign it.
 */
import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

const WIZARD = codeOnly("app/agent-creation-wizard.tsx");
const ACTIONS = codeOnly("app/agent-studio-actions.ts");
const LIB = codeOnly("lib/agent-wizard.ts");
const ROSTER = codeOnly("app/workforce-ui.tsx");

describe("making a new agent", () => {
  it("the roster has a way to add one", () => {
    expect(ROSTER).toContain('data-testid="wf-new-agent"');
    // The one-screen create is what the button opens; the full Studio wizard is one press on.
    expect(ROSTER).toContain("<AgentQuickCreate");
    expect(ROSTER).toContain("<AgentCreationWizard");
  });

  it("the wizard can make a role rather than only borrow one", () => {
    // Every option was a role that already existed, so the wizard could only ever point at
    // somebody else's.
    expect(WIZARD).toContain("A new role for this agent");
    expect(WIZARD).toContain("draft.roleKey === NEW_ROLE");
  });

  // REWRITTEN. These asserted the wizard POSTed /v1/roles with the chosen skills in
  // `spec.allowed_skills` and `autonomy_level: 0` before the Studio definition. That was the
  // defect: allowed_skills is prose the runtime never reads, so the agent held nothing; and
  // /v1/roles took its UPDATE path on an existing key, so naming a new agent "QA" overwrote the
  // bundled qa role. The agent is now created through POST /v1/agents, which grants its skills
  // and MCP servers in the same transaction and refuses an existing key (autonomy 0 is set
  // server-side: apps/api/tests/test_one_agent_model.py).
  it("and the submission carries that agent, with its grants", () => {
    expect(LIB).toContain("const makingRole = draft.roleKey === NEW_ROLE;");
    expect(LIB).toContain("role_key: newRoleKey");
    expect(LIB).toContain("const roleKey = makingRole ? newRoleKey : draft.roleKey;");
    expect(LIB).toContain("skills,");
    expect(LIB).toContain("mcp_servers: mcpServers,");
  });

  it("the agent is created, with its grants, BEFORE the Studio profile", () => {
    const agent = ACTIONS.indexOf('post("/v1/agents"');
    const definition = ACTIONS.indexOf('post("/v1/agent-studio/agents"');
    expect(agent).toBeGreaterThan(-1);
    expect(agent).toBeLessThan(definition);
    // An existing agent is GIVEN what the profile uses, through the same agent resource.
    expect(ACTIONS).toContain("patch(");
    expect(ACTIONS).not.toContain('post("/v1/roles"');
  });

  it("and a failed creation stops there rather than half-creating", () => {
    expect(ACTIONS).toContain("The agent could not be created");
  });

  it("and the roster keeps its own class namespace", () => {
    // A guard test already enforces this; the new button has to obey it, because a shared class
    // means a restyle elsewhere silently restyles this screen.
    expect(ROSTER).not.toContain('className="cap-btn primary"');
  });
});
