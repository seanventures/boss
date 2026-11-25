/**
 * DEV AUTONOMY (BOSSAPP-1441) — the console's typed client for apps/api/app/routers/autonomy.py.
 *
 *   GET  /v1/autonomy/policy?project_id=     the policy in force, the org default, the one line
 *   PUT  /v1/autonomy/policy                 this project's row (stricter than the org only: 422)
 *   PUT  /v1/autonomy/org-policy             the org default (org:update; asks "Confirm it's you")
 *   GET  /v1/autonomy/decided?project_id=    "Decided for you", latest first
 *   POST /v1/autonomy/decided/{id}/undo      409 when it has no undo (the sentence says why)
 *   POST /v1/autonomy/decided/{id}/reviewed  "Looks good"
 *
 * Reads go to the same-origin `/v1` proxy; every write goes through obsWrite → apiWrite (one press,
 * one effect; a step_up_required opens the existing "Confirm it's you").
 */
import { obsWrite, type WriteResult } from "../app/observability/obs-write";
import { aaRead } from "./ambient";

const s = (v: unknown, d = ""): string => (typeof v === "string" ? v : v == null ? d : String(v));
const sn = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const num = (v: unknown, d: number): number => (typeof v === "number" && Number.isFinite(v) ? v : d);
const o = (v: unknown): Record<string, unknown> => (v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {});
const a = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);

export type DevMode = "act" | "act_capped" | "ask";
export const DEV_MODES: DevMode[] = ["act", "act_capped", "ask"];
export const DEV_LABEL: Record<DevMode, string> = { act: "Agents act", act_capped: "Act within cap", ask: "Ask me" };
/** The Setup line's word for each mode. */
export const DEV_WORDS: Record<DevMode, string> = { act: "agents act", act_capped: "agents act within the cap", ask: "you decide" };
const STRICT: Record<DevMode, number> = { act: 0, act_capped: 1, ask: 2 };
/** A project may only be as strict as its org, or stricter. */
export const looserThan = (mode: DevMode, org: DevMode) => STRICT[mode] < STRICT[org];

export type Policy = {
  dev: DevMode; topup: { per_item: number; per_day: number };
  source: "default" | "org" | "project"; set_by: string | null; set_at: string | null; line: string;
};
export type PolicyView = { policy: Policy; org: Policy };

const mode = (v: unknown): DevMode => (DEV_MODES.includes(v as DevMode) ? v as DevMode : "ask");
export function parsePolicy(w: unknown): Policy {
  const x = o(w);
  const t = o(x.dev_topup_usd);
  const src = s(x.source);
  return {
    dev: mode(x.dev), topup: { per_item: num(t.per_item, 0), per_day: num(t.per_day, 0) },
    source: src === "org" || src === "project" ? src : "default",
    set_by: sn(x.set_by), set_at: sn(x.set_at), line: s(x.line),
  };
}
export const parsePolicyView = (w: unknown): PolicyView => ({ policy: parsePolicy(o(w).policy), org: parsePolicy(o(w).org_default) });

export const policyPath = (projectId: string) => `/v1/autonomy/policy?project_id=${encodeURIComponent(projectId)}`;
export const decidedPath = (projectId: string) => `/v1/autonomy/decided?project_id=${encodeURIComponent(projectId)}`;

export const readPolicy = (projectId: string, etag: string | null, signal: AbortSignal) =>
  aaRead(policyPath(projectId), parsePolicyView, etag, signal);

export type PolicyScope = "org" | "project";
export function savePolicy(projectId: string, scope: PolicyScope, dev: DevMode, topup: { per_item: number; per_day: number }): Promise<WriteResult> {
  return obsWrite(scope === "org" ? "/v1/autonomy/org-policy" : "/v1/autonomy/policy",
                  { project_id: projectId, policy: { dev, dev_topup_usd: topup } },
                  { scope: `autonomy:${scope}`, label: "Save" }, "PUT");
}

// ── Decided for you ──────────────────────────────────────────────────────────────────────────

export type Decided = {
  id: string; question: string; agent: string; cls: string | null; why: string | null; action: string | null;
  at: string | null; undo: string | null; noUndo: string | null; undone: boolean; reviewed: boolean;
  links: Record<string, string>;
};
export function parseDecided(w: unknown): Decided[] {
  return a(o(w).items).map((i) => {
    const x = o(i);
    const links: Record<string, string> = {};
    for (const [k, v] of Object.entries(o(x.links))) if (typeof v === "string" && v) links[k] = v;
    return {
      id: s(x.decision_id), question: s(x.question), agent: s(x.agent), cls: sn(x.class), why: sn(x.why), action: sn(x.action),
      at: sn(x.decided_at), undo: sn(x.undo), noUndo: sn(x.undo_unavailable), undone: x.undone === true, reviewed: x.reviewed === true, links,
    };
  }).filter((d) => d.id);
}
export const readDecided = (projectId: string, etag: string | null, signal: AbortSignal) =>
  aaRead(decidedPath(projectId), parseDecided, etag, signal);

export function undoDecided(projectId: string, id: string): Promise<WriteResult> {
  return obsWrite(`/v1/autonomy/decided/${encodeURIComponent(id)}/undo`, { project_id: projectId }, { scope: `autonomy:undo:${id}`, label: "Undo" });
}
export function reviewDecided(projectId: string, id: string): Promise<WriteResult> {
  return obsWrite(`/v1/autonomy/decided/${encodeURIComponent(id)}/reviewed`, { project_id: projectId }, { scope: `autonomy:reviewed:${id}`, label: "Looks good" });
}

/** The agent key as a name ("release_manager" → "Release manager"), as the engine says it. */
export const agentName = (key: string) => {
  const t = (key || "agent").replace(/_/g, " ").trim();
  return t.charAt(0).toUpperCase() + t.slice(1).toLowerCase();
};
/** "Merge PR #412 into develop?" → "Merge PR #412 into develop". */
export const whatHappened = (q: string) => q.replace(/\s+/g, " ").trim().replace(/\?+$/, "");

export const UNDO_LABEL: Record<string, string> = {
  revert_merge: "Revert merge", rollback_deploy: "Roll back", send_back: "Send back", revoke_grant: "Revoke",
  disable_skill: "Disable", remove_self_fix: "Remove", cancel_run: "Stop run",
};

/** The row's Run › link: the workflow run, the build, the deploy, the PR — else the decision. */
export function runLink(d: Decided, projectId: string): { href: string; external: boolean } {
  const q = (p: Record<string, string>) => `?${new URLSearchParams({ project_id: projectId, ...p }).toString()}`;
  if (d.links.workflow_run_id) return { href: q({ area: "delivery_workflows", wfr: d.links.workflow_run_id }), external: false };
  if (d.links.run_id) return { href: q({ area: "code_tasks", view: "apps", build: d.links.run_id }), external: false };
  if (d.links.deployment_run_id) return { href: q({ area: "infra", view: "deploys", deploy: d.links.deployment_run_id }), external: false };
  const pr = d.links.pr_url || d.links.pull_request_url;
  if (pr && /^https?:\/\//.test(pr)) return { href: pr, external: true };
  return { href: q({ area: "decisions", decision: d.id }), external: false };
}

/** Govern's 7 compact cards (the wireframe): a short title, and what still waits for a person
 *  while development acts on its own. Under "Ask me" everything asks, so the card says the
 *  list's own rule instead. An entry the console does not know keeps the list's words. */
const COMPACT: Record<string, { title: string; line: string }> = {
  prod_deploy: { title: "Production deploy", line: "Prod targets only" },
  merge: { title: "Merge", line: "Into a prod-releasing branch only" },
  spend: { title: "Spend", line: "Beyond budget + top-up" },
  secrets: { title: "Secrets", line: "Always" },
  outside: { title: "Public", line: "Publishing outside the org" },
  delete: { title: "Prod infra", line: "Production hosts and data" },
  access: { title: "Access", line: "New grants, wider egress" },
};
export function compactCard(key: string, title: string, line: string, dev: DevMode | null): { title: string; line: string } {
  const c = COMPACT[key];
  if (!c) return { title, line };
  return { title: c.title, line: dev === "ask" || dev === null ? line : c.line };
}
