import { API_URL, PROJECT_ID, resolveProjectId, authHeaders } from "./client";

// Phase 3 inbound acknowledgment ledger (GET /v1/comms/inbox/ack-state) — read directly.

export type AckState = {
  id: string;
  inboxMessageId: string;
  channel: string;
  state: string; // received | parsed | routed | actioned | deadlettered | acknowledged
  terminal: boolean;
  routeKind: string | null;
  targetRef: string | null;
  reason: string | null;
  remediation: string | null;
  updatedAt: string;
};

export async function fetchAckState(
  projectId = PROJECT_ID,
  limit = 25,
  pendingOnly = false,
  active = true,
): Promise<AckState[]> {
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return [];
  try {
    const r = await fetch(
      `${API_URL}/v1/comms/inbox/ack-state?project_id=${encodeURIComponent(resolveProjectId(projectId))}&limit=${limit}&pending_only=${pendingOnly}`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!r.ok) return [];
    const items = (await r.json()).items ?? [];
    return items.map((it: Record<string, unknown>) => ({
      id: String(it.id),
      inboxMessageId: String(it.inbox_message_id),
      channel: String(it.channel),
      state: String(it.state),
      terminal: Boolean(it.terminal),
      routeKind: it.route_kind != null ? String(it.route_kind) : null,
      targetRef: it.target_ref != null ? String(it.target_ref) : null,
      reason: it.reason != null ? String(it.reason) : null,
      remediation: it.remediation != null ? String(it.remediation) : null,
      updatedAt: String(it.updated_at ?? ""),
    }));
  } catch {
    return [];
  }
}
