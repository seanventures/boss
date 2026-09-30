/**
 * What the per-app controls offer, and what they report — as decisions, not markup.
 *
 * Pulled out of the component so it can be tested for real. The judgements here are where this
 * console's control bugs have actually lived, and every one of them was invisible to a test
 * that only checked the reply rendered:
 *
 *   - "retry started" printed whether or not anything started;
 *   - Unbind worked while the card went on saying the target was bound;
 *   - a Deploy button armed beside a card claiming the app was never deployed;
 *   - "No verified host yet" stated before the lookup had returned.
 *
 * The shape of the fix is the same each time: separate WHAT WE KNOW from WHAT WE ARE SAYING,
 * and refuse to say anything in the gap.
 */

export type Deployment = { id?: string; status?: string; app_name?: string };

export type Binding = {
  linked_host?: string | null;
  linked_target_id?: string | null;
  deployments?: Deployment[];
  error?: string;
};

/** Planned runs await a ChangeSet decision; only execution states should poll. */
export const LIVE_STATUSES = new Set(["running", "in_progress", "deploying"]);
const PLAN_STATUSES = new Set(["created", "planned"]);

export type ControlsState =
  | { kind: "checking" }
  | { kind: "unreadable"; reason: string }
  | {
      kind: "ready";
      bound: boolean;
      host: string | null;
      targetId: string | null;
      canDeploy: boolean;
      /** Why not, when canDeploy is false. Never empty in that case. */
      blockedReason: string;
      canRetry: boolean;
      retryDeploymentId: string | null;
      live: boolean;
      waitingForReview: boolean;
      liveStatus: string | null;
    };

/**
 * What to show, given what is known.
 *
 * `binding === null` means the lookup has not returned. That is NOT "no destination": stating
 * absence before looking is exactly the bug that told an operator there was no verified host
 * while one was configured the whole time.
 */
export function controlsState(
  binding: Binding | null,
  opts: { deployable: boolean; deployableReason: string },
): ControlsState {
  if (binding === null) return { kind: "checking" };

  // A failed lookup is not an empty result. Offering actions on a state we could not read is
  // how the wrong app gets deployed — which has happened.
  if (binding.error) return { kind: "unreadable", reason: binding.error };

  const targetId = binding.linked_target_id || null;
  const latest = binding.deployments?.[0];
  const status = latest?.status ? String(latest.status) : null;
  const live = Boolean(status && LIVE_STATUSES.has(status));
  const waitingForReview = Boolean(status && PLAN_STATUSES.has(status));

  return {
    kind: "ready",
    bound: Boolean(targetId),
    host: binding.linked_host || null,
    targetId,
    // Bound AND reviewed AND not already mid-flight. Any one missing and the button would
    // promise something the pipeline cannot honour.
    canDeploy: Boolean(targetId) && opts.deployable && !live && !waitingForReview,
    blockedReason: !targetId
      ? "no destination yet — bind a verified host to deploy this app"
      : !opts.deployable
        ? opts.deployableReason
        : live
          ? "a deployment is already running"
          : waitingForReview
            ? "a deployment ChangeSet is awaiting review"
          : "",
    // Retry is for a deployment that FAILED. Offering it on a success would start a second
    // deployment of the same commit for no reason.
    canRetry: status === "failed" && Boolean(latest?.id),
    retryDeploymentId: status === "failed" ? latest?.id ?? null : null,
    live,
    waitingForReview,
    liveStatus: status,
  };
}

/**
 * What to tell the operator after an action.
 *
 * Takes the RESPONSE, not the intent. "retry started" printed regardless of what came back is
 * how a control ends up lying about work that never began.
 */
export function outcomeMessage(
  res: { ok: boolean; status: number },
  data: unknown,
  onSuccess: string,
): string {
  if (res.ok) return onSuccess;
  const error = (data as { error?: unknown } | null)?.error;
  const text = String(error ?? "").trim();
  // Fall back to the status code rather than a generic apology: an operator can act on a 409
  // and cannot act on "something went wrong".
  return text || `HTTP ${res.status}`;
}

/** The exact body /api/app-deploy expects, per op. Kept beside the state so a control cannot
 *  offer an action whose request it does not know how to build. */
export function deployRequest(
  op: "deploy" | "retry",
  args: {
    projectId: string;
    actor: string;
    runId?: string;
    targetId?: string | null;
    deploymentRunId?: string | null;
  },
): Record<string, unknown> {
  const base = { op, project_id: args.projectId, actor: args.actor };
  return op === "deploy"
    ? { ...base, run_id: args.runId, target_id: args.targetId || undefined, environment: "staging" }
    : { ...base, deployment_run_id: args.deploymentRunId };
}
