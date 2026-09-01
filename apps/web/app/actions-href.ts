/**
 * Where the console sends you, as a plain string.
 *
 * Kept out of actions.ts because a "use server" module may only export async server actions —
 * and kept a pure function so the destination is testable without a browser or a server.
 */

/**
 * The Workflow area, opened on a particular run when we know which one.
 *
 * `orgId` is not optional decoration: the console gates on `?org=`, so a redirect without it
 * lands the operator on "Select your organization" — which reads as being logged out in the
 * middle of starting a project.
 */
export function workflowHref(workflowRunId: string | null, orgId?: string | null): string {
  const params = new URLSearchParams();
  if (orgId) params.set("org", orgId);
  params.set("area", "delivery_workflows");
  if (workflowRunId) params.set("wfr", workflowRunId);
  return `/?${params.toString()}`;
}

/** The org the operator is working as, lifted from the page that submitted the form.
 *
 *  A server action has no URL of its own; the referer is the page the operator was on, and it
 *  carries the org because the console cannot render without one. */
export function orgFromReferer(referer: string | null | undefined): string | null {
  if (!referer) return null;
  try {
    const url = new URL(referer);
    return url.searchParams.get("org") || url.searchParams.get("org_id");
  } catch {
    return null;
  }
}
