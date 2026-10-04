import {
  BossApiError,
  type DiscoveryListItem,
  type DiscoveryRun,
  type DiscoverySummary,
} from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";
import { clipText } from "./list-budget";

export type DiscoveryFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: DiscoveryListItem[];
};

export function emptyDiscoveryFeed(reason?: string, projectId = PROJECT_ID): DiscoveryFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

type AnyDiscoveryRow = DiscoveryListItem | (DiscoveryRun & { summary?: DiscoverySummary });

/**
 * A list row, whatever the API sent. The API returns summaries (no `state`); a row that still
 * carries the dossier — an API older than this console — is summarised here, so the page never
 * serialises a dossier into a refresh either way (lib/list-budget.ts).
 */
export function toDiscoveryListItem(row: AnyDiscoveryRow): DiscoveryListItem {
  const { state, context: _context, summary, ...rest } =
    row as DiscoveryRun & { summary?: DiscoverySummary };
  const st = state ?? {};
  const attention = rest.attention
    ? { ...rest.attention, detail: clipText(rest.attention.detail) ?? undefined }
    : rest.attention;
  return {
    ...rest,
    title: clipText(rest.title) ?? "",
    seed: clipText(rest.seed),
    error: clipText(rest.error),
    usage: rest.usage
      ? { cost_usd_micros: rest.usage.cost_usd_micros, model: rest.usage.model }
      : rest.usage,
    attention,
    summary: summary ?? {
      verdict: st.gate?.verdict ?? null,
      confidence: typeof st.gate?.confidence === "number" ? st.gate.confidence : null,
      findings: Array.isArray(st.signal_findings) ? st.signal_findings.length : 0,
      outcomes: Array.isArray(st.candidate_outcomes) ? st.candidate_outcomes.length : 0,
      deep: Boolean((st as { deep?: boolean }).deep),
    },
  };
}

export async function fetchDiscoveriesFeed(projectId = PROJECT_ID, active = true): Promise<DiscoveryFeed> {
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return emptyDiscoveryFeed("not requested", resolveProjectId(projectId));
  const resolved = resolveProjectId(projectId);
  try {
    const res = await client.getDiscoveries(resolved);
    return {
      projectId: resolved,
      apiReachable: true,
      items: (res.discoveries ?? []).map((row) => toDiscoveryListItem(row as AnyDiscoveryRow)),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyDiscoveryFeed(error.message, resolved);
    }
    return emptyDiscoveryFeed("API unreachable", resolved);
  }
}
