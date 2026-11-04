/**
 * Govern's writes, by name: the method, path and body each op sends to the API. ONE table, read
 * by the route handler (app/api/govern/route.ts) and by the request-contract test, which holds
 * every body to the one the old Govern components sent (plus the two decided tightenings).
 */
const str = (v: unknown) => (v === undefined || v === null ? "" : String(v));
const enc = (v: unknown) => encodeURIComponent(str(v));

export type GovernOp = { method: "PUT" | "POST"; path: (b: Record<string, unknown>) => string;
            body: (b: Record<string, unknown>) => Record<string, unknown> };


export const GOVERN_OPS: Record<string, GovernOp> = {
  // The knob: the body SettingsKnobs sent, to the path /api/settings forwarded it to.
  setting: { method: "PUT", path: (b) => `/v1/settings/${enc(b.key)}`,
             body: (b) => ({ key: b.key, project_id: b.project_id, actor: b.actor,
                             value: b.value === "" || b.value === undefined ? null : b.value }) },
  // Pace: setBudgetIntensityAction's body, now with the signed-in actor (spec §10 Q4).
  pace: { method: "POST", path: () => "/v1/budget/set_intensity",
          body: (b) => ({ scope_id: b.project_id, intensity: b.intensity, actor: b.actor }) },
  pace_preview: { method: "POST", path: () => "/v1/budget/intensity/preview",
                  body: (b) => ({ intensity: b.intensity, project_id: b.project_id }) },
  cap_preview: { method: "POST", path: () => "/v1/budget/cap/preview",
                 body: (b) => ({ project_id: b.project_id, period: b.period, cap_usd: b.cap_usd }) },
  // The monthly cap: Setup's Budget row's write (the same route), saying it came from Govern.
  monthly_cap: { method: "PUT", path: () => "/v1/setup/budget",
                 body: (b) => ({ project_id: b.project_id, monthly_cap_usd: b.monthly_cap_usd, via: "govern", actor: b.actor }) },
  // Token guides: setStageTokenCapAction's body, now with the signed-in actor (spec §10 Q3).
  token_cap: { method: "PUT", path: () => "/v1/token-efficiency/policies",
               body: (b) => ({ project_id: b.project_id, actor: b.actor,
                               policies: [{ stage: b.stage, max_total_tokens_per_call: b.max_total_tokens_per_call }] }) },
  // FinOps: the FinopsCard's posts, with the signed-in actor instead of "system" (§10 Q4).
  reconcile: { method: "POST", path: () => "/v1/finops/reconcile-commitments",
               body: (b) => ({ project_id: b.project_id, actor: b.actor }) },
  initiative: { method: "POST", path: () => "/v1/finops/initiative-budget",
                body: (b) => ({ project_id: b.project_id, title: b.title, tier: b.tier,
                                amount_usd_micros: b.amount_usd_micros, actor: b.actor }) },
  // Govern › Limits' one Save (BOSSAPP-807): only the fields the screen changed are sent.
  limits: { method: "POST", path: () => "/v1/govern/limits",
            body: (b) => {
              const out: Record<string, unknown> = { project_id: b.project_id, actor: b.actor };
              for (const k of ["monthly_cap_usd", "daily_cap_usd", "burst_usd_per_min", "at_limit"]) {
                if (b[k] !== undefined && b[k] !== null && b[k] !== "") out[k] = b[k];
              }
              return out;
            } },
  // A project's monthly limit: a project budget envelope; null is "No limit".
  project_limit: { method: "POST", path: () => "/v1/govern/project-limit",
                   body: (b) => ({ project_id: b.project_id, actor: b.actor,
                                   monthly_limit_usd: b.monthly_limit_usd === "" || b.monthly_limit_usd === undefined
                                     ? null : b.monthly_limit_usd }) },
};

