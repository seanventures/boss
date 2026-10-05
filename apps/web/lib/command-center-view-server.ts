/**
 * The Command Center view's SERVER side: the first paint's fetch, and the two names the page
 * and the poll share. Nothing here may import a client module — page.tsx is a server
 * component, and lib/command-center-view.ts pulls in React hooks (the poll, Live's parsers),
 * which Next refuses in a server graph. So the server hands the client the WIRE, and the client
 * component parses it with the one parser (parseCommandCenterView).
 */

/** The cookie the page writes so the server's first paint uses the viewer's zone too. */
export const TZ_COOKIE = "boss_tz";

export function viewUrl(projectId: string, tz: string, viewer?: string | null): string {
  return `/v1/command-center/view?project_id=${encodeURIComponent(projectId)}&tz=${encodeURIComponent(tz)}`
    + (viewer ? `&viewer=${encodeURIComponent(viewer)}` : "");
}

/** Server-side first paint: the view as the API sent it (unparsed), or why it could not be read. */
export async function fetchCommandCenterView(projectId: string, tz: string, viewer?: string | null,
                                             enabled = true):
    Promise<{ wire: unknown; everRan: boolean | null; error: string | null }> {
  if (!enabled || !projectId) return { wire: null, everRan: null, error: null };
  const { API_URL, authHeaders } = await import("./client");
  try {
    const r = await fetch(`${API_URL}${viewUrl(projectId, tz, viewer)}`,
                          { headers: authHeaders({}), cache: "no-store" });
    if (!r.ok) {
      let said = "";
      try { said = String(((await r.json()) as { detail?: unknown }).detail ?? ""); } catch { /* none */ }
      return { wire: null, everRan: null, error: said || `The API answered ${r.status}.` };
    }
    const wire = (await r.json()) as { today?: { ever_ran?: unknown } } | null;
    return { wire, everRan: wire?.today?.ever_ran === true, error: null };
  } catch (e) {
    return { wire: null, everRan: null, error: e instanceof Error ? e.message : "API unreachable" };
  }
}
