/**
 * A workflow's webhook triggers (#512), for the designer's Trigger panel.
 *
 * The calls go through the Workflows proxy (`/api/delivery-workflows`) like everything else on
 * the screen. The signing secret arrives in exactly two replies — create and rotate — and is
 * kept only in the panel's state until it is dismissed; the list never carries it.
 *
 * SENDERS DO NOT POST TO THE CONSOLE. The console's `/v1` pass-through drops `X-BOSS-*` headers
 * by design, so a signature would never arrive: the URL a sender uses is the BOSS API's own
 * public address plus `path`. `curlExample` writes that with a placeholder host.
 */
import { apiWrite } from "./api-write";

export type WebhookTrigger = {
  id: string;
  workflow_key: string;
  name: string;
  path: string;
  enabled: boolean;
  secret_last4?: string | null;
  refused_unrecorded?: number;
  last_delivery_at?: string | null;
};

export type WebhookTriggerWithSecret = WebhookTrigger & { secret: string };

export type WebhookDelivery = {
  id: string;
  delivery_id?: string | null;
  status: "started" | "duplicate" | "refused";
  reason?: string | null;
  run_id?: string | null;
  received_at: string;
};

type Result<T> = { ok: true; data: T } | { ok: false; error: string };

async function read<T>(params: Record<string, string>): Promise<Result<T>> {
  try {
    const r = await fetch(`/api/delivery-workflows?${new URLSearchParams(params)}`, { cache: "no-store" });
    const body = await r.json().catch(() => ({}));
    return r.ok ? { ok: true, data: body as T } : { ok: false, error: String(body?.error ?? `HTTP ${r.status}`) };
  } catch (e) {
    return { ok: false, error: String((e as Error)?.message ?? e) };
  }
}

async function write<T>(payload: Record<string, unknown>): Promise<Result<T>> {
  try {
    const r = await apiWrite("/api/delivery-workflows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await r.json().catch(() => ({}));
    return r.ok ? { ok: true, data: body as T } : { ok: false, error: String(body?.error ?? `HTTP ${r.status}`) };
  } catch (e) {
    return { ok: false, error: String((e as Error)?.message ?? e) };
  }
}

/** Where a sender's API address goes in the examples. */
export const API_HOST_PLACEHOLDER = "https://YOUR-BOSS-API";

/** The words for a refusal reason, short enough for a row. */
const REASONS: Record<string, string> = {
  bad_signature: "Bad signature",
  missing_signature: "Not signed",
  stale_timestamp: "Stale timestamp",
  too_large: "Too large",
  disabled: "Trigger off",
  unsupported_content_type: "Not JSON",
  invalid_json: "Not a JSON object",
  not_ready: "Workflow not ready",
  workflow_missing: "Not published",
  rate_limited: "Rate limited",
  start_failed: "Start failed",
};

/** What one delivery row says: Started, Duplicate, or the refusal's reason. */
export function deliveryLabel(d: Pick<WebhookDelivery, "status" | "reason">): string {
  if (d.status === "started") return "Started";
  if (d.status === "duplicate") return "Duplicate";
  return REASONS[d.reason ?? ""] ?? (d.reason ? d.reason.replace(/_/g, " ") : "Refused");
}

export function deliveryTone(d: Pick<WebhookDelivery, "status">): "ok" | "muted" | "bad" {
  return d.status === "started" ? "ok" : d.status === "duplicate" ? "muted" : "bad";
}

/** The full URL a sender POSTs to, on the given API origin. */
export function inboundUrl(path: string, apiOrigin = API_HOST_PLACEHOLDER): string {
  return `${apiOrigin.replace(/\/$/, "")}${path.startsWith("/") ? path : `/${path}`}`;
}

/**
 * A copy-paste example that signs and sends one delivery: HMAC-SHA256 over
 * "<timestamp>.<raw body>", hex, as `X-BOSS-Signature: v1=<hex>`. The secret is read from an
 * environment variable, never pasted into the command (shell history keeps commands).
 */
export function curlExample(path: string, apiOrigin = API_HOST_PLACEHOLDER): string {
  return [
    "# export BOSS_WEBHOOK_SECRET=whsec_...  (shown once, when the trigger was made or rotated)",
    "BODY='{\"example\": \"value\"}'",
    "TS=$(date +%s)",
    "SIG=$(printf '%s.%s' \"$TS\" \"$BODY\" | openssl dgst -sha256 -hmac \"$BOSS_WEBHOOK_SECRET\" | sed 's/^.* //')",
    `curl -sS -X POST '${inboundUrl(path, apiOrigin)}' \\`,
    "  -H 'Content-Type: application/json' \\",
    "  -H \"X-BOSS-Timestamp: $TS\" \\",
    "  -H \"X-BOSS-Signature: v1=$SIG\" \\",
    "  -H \"X-BOSS-Delivery: $(uuidgen)\" \\",
    "  --data-binary \"$BODY\"",
  ].join("\n");
}

export function listWebhooks(projectId: string, workflowKey: string) {
  return read<{ webhooks: WebhookTrigger[] }>({ view: "webhooks", project_id: projectId, key: workflowKey });
}

export function listDeliveries(projectId: string, webhookId: string) {
  return read<{ deliveries: WebhookDelivery[] }>({
    view: "webhook_deliveries", project_id: projectId, webhook_id: webhookId,
  });
}

export function createWebhook(projectId: string, workflowKey: string, name: string, actor?: string) {
  return write<WebhookTriggerWithSecret>({
    action: "webhook_create", project_id: projectId, workflow_key: workflowKey, name,
    ...(actor ? { actor } : {}),
  });
}

export function rotateWebhook(projectId: string, webhookId: string, actor?: string) {
  return write<WebhookTriggerWithSecret>({
    action: "webhook_rotate", webhook_id: webhookId, project_id: projectId, ...(actor ? { actor } : {}),
  });
}

export function setWebhookEnabled(projectId: string, webhookId: string, enabled: boolean, actor?: string) {
  return write<WebhookTrigger>({
    action: "webhook_enabled", webhook_id: webhookId, project_id: projectId, enabled,
    ...(actor ? { actor } : {}),
  });
}
