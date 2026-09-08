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
    expect(ROSTER).toContain("<AgentCreationWizard");
  });

  it("the wizard can make a role rather than only borrow one", () => {
    // Every option was a role that already existed, so the wizard could only ever point at
    // somebody else's.
    expect(WIZARD).toContain("A new role for this agent");
    expect(WIZARD).toContain("draft.roleKey === NEW_ROLE");
  });

  it("and the submission carries that role", () => {
    expect(LIB).toContain("const makingRole = draft.roleKey === NEW_ROLE;");
    expect(LIB).toContain("role_key: newRoleKey");
    // The version must point at the role being made, not at whatever was selected before.
    expect(LIB).toContain("role_key: makingRole ? newRoleKey : draft.roleKey");
  });

  it("the role is registered BEFORE the definition", () => {
    // An agent whose role does not exist is one the roster cannot list and the assignment model
    // cannot assign. Order matters: the version references the role_key.
    const role = ACTIONS.indexOf('post("/v1/roles"');
    const definition = ACTIONS.indexOf('post("/v1/agent-studio/agents"');
    expect(role).toBeGreaterThan(-1);
    expect(role).toBeLessThan(definition);
  });

  it("and a failed registration stops there rather than half-creating", () => {
    expect(ACTIONS).toContain("The agent's role could not be registered");
  });

  it("a hand-made agent starts at autonomy zero", () => {
    // It has done nothing yet. An autonomy level it did not earn is the one number on this page
    // that must not be self-awarded.
    expect(LIB).toContain("autonomy_level: 0");
  });

  it("and the roster keeps its own class namespace", () => {
    // A guard test already enforces this; the new button has to obey it, because a shared class
    // means a restyle elsewhere silently restyles this screen.
    expect(ROSTER).not.toContain('className="cap-btn primary"');
  });
});
