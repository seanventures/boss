/**
 * The projects an access role can be bound in, for the Add access and group pickers (server-only).
 * Unreadable (no project:read, API down) → null, and the sheet falls back to typing the project.
 */
import { fetchProjectFeed } from "../../../lib/projects";
import type { ProjectChoice } from "../../../lib/identity/types-people";

export async function loadProjectChoices(): Promise<ProjectChoice[] | null> {
  try {
    const feed = await fetchProjectFeed();
    if (!feed.apiReachable || !feed.items.length) return null;
    return feed.items.map((p) => ({ id: p.id, name: p.name || p.slug || p.id }));
  } catch {
    return null;
  }
}
