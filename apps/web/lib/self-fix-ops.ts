/**
 * BOSS fixes itself (docs/product/BOSS_FIXES_ITSELF.md §10, §11.4): every read and write BY NAME.
 * The route handler app/api/self-fix/route.ts reads this table; the browser cannot reach any other
 * API path through it. Approval is NOT here: a self-fix install is answered in Approvals like any
 * decision (the decisions API), never by a self-fix route.
 */
const str = (v: unknown) => (v === undefined || v === null ? "" : String(v));
const enc = (v: unknown) => encodeURIComponent(str(v));

export type SelfFixOp = { method: "PUT" | "POST"; path: (b: Record<string, unknown>) => string;
  body: (b: Record<string, unknown>) => Record<string, unknown> };

const who = (b: Record<string, unknown>) => ({ project_id: b.project_id, actor: b.actor });

export const SELF_FIX_OPS: Record<string, SelfFixOp> = {
  start: { method: "POST", path: () => "/v1/self-fixes",
           body: (b) => ({ ...who(b), recommendation_id: b.recommendation_id }) },
  cancel: { method: "POST", path: (b) => `/v1/self-fixes/${enc(b.id)}/cancel`,
            body: (b) => ({ ...who(b), reason: b.reason ?? null }) },
  settings: { method: "PUT", path: () => "/v1/self-fix/settings",
              body: (b) => ({ ...who(b),
                              ...(typeof b.on === "boolean" ? { on: b.on } : {}),
                              ...(b.settings && typeof b.settings === "object" ? { settings: b.settings } : {}),
                              ...(b.why ? { why: b.why } : {}) }) },
  preview: { method: "POST", path: (b) => `/v1/self-fixes/${enc(b.id)}/upstream/preview`, body: who },
  edit_preview: { method: "PUT", path: (b) => `/v1/self-fixes/${enc(b.id)}/upstream/preview`,
                  body: (b) => ({ ...who(b), send_id: b.send_id,
                                  ...(typeof b.body === "string" ? { body: b.body } : {}),
                                  ...(typeof b.evidence === "string" ? { evidence: b.evidence } : {}) }) },
  // `checked` is sent only as the literal the person ticked: never defaulted to true here.
  send: { method: "POST", path: (b) => `/v1/self-fixes/${enc(b.id)}/upstream/send`,
          body: (b) => ({ ...who(b), send_id: b.send_id, checked: b.checked === true }) },
  fork: { method: "POST", path: (b) => `/v1/self-fixes/${enc(b.id)}/upstream/fork`, body: who },
  remove: { method: "POST", path: (b) => `/v1/self-fixes/${enc(b.id)}/remove`, body: who },
  remove_all: { method: "POST", path: () => "/v1/self-fixes/remove-all", body: who },
};

/** GET views: name → API path from the query. `raw` ones are passed through as a download. */
export const SELF_FIX_READS: Record<string, { path: (q: URLSearchParams) => string; raw?: boolean }> = {
  list: { path: (q) => `/v1/self-fixes?project_id=${enc(q.get("project_id"))}`
                     + (q.get("status") ? `&status=${enc(q.get("status"))}` : "") },
  one: { path: (q) => `/v1/self-fixes/${enc(q.get("id"))}?project_id=${enc(q.get("project_id"))}` },
  diff: { path: (q) => `/v1/self-fixes/${enc(q.get("id"))}/diff?project_id=${enc(q.get("project_id"))}` },
  settings: { path: (q) => `/v1/self-fix/settings?project_id=${enc(q.get("project_id"))}` },
  upstream: { path: (q) => `/v1/self-fixes/${enc(q.get("id"))}/upstream?project_id=${enc(q.get("project_id"))}` },
  patch: { raw: true, path: (q) => `/v1/self-fixes/${enc(q.get("id"))}/patch?project_id=${enc(q.get("project_id"))}` },
  // What BOSS recorded about its own bugs (services/boss_recommendations.py): the ones a self-fix
  // did not start for, and the way a person starts one by hand.
  recommendations: { path: (q) => `/v1/boss-recommendations?project_id=${enc(q.get("project_id"))}` },
  autonomy: { path: (q) => `/v1/autonomy?project_id=${enc(q.get("project_id"))}&window=${enc(q.get("window") || "30d")}` },
  run_autonomy: { path: (q) => `/v1/autonomy/runs/${enc(q.get("run_id"))}?project_id=${enc(q.get("project_id"))}` },
};
