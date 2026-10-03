/**
 * The editor's API client, through the real /api/delivery-workflows proxy, to a fake API.
 *
 * Each call goes client → route handler → upstream exactly as in the console, so these hold
 * both halves to one contract: which upstream path each call reaches, what it sends, and that
 * a refusal arrives as the error the editor acts on — the problems of a 422 still pinned to
 * their steps and fields, the suggested name of a 409 — not flattened into one sentence.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DELETE, GET, PATCH, POST } from "../app/api/delivery-workflows/route";
import {
  NameInUseError, ProblemsError, checkSpec, deleteWorkflow, draftFromDescription, fetchLibrary,
  fetchPalette, fetchSpec, publishSpec, renameWorkflow,
} from "../lib/workflow-api";
import { LINEAR } from "./_helpers/workflow-fixtures";

type Call = { method: string; path: string; body: unknown };
let calls: Call[] = [];
let upstream: (call: Call) => { status?: number; body: unknown };

const HANDLERS = { GET, POST, PATCH, DELETE } as const;

beforeEach(() => {
  calls = [];
  upstream = () => ({ body: {} });
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), "http://console.test");
    const method = (init?.method ?? "GET").toUpperCase() as keyof typeof HANDLERS;
    if (url.pathname === "/api/delivery-workflows") {
      // The console's own route: hand the request to the real handler.
      return HANDLERS[method](new Request(url, { method, body: init?.body, headers: init?.headers }));
    }
    const call = { method, path: url.pathname + url.search,
                   body: init?.body ? JSON.parse(String(init.body)) : null };
    calls.push(call);
    const answer = upstream(call);
    return Response.json(answer.body, { status: answer.status ?? 200 });
  }));
});
afterEach(() => { vi.unstubAllGlobals(); });

describe("the palette", () => {
  it("joins capabilities, the project's steps and the roster into the editor's shape", async () => {
    upstream = ({ path }) => path.startsWith("/v1/delivery-workflows/capabilities") ? { body: {
      capabilities: [{ key: "deploy", label: "Deploy", summary: "Ship", risk_tier: 3,
                       required_inputs: ["run_id"], optional_inputs: [], produces: ["url"],
                       skills: [], typical_minutes: 4, is_gate: false,
                       approval_forced_manual: true, captain: "sre" }],
      blocks: [
        { kind: "decision", label: "Approval gate", summary: "s", canvas_required: false,
          defaults: { approver_role: "human" },
          configure: [{ field: "question", label: "What is being approved?", type: "text" }] },
        { kind: "parallel", label: "Do at the same time", summary: "s", canvas_required: true },
      ],
      agents: [{ role_key: "qa", name: "QA Lead", holds: ["test.run"], mcp: ["github"] }],
      human_role: "human", manual_approval_risk_floor: 2,
    } } : { body: {
      steps: [{ step_key: "yt", label: "YouTube", binding_kind: "skill", skill_id: "youtube.upload",
                inputs: [{ name: "media", required: true, description: "" }], produces: ["url"],
                enabled: true, risk_tier: 3, usable: false, unusable_reason: "Skill is off",
                approval_forced_manual: true },
              { step_key: "old", label: "Old", binding_kind: "skill", enabled: false, inputs: [],
                produces: [], risk_tier: 0, usable: true }],
      bindable: { skills: [{ skill_id: "youtube.upload", label: "YouTube upload", risk_tier: 3 }],
                  mcp_servers: [{ id: "m1", name: "github", tools: [{ name: "pr", description: "" }] }] },
    } };
    const palette = await fetchPalette("p1");
    expect(calls.map((c) => c.path).sort()).toEqual([
      "/v1/delivery-workflows/capabilities?project_id=p1",
      "/v1/delivery-workflows/custom-steps?project_id=p1",
    ]);
    expect(palette.capabilities[0]).toMatchObject({ key: "deploy", approval_forced_manual: true,
                                                    typical_minutes: 4 });
    // Only blocks a canvas can place, worded as the inspector words them.
    expect(palette.blocks).toEqual([{ kind: "decision", label: "Approval gate", summary: "s",
      defaults: { approver_role: "human" },
      configure: [{ key: "question", label: "Question", type: "text", required: true, max: 400 }] }]);
    expect(palette.customSteps).toEqual([{ step_key: "yt", label: "YouTube", binding_kind: "skill",
      skill_id: "youtube.upload", inputs: [{ name: "media", required: true }], produces: ["url"],
      risk_tier: 3, usable: false, unusable_reason: "Skill is off", approval_forced_manual: true }]);
    expect(palette.agents).toEqual([{ role_key: "qa", name: "QA Lead", skills: ["test.run"],
                                      mcp: ["github"] }]);
    expect(palette.bindable).toEqual({ skills: [{ id: "youtube.upload", name: "YouTube upload" }],
      mcp_servers: [{ id: "m1", name: "github", tools: [{ name: "pr" }] }] });
    expect([palette.humanRole, palette.riskFloor]).toEqual(["human", 2]);
  });

  it("still opens with the built-ins when the project's steps cannot be read", async () => {
    upstream = ({ path }) => path.includes("custom-steps") ? { status: 503, body: { detail: "down" } }
      : { body: { capabilities: [], blocks: [], agents: [], custom_steps: [
        { step_key: "yt", label: "YouTube", binding_kind: "skill", inputs: [], produces: [],
          risk_tier: 0, usable: true }] } };
    const palette = await fetchPalette("p1");
    expect(palette.customSteps.map((s) => s.step_key)).toEqual(["yt"]);
  });
});

describe("the library", () => {
  it("reads the workflow list and draws each step by the step-view rule", async () => {
    upstream = () => ({ body: { items: [{
      id: "wfd_1", workflow_key: "mine", name: "Mine", is_catalogue: false, active_version: 3,
      step_count: 3, forked_from_workflow_key: "minor_feature", forked_from_name: "Minor Feature",
      run_count: 2, last_run: { status: "completed", at: "2026-10-01T00:00:00Z" },
      steps: [
        { id: "build", label: "build", kind: "capability", captain: "", approval: "auto" },
        { id: "write", label: "write", kind: "capability", agent_work: true, title: "Write it" },
        { id: "ok", label: "ok", kind: "decision", captain: "human", approval: "manual" },
      ] }] } });
    const [w] = await fetchLibrary("p1");
    expect(calls[0].path).toBe("/v1/delivery-workflows?project_id=p1");
    expect(w).toMatchObject({ definition_id: "wfd_1", version: 3, run_count: 2,
                              forked_from_workflow_key: "minor_feature",
                              last_run: { status: "completed" } });
    expect(w.steps).toEqual([
      { id: "build", title: "build", capability: "build", approval: "auto" },
      { id: "write", title: "Write it", capability: "agent" },
      { id: "ok", title: "ok", capability: "gate", captain: "human", approval: "manual" },
    ]);
  });
});

describe("one workflow", () => {
  it("opens a version read-only, and finds your copy of a standard", async () => {
    upstream = ({ path }) => path.startsWith("/v1/delivery-workflows/minor_feature/spec") ? { body: {
      definition_id: "wfd_std", workflow_key: "minor_feature", name: "Minor Feature",
      is_catalogue: true, version: 2, spec: LINEAR,
      versions: [{ version: 3, published_at: "x", is_active: true },
                 { version: 2, published_at: "y", is_active: false }],
    } } : { body: { items: [
      { id: "wfd_c", workflow_key: "minor_feature_mine", name: "Minor (mine)", is_catalogue: false,
        forked_from_workflow_key: "minor_feature", steps: [] }] } };
    const { spec, meta, versions } = await fetchSpec("p1", "minor_feature", 2);
    expect(calls[0].path).toBe("/v1/delivery-workflows/minor_feature/spec?project_id=p1&version=2");
    expect(spec).toEqual(LINEAR);
    expect(versions).toHaveLength(2);
    expect(meta).toMatchObject({ definition_id: "wfd_std", is_catalogue: true, version: 2,
      readOnlyVersion: 2,
      copy: { definition_id: "wfd_c", workflow_key: "minor_feature_mine", name: "Minor (mine)" } });
  });

  it("opens the running version editable", async () => {
    upstream = () => ({ body: { definition_id: "d", workflow_key: "k", name: "K", version: 3,
                                is_catalogue: false, spec: { steps: [] }, copy: null,
                                versions: [{ version: 3, published_at: "x", is_active: true }],
                                forked_from: { workflow_key: "minor_feature" } } });
    const { meta } = await fetchSpec("p1", "k");
    expect(meta).toMatchObject({ readOnlyVersion: null, copy: null,
                                 from_workflow_key: "minor_feature" });
  });

  it("check returns the problems, pinned, in one answer", async () => {
    upstream = () => ({ body: { ok: false, problems: [
      { step_id: "build", field: "instructions", message: "Missing input instructions" },
      { step_id: "gate", field: "question", message: "Ask a question" }],
      summary: { steps: 3, agents: ["qa"], approvals: ["deploy"], typical_minutes: 40, needs: [] } } });
    const result = await checkSpec("p1", LINEAR, "wfd_1");
    expect(calls[0]).toMatchObject({ method: "POST", path: "/v1/delivery-workflows/check",
      body: { project_id: "p1", spec: LINEAR, definition_id: "wfd_1" } });
    expect(result.ok).toBe(false);
    expect(result.problems.map((p) => [p.step_id, p.field])).toEqual(
      [["build", "instructions"], ["gate", "question"]]);
    expect(result.summary.typical_minutes).toBe(40);
  });

  it("publish sends one call and reads the new version", async () => {
    upstream = () => ({ body: { definition: { id: "wfd_c", workflow_key: "mine", name: "Mine" },
                                version: 1, created: true } });
    const result = await publishSpec("p1", { definition_id: null, name: "Mine", spec: LINEAR,
                                              from_workflow_key: "minor_feature" });
    expect(calls).toHaveLength(1);
    expect(calls[0]).toMatchObject({ path: "/v1/delivery-workflows/publish",
      body: { project_id: "p1", definition_id: null, from_workflow_key: "minor_feature" } });
    expect(result).toEqual({ definition: { id: "wfd_c", workflow_key: "mine", name: "Mine" },
                             version: 1, created: true });
  });

  it("publish says when nothing changed", async () => {
    upstream = () => ({ body: { unchanged: true } });
    expect(await publishSpec("p1", { definition_id: "d", name: "x", spec: LINEAR }))
      .toEqual({ unchanged: true });
  });

  it("a 422 arrives as ProblemsError with every problem still pinned", async () => {
    upstream = () => ({ status: 422, body: { detail: { problems: [
      { step_id: "deploy", field: "approval", message: "Risk 3 — always needs a person" }] } } });
    const error = await publishSpec("p1", { definition_id: "d", name: "x", spec: LINEAR })
      .catch((e) => e);
    expect(error).toBeInstanceOf(ProblemsError);
    expect(error.problems).toEqual([{ step_id: "deploy", field: "approval",
                                      message: "Risk 3 — always needs a person" }]);
  });

  it("a 409 name clash arrives as NameInUseError with the suggested name", async () => {
    upstream = () => ({ status: 409, body: { detail: "Name in use", suggested_name: "Mine 2" } });
    const error = await publishSpec("p1", { definition_id: null, name: "Mine", spec: LINEAR })
      .catch((e) => e);
    expect(error).toBeInstanceOf(NameInUseError);
    expect(error.suggestedName).toBe("Mine 2");
    const renamed = await renameWorkflow("p1", "d", { name: "Mine" }).catch((e) => e);
    expect(renamed).toBeInstanceOf(NameInUseError);
  });

  it("any other refusal is the API's own sentence", async () => {
    upstream = () => ({ status: 403, body: { detail: "Standards can't be changed in place." } });
    const error = await renameWorkflow("p1", "d", { name: "x" }).catch((e) => e);
    expect(error.message).toBe("Standards can't be changed in place.");
  });

  it("rename patches the definition in place", async () => {
    await renameWorkflow("p1", "wfd 1", { name: "New", description: "D" });
    expect(calls[0]).toEqual({ method: "PATCH", path: "/v1/delivery-workflows/wfd%201",
      body: { project_id: "p1", name: "New", description: "D" } });
  });

  it("delete goes over DELETE and reports a retire", async () => {
    upstream = () => ({ body: { outcome: "retired" } });
    expect(await deleteWorkflow("p1", "wfd_1")).toEqual({ outcome: "retired" });
    expect(calls[0]).toMatchObject({ method: "DELETE",
                                     path: "/v1/delivery-workflows/wfd_1?project_id=p1" });
  });

  it("draft writes nothing and returns steps to review", async () => {
    upstream = () => ({ body: { name: "Weekly digest", spec: { entry_step: "a", steps: [] } } });
    expect(await draftFromDescription("p1", "send a digest")).toEqual(
      { name: "Weekly digest", spec: { entry_step: "a", steps: [] } });
    expect(calls[0]).toMatchObject({ path: "/v1/delivery-workflows/draft",
                                     body: { project_id: "p1", description: "send a digest" } });
  });
});

describe("the proxy keeps its older doors", () => {
  it("a custom step PATCH still goes to the step, by key", async () => {
    const r = await PATCH(new Request("http://console.test/api/delivery-workflows", {
      method: "PATCH", body: JSON.stringify({ step_key: "yt", label: "YouTube" }) }));
    expect(r.status).toBe(200);
    expect(calls[0]).toMatchObject({ method: "PATCH",
      path: "/v1/delivery-workflows/custom-steps/yt", body: { label: "YouTube" } });
  });

  it("a delete with neither a workflow, a run, a schedule nor a step is refused", async () => {
    const r = await DELETE(new Request("http://console.test/api/delivery-workflows?project_id=p1",
                                       { method: "DELETE" }));
    expect(r.status).toBe(400);
    expect(calls).toEqual([]);
  });
});
