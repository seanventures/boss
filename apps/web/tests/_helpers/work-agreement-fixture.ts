import type { WorkAgreement } from "../../lib/work-agreement";
export const BRIEF = "Add a health endpoint and its test. Open a PR for review.";
export function agreementFixture(directive = BRIEF, projectId = "p1"): WorkAgreement {
  const content_hash = "1".repeat(64), expires_at_unix = Math.floor(Date.now() / 1000) + 600;
  return { schema_version: 1, action: "delivery_workflow.start", project_id: projectId,
    content_hash, binding_token: `${expires_at_unix}.${content_hash}.${"3".repeat(32)}.${"2".repeat(64)}`, expires_at_unix, start_intent_key: `agreement:${"4".repeat(64)}`,
    workflow_key: "bounded", workflow_name: "Bounded software change", workflow_version_id: "wfv1", workflow_version: 3,
    entry_step: "plan", input: { title: directive, directive }, start_anyway: [], repository: {
      full_name: "owner/app", base_branch: "main", base_revision: null, source: "registry", binding_id: "b1", selection: "current_project_target" },
    approved_spec: null, acceptance_criteria: [], test_commands: [], selected_provider: null, selected_account: null,
    steps: [{ id: "plan", label: "Plan", does: "Draft the task specification.", capability: "prd", agent: "pm",
      approval: "auto", approval_note: "Runs without stopping.", produces: ["PRD"], next: ["review"] },
      { id: "review", label: "Review", does: "Review the specification.", capability: "review", agent: null,
        approval: "manual", approval_note: "A person must release this step.", produces: ["Decision"], next: [] }],
    results: ["PRD", "Decision"], stop_conditions: ["A person must release this step."],
    financial: { estimate_usd_micros: null, reservation_usd_micros: null, authorized_limit_usd_micros: null,
      history: null, history_basis: "API-equivalent value; may include subscription usage." },
    bounds: ["Existing identity, consent, budget and policy checks apply at execution."],
    unknowns: ["No approved task specification or checks are recorded yet."] };
}
