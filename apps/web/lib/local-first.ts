/**
 * LOCAL-FIRST PROJECTS in the console (BOSSAPP-915): a project whose code BOSS keeps on this
 * machine, and Publish to GitHub (BOSSAPP-921).
 *
 *   GET  /v1/projects/{id}/local-repo                 is the code local? its branches, open changes
 *   GET  /v1/projects/{id}/github-repository/check    can BOSS make (or publish into) owner/repo?
 *   POST /v1/projects/{id}/publish                    a person's decision: make the repo, push the
 *                                                     whole history, open PRs for open changes, switch
 *
 * Publish is idempotent and resumable: a refusal names the step it stopped at, and the same press
 * again starts there. The sheet draws that as a small diagram (publishSteps).
 */
import { enc, idGet, idPost, IdentityError } from "./identity/api";

export type LocalRepo = {
  project_id: string; local: boolean; binding_id?: string; full_name?: string; role?: string; exists?: boolean;
  default_branch?: string; head_sha?: string | null; branches?: string[]; open_change_sets?: number;
  published?: { to?: string; by?: string } | null;
};

export type PublishBody = { owner: string; repo: string; private: boolean; create: boolean };
export type PublishResult = {
  project_id: string; status: string; full_name: string; pull_requests?: Record<string, string>;
  steps: { key: string; ok: boolean; words: string }[]; words: string;
};
export type PublishRefusal = { error: string; message: string; step: string };

export const PUBLISH_STEPS = [
  { key: "repository", label: "Create repo", short: "repo" },
  { key: "push", label: "Push history", short: "push" },
  { key: "pull_requests", label: "Open PRs", short: "PRs" },
  { key: "switch", label: "Switch", short: "switch" },
] as const;
export type PublishStepKey = (typeof PUBLISH_STEPS)[number]["key"];
export type StepState = "todo" | "doing" | "done" | "skipped" | "failed";

/** Each step's state: while it runs (`busy`), after it answered (`result`), or after a refusal
 *  (`refusal.step`: the steps before it are done, it failed, the rest wait). Publishing into an
 *  existing repository skips "Create repo"; no open changes skips "Open PRs". */
export function publishSteps(opts: { busy?: boolean; create: boolean; result?: PublishResult | null;
                                     refusal?: PublishRefusal | null; openChanges?: number | null }): Record<PublishStepKey, StepState> {
  const out = {} as Record<PublishStepKey, StepState>;
  const keys = PUBLISH_STEPS.map((s) => s.key);
  for (const k of keys) out[k] = "todo";
  if (!opts.create) out.repository = "skipped";
  if (opts.result) {
    const said = new Set(opts.result.steps.map((s) => s.key));
    for (const k of keys) {
      if (k === "repository" && !opts.create) continue;
      out[k] = said.has(k) || opts.result.status === "published" ? "done" : "todo";
    }
    if (!said.has("pull_requests") && !Object.keys(opts.result.pull_requests ?? {}).length) out.pull_requests = "skipped";
    return out;
  }
  if (opts.refusal) {
    const at = keys.indexOf((opts.refusal.step === "input" ? "repository" : opts.refusal.step) as PublishStepKey);
    keys.forEach((k, i) => {
      if (k === "repository" && !opts.create) return;
      out[k] = at < 0 ? "todo" : i < at ? "done" : i === at ? "failed" : "todo";
    });
    return out;
  }
  if (opts.busy) {
    const first = keys.find((k) => out[k] === "todo");
    if (first) out[first] = "doing";
    if (opts.openChanges === 0) out.pull_requests = "skipped";
  }
  return out;
}

export const localFirstApi = {
  localRepo: (projectId: string, signal?: AbortSignal) =>
    idGet<LocalRepo>(`/v1/projects/${enc(projectId)}/local-repo`, { signal }),
  publish: async (projectId: string, body: PublishBody): Promise<{ ok: true; data: PublishResult } | { ok: false; refusal: PublishRefusal }> => {
    try {
      return { ok: true, data: await idPost<PublishResult>(`/v1/projects/${enc(projectId)}/publish`, body) };
    } catch (e) {
      // A step-up (or a sign-in) is the caller's; GitHub's and the engine's refusals are words.
      if (e instanceof IdentityError && (e.code === "step_up_required" || e.status === 401 || e.code === "step_up_cancelled")) throw e;
      if (e instanceof IdentityError) {
        const b = e.body as Record<string, unknown>;
        const d = (b.detail && typeof b.detail === "object" ? b.detail : {}) as Record<string, unknown>;
        const message = String(b.message || d.message || e.message || "Publishing was refused.");
        return { ok: false, refusal: { error: String(b.error || d.error || "refused"), message, step: String(b.step || d.step || "") } };
      }
      throw e;
    }
  },
};
