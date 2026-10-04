/**
 * Repo and models, as the page server reads it: the landing rule and the rail badge.
 *
 * One GET /v1/setup per render (no network behind it on the API side). Any failure answers
 * "nothing to say" — the console never blocks its own render on the setup summary.
 */
import { API_URL, authHeaders } from "./client";
import { summarize, type SetupSummary } from "./setup-stepper";
import type { SetupView } from "./setup-types";

export { summarize, type SetupSummary };

export async function fetchSetupSummary(projectId: string): Promise<SetupSummary | null> {
  try {
    const r = await fetch(`${API_URL}/v1/setup?project_id=${encodeURIComponent(projectId)}`, {
      headers: authHeaders({}), cache: "no-store",
    });
    if (!r.ok) return null;
    return summarize((await r.json()) as SetupView);
  } catch {
    return null;
  }
}

/** Where a sign-in with no `area` lands: Repo and models while setup is not ready. */
export function landingArea(requestedArea: string | undefined, summary: SetupSummary | null): string | null {
  if (requestedArea && requestedArea.trim()) return null;
  return summary && summary.ready === false ? "setup" : null;
}
