/**
 * The spec the editor writes is the spec the API's management journey publishes and runs.
 *
 * The API tests (apps/api/tests/test_workflow_management_runs_e2e.py) build, start and drive a
 * workflow through every endpoint. They used to hand-write that workflow "as the editor writes
 * it" — so a serialiser that wrote `mcp_servers` for `mcp`, or put a gate's approver somewhere
 * else, would pass every API test while a save from the real editor failed or lost data.
 *
 * Now this test builds the workflow the way the editor does — the palette's "Agent task" and
 * "Approval gate" with the inspector's fields filled in, then `specFromDraft` — and holds it
 * equal to `fixtures/editor-built-workflow.json`, which the API journey publishes and runs.
 * A change to what the editor writes fails here until the fixture is regenerated
 * (`WRITE_FIXTURES=1 npx vitest run tests/the-editor-writes-what-the-api-runs.test.ts`), and the
 * regenerated fixture is then run end to end by the API tests.
 *
 * Agents, skills and MCP servers are made fresh by each API test, so the fixture names them
 * with placeholders the API test fills in: `{agent}`, `{skill}`, `{mcp}`.
 */
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { specFromDraft, type DraftStep } from "../lib/workflow-spec";

const FIXTURE = new URL("./fixtures/editor-built-workflow.json", import.meta.url);

/** The Approval gate's palette defaults, as `workflow_capabilities.CONTROL_BLOCKS` serves
 *  them; the API test holds the fixture's gate to the served defaults. */
const GATE_DEFAULTS = { approver_role: "human" };

/** The canvas after: Agent task → Approval gate → Agent task, each set up in the inspector. */
const DRAFT: DraftStep[] = [
  {
    // "Agent task": an agent, one skill, one MCP server; the task comes from the start form.
    id: "draft", kind: "capability", agentWork: true, approval: "auto", config: {},
    captain: "{agent}", skills: ["{skill}"], mcp: ["{mcp}"],
  },
  {
    // "Approval gate": a question and what to check.
    id: "look", kind: "decision", approval: "auto",
    config: { ...GATE_DEFAULTS, question: "Ship the draft?", guidance: "Read it through once." },
  },
  {
    // An agent task with a fixed task, switched to "Needs approval".
    id: "finish", kind: "capability", agentWork: true, approval: "manual", config: {},
    captain: "{agent}", instructions: { value: "Write the release note." },
  },
];

const built = () => specFromDraft(DRAFT, { loaded: null, capabilities: [], customSteps: [] });

describe("the editor's spec is the one the API journey runs", () => {
  it("equals the fixture the API tests publish", () => {
    const spec = built();
    if (process.env.WRITE_FIXTURES === "1" || !existsSync(FIXTURE)) {
      writeFileSync(FIXTURE, `${JSON.stringify(spec, null, 2)}\n`);
    }
    expect(JSON.parse(readFileSync(FIXTURE, "utf8"))).toEqual(spec);
  });

  it("writes the keys the runtime reads, not look-alikes", () => {
    const [draft, look, finish] = built().steps as Record<string, any>[];
    expect(draft.binding).toEqual({
      agent_work: true, inputs: { instructions: { from_input: "instructions" } },
      skills: ["{skill}"], mcp: ["{mcp}"],
    });
    expect(draft.captain).toBe("{agent}");
    expect(look).toMatchObject({ kind: "decision", question: "Ship the draft?",
      guidance: "Read it through once.", approver_role: "human" });
    expect(finish.approval).toEqual({ mode: "manual" });
  });
});
