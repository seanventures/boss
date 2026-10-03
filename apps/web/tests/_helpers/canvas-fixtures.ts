/**
 * Palettes and specs for the Workflow Studio canvas tests: a linear flow of any length, a
 * branched one (parallel → join, then a condition), and a palette with built-ins, a gate, a
 * forced-approval step, one of your own steps and the three flow blocks.
 */
import type { Palette, Spec, SpecStep } from "../../lib/workflow-editor-types";

const cap = (key: string, label: string, extra: Partial<Palette["capabilities"][number]> = {}) => ({
  key, label, summary: `${label} the work`, risk_tier: 1, required_inputs: [], optional_inputs: [], produces: [],
  skills: [], typical_minutes: 10, ...extra,
});

export const KEYS = [
  "research", "meeting", "spec_review", "build", "review", "test", "preview", "ux_review", "security_review",
  "deploy", "monitor", "notify", "smoke_test", "redeploy", "cost_check", "document",
];

export const PALETTE: Palette = {
  capabilities: [
    cap("research", "Research"), cap("meeting", "Convene a panel"),
    cap("spec_review", "PRD approval", { is_gate: true }),
    cap("build", "Build, review and test", { required_inputs: ["repo"], produces: ["code_task_run_id"], typical_minutes: 46 }),
    cap("review", "Review"), cap("test", "Test"), cap("preview", "Preview"), cap("ux_review", "UX review"),
    cap("security_review", "Security review"),
    cap("deploy", "Deploy", { risk_tier: 3, approval_forced_manual: true }),
    cap("monitor", "Set up monitoring"), cap("notify", "Notify"), cap("smoke_test", "Smoke test"),
    cap("redeploy", "Redeploy"), cap("cost_check", "Cost check"), cap("document", "Document"),
  ],
  customSteps: [{
    step_key: "trim_clip", label: "Trim the clip", summary: "Cut a clip", binding_kind: "skill", skill_id: "sk_trim",
    inputs: [{ name: "video_url", required: true }], produces: ["clip_url"], risk_tier: 1, usable: true,
  }, {
    step_key: "broken", label: "Broken step", binding_kind: "mcp_tool", inputs: [], produces: [], risk_tier: 1,
    usable: false, unusable_reason: "Its MCP server is gone.",
  }],
  blocks: [
    { kind: "decision", label: "Decision", summary: "A person decides.", defaults: {}, configure: [] },
    { kind: "timer", label: "Timer", summary: "Wait a while.", defaults: {}, configure: [] },
    { kind: "subworkflow", label: "Subworkflow", summary: "Run another workflow.", defaults: {}, configure: [] },
  ],
  agents: [
    { role_key: "qa", name: "QA Lead", skills: ["sk_trim"], mcp: [] },
    { role_key: "software_engineer", name: "Software Engineer", skills: [], mcp: [] },
  ],
  humanRole: "human", riskFloor: 2,
  bindable: { skills: [{ id: "sk_trim", name: "trim" }], mcp_servers: [] },
};

/** n built-in steps in a row, each named by its capability and wired by `next`. */
export function linear(n: number, extra: (i: number) => Partial<SpecStep> = () => ({})): Spec {
  const ids = Array.from({ length: n }, (_, i) => (i < KEYS.length ? KEYS[i] : `${KEYS[i % KEYS.length]}_${i}`));
  return {
    entry_step: ids[0],
    steps: ids.map((id, i) => ({
      id, kind: "capability", binding: { capability: KEYS[i % KEYS.length] },
      ...(i + 1 < n ? { next: [ids[i + 1]] } : {}), ...extra(i),
    })),
  };
}

/** research → parallel(review, test) → join → condition(yes: deploy, no: notify). */
export const BRANCHED: Spec = {
  entry_step: "research",
  steps: [
    { id: "research", kind: "capability", binding: { capability: "research" }, next: ["fan"] },
    { id: "fan", kind: "parallel", branches: ["review", "test"], join: "gather" },
    { id: "review", kind: "capability", binding: { capability: "review" }, next: ["gather"] },
    { id: "test", kind: "capability", binding: { capability: "test" }, next: ["gather"] },
    { id: "gather", kind: "join", wait_for: ["review", "test"], next: ["ok"] },
    { id: "ok", kind: "condition", expression: "tests passed", on_true: "deploy", on_false: "notify" },
    { id: "deploy", kind: "capability", binding: { capability: "deploy" } },
    { id: "notify", kind: "capability", binding: { capability: "notify" } },
  ],
};

/** research → parallel(a1 → a2, b1) → join → done: a branch with a linear stretch inside it. */
export const STRETCH: Spec = {
  entry_step: "research",
  steps: [
    { id: "research", kind: "capability", binding: { capability: "research" }, next: ["fan"] },
    { id: "fan", kind: "parallel", branches: ["a1", "b1"], join: "gather" },
    { id: "a1", kind: "capability", binding: { capability: "review" }, next: ["a2"] },
    { id: "a2", kind: "capability", binding: { capability: "test" }, next: ["gather"] },
    { id: "b1", kind: "capability", binding: { capability: "ux_review" }, next: ["gather"] },
    { id: "gather", kind: "join", wait_for: ["a2", "b1"], next: ["done"] },
    { id: "done", kind: "capability", binding: { capability: "notify" } },
  ],
};
