/** Server-issued first-workflow preview. This reader creates no authority or binding. */
export type WorkAgreement = {
  schema_version: 1; action: "delivery_workflow.start"; project_id: string;
  content_hash: string; binding_token: string; start_intent_key: string; expires_at_unix: number;
  workflow_key: string; workflow_name: string; workflow_version_id: string; workflow_version: number; entry_step: string;
  input: { title: string; directive: string }; start_anyway: string[];
  repository: { full_name: string; base_branch: string | null; base_revision: string | null;
    source: string | null; binding_id: string | null; selection: string } | null;
  approved_spec: null; acceptance_criteria: string[]; test_commands: string[];
  selected_provider: null; selected_account: null;
  steps: { id: string; label: string; does: string | null; capability: string | null;
    runs?: string | null;
    agent: string | null; approval: string | null; approval_note: string | null; produces: string[] | null; next: string[] }[];
  results: string[]; stop_conditions: string[]; bounds: string[]; unknowns: string[];
  financial: { estimate_usd_micros: number | null; reservation_usd_micros: number | null;
    authorized_limit_usd_micros: number | null;
    history: { runs: number; low_usd: number; high_usd: number; metered: boolean } | null;
    history_basis: string };
};

const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const strings = (v: unknown): v is string[] => Array.isArray(v) && v.every((x) => typeof x === "string");
const nullableString = (v: unknown) => v === null || typeof v === "string";
const amount = (v: unknown) => v === null || (typeof v === "number" && Number.isSafeInteger(v) && v >= 0);

/** Refuse a partial/wrong-scope response before rendering a confirmation control. */
export function readWorkAgreement(value: unknown, projectId: string, directive: string): WorkAgreement | null {
  if (!record(value) || value.schema_version !== 1 || value.action !== "delivery_workflow.start"
      || value.project_id !== projectId || !record(value.input) || value.input.directive !== directive
      || typeof value.input.title !== "string" || typeof value.content_hash !== "string"
      || !/^[a-f0-9]{64}$/.test(value.content_hash) || typeof value.binding_token !== "string"
      || !/^\d+\.[a-f0-9]{64}\.[a-f0-9]{32}\.[a-f0-9]{64}$/.test(value.binding_token)
      || typeof value.start_intent_key !== "string" || !/^agreement:[a-f0-9]{64}$/.test(value.start_intent_key)
      || typeof value.expires_at_unix !== "number" || !Number.isSafeInteger(value.expires_at_unix)
      || !["workflow_key", "workflow_name", "workflow_version_id", "entry_step"].every((k) => typeof value[k] === "string")
      || !Number.isSafeInteger(value.workflow_version) || !strings(value.start_anyway)
      || !["acceptance_criteria", "test_commands", "results", "stop_conditions", "bounds", "unknowns"].every((k) => strings(value[k]))
      || value.approved_spec !== null || value.selected_provider !== null || value.selected_account !== null) return null;
  if (value.repository !== null && (!record(value.repository) || typeof value.repository.full_name !== "string"
      || !["base_branch", "base_revision", "source", "binding_id"].every((k) => nullableString(value.repository && (value.repository as Record<string, unknown>)[k]))
      || !["supplied", "current_project_target"].includes(String(value.repository.selection)))) return null;
  if (!Array.isArray(value.steps) || !value.steps.every((s) => record(s) && typeof s.id === "string"
      && typeof s.label === "string" && (s.runs === undefined || nullableString(s.runs))
      && ["does", "capability", "agent", "approval", "approval_note"].every((k) => nullableString(s[k]))
      && (s.produces === null || strings(s.produces)) && strings(s.next))) return null;
  const financial = value.financial;
  if (!record(financial) || typeof financial.history_basis !== "string"
      || !["estimate_usd_micros", "reservation_usd_micros", "authorized_limit_usd_micros"].every((k) => amount(financial[k]))) return null;
  const history = financial.history;
  if (history !== null && (!record(history) || !Number.isSafeInteger(history.runs)
      || typeof history.metered !== "boolean" || !["low_usd", "high_usd"].every((k) => typeof history[k] === "number"
        && Number.isFinite(history[k]) && (history[k] as number) >= 0))) return null;
  return value as unknown as WorkAgreement;
}
