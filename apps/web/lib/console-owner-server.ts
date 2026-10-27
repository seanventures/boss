import {
  readDeepLinkTarget,
  resolveDeepLinkOwner,
  type OwnerResolution,
} from "../app/console-owner";

import { API_URL } from "./client";

/**
 * Build L (server side): before app/page.tsx computes `activeProjectId` and fans out its ~55
 * feeds, resolve a deep-linked object to its own project so the fan-out loads the RIGHT project
 * and the panel never renders against the wrong one and flips.
 *
 * The session is forwarded onto `${API_URL}` by lib/identity/server-fetch.ts, so a plain fetch
 * carries the signed-in person's identity; the resolver answers visibility for THEM. Any failure
 * fails open (keep the project we started on): a resolver hiccup must not block the console.
 */
export async function resolveDeepLinkOwnerOnServer(
  search: string,
  currentProjectId: string | null,
): Promise<OwnerResolution> {
  // No API address configured, or no object named: nothing to resolve, keep the current project.
  if (!API_URL || !readDeepLinkTarget(search)) {
    return { target: readDeepLinkTarget(search), projectId: currentProjectId, switched: false, canView: false };
  }
  return resolveDeepLinkOwner({
    search,
    projectId: currentProjectId,
    base: API_URL,
    fetchImpl: (path) => fetch(path, { cache: "no-store", headers: { Accept: "application/json" } }),
  });
}
