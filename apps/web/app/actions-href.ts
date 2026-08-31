/**
 * Where the console sends you, as a plain string.
 *
 * Kept out of actions.ts because a "use server" module may only export async server actions —
 * and kept a pure function so the destination is testable without a browser or a server.
 */

/** The Workflow area, opened on a particular run when we know which one. */
export function workflowHref(workflowRunId: string | null): string {
  const params = new URLSearchParams({ area: "delivery_workflows" });
  if (workflowRunId) params.set("wfr", workflowRunId);
  return `/?${params.toString()}`;
}
