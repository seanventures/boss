/**
 * Specs and a palette the Workflow Studio model tests share.
 *
 * The branched specs are shaped the way the runtime reads them (engine workflows.py
 * `_targets` / `_process_control_step`): a parallel step's `branches` start together, each
 * branch's `next` leads to the join, and the join `wait_for`s them.
 */
import { readFileSync } from "node:fs";

import type { Palette, Spec } from "../../lib/workflow-editor-types";

/** A straight line with the extras a real spec carries and the canvas does not draw. */
export const LINEAR: Spec = {
  entry_step: "research",
  objective: "Ship it",
  input_schema: { type: "object", required: ["directive"],
                  properties: { directive: { type: "string", title: "What to build" } } },
  steps: [
    { id: "research", kind: "capability", max_attempts: 3,
      binding: { capability: "research", inputs: { topic: { from_input: "directive" } } },
      approval: { mode: "auto", on_timeout: "block" }, next: ["build"] },
    { id: "build", kind: "capability", captain: "engineering_captain",
      binding: { capability: "build", inputs: {
        instructions: { from_input: "directive" },
        brief: { from_step: "research", field: "dossier" } } },
      approval: { mode: "auto", on_timeout: "block", sla_hours: 4 }, next: ["deploy"] },
    { id: "deploy", kind: "capability",
      binding: { capability: "deploy", inputs: { run_id: { from_step: "build", field: "run_id" } } },
      approval: { mode: "manual", on_timeout: "block", forced_manual: true }, next: [] },
  ],
};

/** A fan-out to two reviews (one of them two steps long), joined, then shipped. */
export const PARALLEL: Spec = {
  entry_step: "intake",
  output_schema: { kind: "release" },
  steps: [
    { id: "intake", kind: "capability", binding: { capability: "research", inputs: {} },
      next: ["fan"] },
    { id: "fan", kind: "parallel", branches: ["review_a", "review_b"], join: "merge", next: [],
      binding: {} },
    { id: "review_a", kind: "capability", binding: { capability: "ux_review", inputs: {} },
      next: ["review_a2"] },
    { id: "review_a2", kind: "capability", binding: { capability: "security_review", inputs: {} },
      next: ["merge"] },
    { id: "review_b", kind: "capability", captain: "qa",
      binding: { agent_work: true, inputs: { instructions: { from_input: "instructions" } } },
      next: ["merge"] },
    { id: "merge", kind: "join", wait_for: ["review_a2", "review_b"], success_policy: "all",
      next: ["ship"], binding: {} },
    { id: "ship", kind: "capability", binding: { capability: "deploy", inputs: {} },
      approval: { mode: "manual" }, next: ["tell"] },
    { id: "tell", kind: "capability", binding: { capability: "notify", inputs: {} }, next: [] },
  ],
};

/** An if/else whose two paths meet again, and a bounded loop. */
export const BRANCHES: Spec = {
  entry_step: "check",
  steps: [
    { id: "check", kind: "condition", expression: { path: "input.urgent", equals: true },
      on_true: "hotfix", on_false: "plan", next: [] },
    { id: "hotfix", kind: "capability", binding: { capability: "build", inputs: {} },
      next: ["poll"] },
    { id: "plan", kind: "capability", binding: { capability: "meeting", inputs: {} },
      next: ["poll"] },
    { id: "poll", kind: "bounded_loop", body: "probe", max_iterations: 5,
      expression: { path: "steps.probe.output.ok", equals: false }, next: ["done"] },
    { id: "probe", kind: "capability", binding: { capability: "smoke_test", inputs: {} },
      next: [] },
    { id: "done", kind: "capability", binding: { capability: "notify", inputs: {} }, next: [] },
  ],
};

export const PALETTE: Palette = {
  capabilities: [
    { key: "research", label: "Research", summary: "", risk_tier: 0, required_inputs: ["topic"],
      optional_inputs: [], produces: ["dossier"], skills: [] },
    { key: "meeting", label: "Panel meeting", summary: "", risk_tier: 0,
      required_inputs: ["directive"], optional_inputs: ["research"], produces: ["prd_markdown"],
      skills: [] },
    { key: "build", label: "Build", summary: "", risk_tier: 1,
      required_inputs: ["instructions"], optional_inputs: ["brief"],
      produces: ["run_id", "pr_url"], skills: [], captain: "engineering_captain" },
    { key: "deploy", label: "Deploy", summary: "", risk_tier: 3, required_inputs: ["run_id"],
      optional_inputs: [], produces: ["url"], skills: [], approval_forced_manual: true },
    { key: "notify", label: "Notify", summary: "", risk_tier: 0,
      required_inputs: ["message", "channel"], optional_inputs: [], produces: [], skills: [] },
    { key: "ux_review", label: "UX review", summary: "", risk_tier: 0, required_inputs: [],
      optional_inputs: [], produces: [], skills: [] },
    { key: "security_review", label: "Security review", summary: "", risk_tier: 0,
      required_inputs: [], optional_inputs: [], produces: [], skills: [] },
    { key: "smoke_test", label: "Smoke test", summary: "", risk_tier: 0, required_inputs: ["url"],
      optional_inputs: [], produces: ["ok"], skills: [] },
  ],
  customSteps: [
    { step_key: "publish_youtube", label: "Publish to YouTube", binding_kind: "skill",
      skill_id: "youtube.upload", inputs: [{ name: "media_artifact_id", required: true },
                                           { name: "title", required: false }],
      produces: ["video_url"], risk_tier: 3, usable: true, approval_forced_manual: true },
    { step_key: "post_slack", label: "Post to Slack", binding_kind: "mcp_tool",
      mcp_server_id: "mcp_1", mcp_tool_name: "post", inputs: [{ name: "text", required: true }],
      produces: [], risk_tier: 1, usable: true },
  ],
  blocks: [
    { kind: "decision", label: "Approval gate", summary: "", defaults: { approver_role: "human" },
      configure: [{ key: "question", label: "Question", type: "text", required: true, max: 400 }] },
    { kind: "timer", label: "Wait", summary: "", defaults: { wait_seconds: 3600 }, configure: [] },
    { kind: "subworkflow", label: "Run another workflow", summary: "", defaults: {}, configure: [] },
  ],
  agents: [
    { role_key: "qa", name: "QA Lead", skills: ["test.run", "youtube.upload"], mcp: [] },
    { role_key: "video_editor", name: "Video Editor", skills: ["video.trim"], mcp: ["slack"] },
    { role_key: "engineering_captain", name: "Engineering Captain", skills: ["code.write"], mcp: [] },
  ],
  humanRole: "human",
  riskFloor: 2,
  bindable: { skills: [{ id: "youtube.upload", name: "YouTube upload" }],
              mcp_servers: [{ id: "mcp_1", name: "slack", tools: [{ name: "post" }] }] },
};

/**
 * Every standard BOSS ships, as published (tests/fixtures/catalogue-specs.json, written by
 * scripts/dump-catalogue-specs.py from engine workflow_catalogue.py). Accepts a map of
 * key → spec, a list of specs, or a list of {workflow_key|key, spec}.
 */
export function catalogueSpecs(): [string, Spec][] {
  const raw: unknown = JSON.parse(readFileSync(
    new URL("../fixtures/catalogue-specs.json", import.meta.url), "utf8"));
  const entries: [string, unknown][] = Array.isArray(raw)
    ? raw.map((item, i) => {
      const obj = item as Record<string, unknown>;
      return obj && "spec" in obj
        ? [String(obj.workflow_key ?? obj.key ?? i), obj.spec]
        : [String(obj?.workflow_name ?? i), item];
    })
    : Object.entries(raw as Record<string, unknown>);
  return entries.map(([key, spec]) => [key, spec as Spec]);
}

export const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
