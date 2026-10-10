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

/** Server-side first paint of the Factory's apps (the hero's numbers, the shelf): the wire and
 *  its ETag, or null — the client's poll then reads it. Never blocks the page on a failure. */
export async function fetchCommandCenterApps(projectId: string, enabled = true):
    Promise<{ wire: unknown; etag: string | null } | null> {
  if (!enabled || !projectId) return null;
  const { API_URL, authHeaders } = await import("./client");
  try {
    const r = await fetch(`${API_URL}/v1/factory/apps?project_id=${encodeURIComponent(projectId)}`,
                          { headers: authHeaders({}), cache: "no-store" });
    if (!r.ok) return null;
    return { wire: slimCcAppsWire(await r.json()), etag: r.headers.get("etag") };
  } catch {
    return null;
  }
}

/**
 * The Factory's apps as the Command Center's FIRST PAINT needs them (console-latency fix,
 * 2026-10-09). /v1/factory/apps draws every attempt of every app with its whole station line —
 * ~470 KB for 35 apps on the seeded stack — and the server render serialised all of it into the
 * Overview's HTML (1.16 MB). parseCcApps reads an attempt's run_id and state and nothing else, so
 * that is all that ships; the poll after it (same ETag) parses the full answer to the same
 * cards. tests/command-center-ships-slim-apps.test.ts pins it against parseCcApps.
 */
export function slimCcAppsWire(wire: unknown): unknown {
  if (!wire || typeof wire !== "object" || !Array.isArray((wire as { apps?: unknown }).apps)) return wire;
  const w = wire as { apps: unknown[] };
  return {
    ...w,
    apps: w.apps.map((app) => {
      if (!app || typeof app !== "object") return app;
      const a = app as Record<string, unknown>;
      const attempts = Array.isArray(a.attempts)
        ? a.attempts.map((x) => {
            const y = (x && typeof x === "object" ? x : {}) as Record<string, unknown>;
            return { run_id: y.run_id, state: y.state };
          })
        : a.attempts;
      return { ...a, attempts };
    }),
  };
}
