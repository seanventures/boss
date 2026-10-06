"use client";

/**
 * The reads and dry-run writes Infra › Deploys' sheets make beyond infra-actions.ts: the targets
 * a deploy can go to, the environment rules, and CI config (catalog, status, generate, validate).
 * Same doors as infra-actions.ts: reads are plain same-origin GETs; every POST goes through
 * apiWrite (`send`), so a double press is one request.
 */
import { navigate } from "../agents/live-util";
import { read, send } from "./infra-actions";
import { declareAction } from "../../lib/identity/guarded-action";

const enc = encodeURIComponent;

export type TargetOption = { id: string; app_name: string; environment: string; target_kind?: string | null;
  repo?: string | null; status?: string | null; host_id?: string | null };
export async function listTargets(projectId: string): Promise<TargetOption[] | string> {
  const r = await read<{ items?: TargetOption[]; targets?: TargetOption[] }>(`/v1/deployment-targets?project_id=${enc(projectId)}&limit=200`);
  if (!r.ok) return r.error;
  return (r.data.items ?? r.data.targets ?? []).filter((t) => t && t.id && t.status !== "retired");
}

export type Policy = { id: string; environment: string; runtime_mode: string; allowed_target_kinds?: string[];
  requires_change_set?: boolean; requires_budget_reservation?: boolean; requires_approval?: boolean;
  requires_health_check?: boolean; requires_rollback_plan?: boolean; status?: string; rollout_notes?: string | null };
export async function listPolicies(projectId: string): Promise<Policy[] | string> {
  const r = await read<{ items?: Policy[] }>(`/v1/deployment-environment-policies?project_id=${enc(projectId)}&limit=50`);
  return r.ok ? r.data.items ?? [] : r.error;
}

// ── CI config (the old CICDPipelinesPanel's capability, through the browser proxy) ─────────

export type CiVendor = { id: string; name: string; config_path?: string; live_adapter?: boolean; languages?: string[] };
export type CiRun = { id: string; vendor: string; status: string; repository?: string | null; ref?: string | null;
  external_id?: string | null; recorded_at?: string | null; url?: string | null };
export type CiState = { reachable: boolean; vendors: CiVendor[]; runs: CiRun[]; total: number | null };

export async function loadCi(projectId: string): Promise<CiState> {
  const [cat, st] = await Promise.all([
    read<{ vendors?: CiVendor[] }>("/v1/cicd/catalog"),
    read<{ vendors?: CiVendor[]; recent_runs?: CiRun[]; run_counts?: { total_listed?: number } }>(`/v1/cicd/status?project_id=${enc(projectId)}`),
  ]);
  const vendors = (cat.ok && cat.data.vendors?.length ? cat.data.vendors : st.ok ? st.data.vendors ?? [] : []) ?? [];
  return {
    reachable: cat.ok || st.ok, vendors,
    runs: st.ok ? st.data.recent_runs ?? [] : [],
    total: st.ok && typeof st.data.run_counts?.total_listed === "number" ? st.data.run_counts.total_listed : null,
  };
}
export const generateCi = (f: { vendor: string; language: string; framework?: string }) =>
  send<{ path?: string; content?: string; checksum?: string }>("POST", "/v1/cicd/generate", f, { scope: "infra-ci", label: "Generate" });
export const validateCi = (f: { yaml: string; vendor: string }) =>
  send<{ valid?: boolean; errors?: string[]; warnings?: string[] }>("POST", "/v1/cicd/validate", f, { scope: "infra-ci", label: "Validate" });

// ── links out of Infra ──────────────────────────────────────────────────────────────────────

/** Follow a console href (`?area=…&build=…`) in place, keeping the org; Infra's own drawer keys
 *  are cleared so the other area does not inherit them. */
export function goConsole(href: string): void {
  const patch: Record<string, string | null> = { view: null, deploy: null, preview: null, host: null, target: null, state: null, app: null, env: null };
  for (const [k, v] of new URLSearchParams(href.replace(/^[^?]*\?/, ""))) patch[k] = v;
  navigate(patch);
}

// THE PERMISSION EACH ACTION DECLARES (IDENTITY.md §6.5, R4; lib/identity/guarded-action.ts).
// A declaration for the console to mirror — disabled with the reason when not held — never a
// decision: the API checks every call. tests/identity-mirroring.test.ts keeps this table whole.
declareAction(listTargets, "deployment:read");
declareAction(listPolicies, "deployment:read");
declareAction(loadCi, "cicd:read");
declareAction(generateCi, "cicd:manage");
declareAction(validateCi, "cicd:read");
