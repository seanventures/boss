/**
 * Improve BOSS, scorecards, experiments and feedback: every read and write BY NAME (the route
 * handler app/api/improve/route.ts reads this table; the browser cannot reach any other API path
 * through it). docs/product/RUN_SCORECARDS.md, "Implementation" → "Routes".
 */
const str = (v: unknown) => (v === undefined || v === null ? "" : String(v));
const enc = (v: unknown) => encodeURIComponent(str(v));

export type ImproveOp = { method: "PUT" | "POST"; path: (b: Record<string, unknown>) => string;
  body: (b: Record<string, unknown>) => Record<string, unknown> };

const who = (b: Record<string, unknown>) => ({ project_id: b.project_id, actor: b.actor });

export const IMPROVE_OPS: Record<string, ImproveOp> = {
  switch: { method: "PUT", path: () => "/v1/telemetry/sharing",
            body: (b) => ({ ...who(b), switch: b.switch, on: b.on === true }) },
  weights: { method: "PUT", path: () => "/v1/telemetry/sharing",
             body: (b) => ({ ...who(b), weights: b.weights }) },
  collector_url: { method: "PUT", path: () => "/v1/telemetry/sharing",
                   body: (b) => ({ ...who(b), url: b.url ?? "" }) },
  mask_words: { method: "PUT", path: () => "/v1/telemetry/sharing",
                body: (b) => ({ ...who(b), mask_words: b.words ?? "" }) },
  rotate: { method: "POST", path: () => "/v1/telemetry/rotate", body: who },
  forget: { method: "POST", path: () => "/v1/telemetry/forget", body: who },
  share_run: { method: "POST", path: () => "/v1/telemetry/share-run",
               body: (b) => ({ ...who(b), bundle: b.bundle, note: b.note }) },
  backfill: { method: "POST", path: () => "/v1/scorecards/backfill", body: who },
  experiment: { method: "POST", path: () => "/v1/experiments",
                body: (b) => ({ ...who(b), name: b.name, target_kind: b.target_kind,
                                target_key: b.target_key, knob: b.knob, arm_a: b.arm_a,
                                arm_b: b.arm_b, split_b: b.split_b, target_units: b.target_units,
                                arm_cost_cap_usd: b.arm_cost_cap_usd }) },
  experiment_stop: { method: "POST", path: (b) => `/v1/experiments/${enc(b.id)}/stop`, body: who },
  experiment_promote: { method: "POST", path: (b) => `/v1/experiments/${enc(b.id)}/promote`,
                        body: (b) => ({ ...who(b), arm: b.arm, anyway: b.anyway === true }) },
  feedback: { method: "POST", path: () => "/v1/feedback",
              body: (b) => ({ ...who(b), subject_kind: b.subject_kind, subject_id: b.subject_id,
                              rating: b.rating ?? null, chips: b.chips ?? [],
                              decision_kind: b.decision_kind ?? null,
                              ...(b.edit_ratio !== undefined ? { edit_ratio: b.edit_ratio } : {}) }) },
  prompt_shown: { method: "POST", path: () => "/v1/feedback/prompts/shown",
                  body: (b) => ({ ...who(b), prompt: b.prompt }) },
  checkin: { method: "POST", path: () => "/v1/feedback/checkin",
             body: (b) => ({ ...who(b), answer: b.answer ?? null, line: b.line ?? null,
                             share_line: b.share_line === true, dismiss: b.dismiss === true }) },
};

/** GET views: name → API path from the query. `raw` ones are passed through as a download. */
export const IMPROVE_READS: Record<string, { path: (q: URLSearchParams) => string; raw?: boolean }> = {
  sharing: { path: (q) => `/v1/telemetry/sharing?project_id=${enc(q.get("project_id"))}` },
  preview: { path: (q) => `/v1/telemetry/preview?project_id=${enc(q.get("project_id"))}` },
  send: { path: (q) => `/v1/telemetry/sends/${enc(q.get("id"))}?project_id=${enc(q.get("project_id"))}` },
  export: { raw: true, path: (q) => `/v1/telemetry/export?project_id=${enc(q.get("project_id"))}&actor=${enc(q.get("actor"))}` },
  scorecards: { path: (q) => `/v1/scorecards?project_id=${enc(q.get("project_id"))}&group=${enc(q.get("group") || "role")}&win=${enc(q.get("win") || "30d")}&kind=${enc(q.get("kind") || "build")}` },
  experiments: { path: (q) => `/v1/experiments?project_id=${enc(q.get("project_id"))}` },
  prompts: { path: (q) => `/v1/feedback/prompts?project_id=${enc(q.get("project_id"))}&actor=${enc(q.get("actor"))}&subjects=${enc(q.get("subjects"))}` },
  collector: { path: (q) => `/v1/telemetry/collector?project_id=${enc(q.get("project_id"))}` },
  collector_csv: { raw: true, path: (q) => `/v1/telemetry/collector.csv?project_id=${enc(q.get("project_id"))}` },
  share_bundle: { path: (q) => `/v1/telemetry/share-bundle?project_id=${enc(q.get("project_id"))}&unit_kind=${enc(q.get("unit_kind"))}&unit_id=${enc(q.get("unit_id"))}&actor=${enc(q.get("actor"))}` },
};
