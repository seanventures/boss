import { createBatcher } from "./batch-fetch";

/**
 * Deployment state for the app cards — one request for all of them.
 *
 * Each card asked for its own state on mount via `runDeploymentsAction`. A Server Action
 * re-renders the whole route to reply, so forty cards meant forty renders of a page that takes
 * tens of seconds to render. Measured on the live Factory area with no interaction at all:
 * 82 invocations of that one action, part of 64.5s of total action time, page not quiet for
 * 74 seconds. That is the "takes 30s to a minute to render fully" complaint.
 *
 * The host list was moved to a route handler for exactly this reason and its comment says so;
 * this is the per-card path that was missed, because one extra call per card looks cheap right
 * up until there are forty cards.
 */

export type RunDeployment = {
  deployments?: unknown[];
  linked_host?: string | null;
  linked_target_id?: string | null;
  error?: string;
};

const batcher = createBatcher<RunDeployment>({
  url: (ids, names) => {
    const query = new URLSearchParams({ run_ids: ids.join(",") });
    // One parameter per run. Joining names with commas confused an app named "Costs, Q3"
    // with two apps and paired the next run with the wrong identity.
    names.forEach((name) => query.append("app_name", name));
    return `/api/run-deployments?${query.toString()}`;
  },
  pick: (body, id) => (body as { runs?: Record<string, RunDeployment> })?.runs?.[id],
  // Under the cards' own 4s poll, or polling would stall on a cached answer.
  ttlMs: 2_500,
  // Matches MAX_IDS in app/api/run-deployments/route.ts.
  maxPerRequest: 60,
  maxConcurrentRequests: 2,
});

/**
 * Deployment state for one run, answered by a shared batched request.
 *
 * The app name rides along because several apps share one repo and the upstream endpoint
 * disambiguates by name — without it, a deploy once reported success for a different app.
 *
 * Never rejects: resolves with `{ error }` so a caller cannot forget to catch and leave a card
 * claiming "not deployed" because a lookup failed.
 */
export function loadRunDeployment(runId: string, appName = ""): Promise<RunDeployment> {
  const id = (runId ?? "").trim();
  if (!id) return Promise.resolve({ error: "missing run id" });
  return batcher.load(id, appName);
}

/** Drop a run's cached answer so the next ask is a real one. Used after Deploy / Unbind /
 *  Retry, where a stale reply would render the world as it was before the action. */
export function forgetRunDeployment(runId: string): void {
  batcher.forget(runId);
}

/** Test seam: reset module state between cases. */
export function __resetRunDeploymentCache(): void {
  batcher.reset();
}
