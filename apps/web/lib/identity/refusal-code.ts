/**
 * THE CODE OF AN API REFUSAL, read the same way by the browser (lib/identity/api.ts) and the
 * server (lib/identity/server.ts serverGet). The identity routes answer `{"error": "<code>"}`; a
 * refusal from before identity runs — the API's transport check answering
 * `401 {"detail": "Valid BOSS API key or OIDC bearer token required."}` — carries only a sentence.
 * A 401 with no code IS "unauthenticated": the visitor has no credential the API honours, so the
 * console sends them to sign in (round 3, W1: a network-mode console after Require sign-in showed
 * the org picker, because the sentence was taken for the code and matched nothing).
 */
export function refusalCode(status: number, data: unknown): string {
  const d = (data && typeof data === "object" ? data : {}) as { error?: unknown; detail?: unknown };
  if (typeof d.error === "string" && d.error) return d.error;
  if (status === 401) return "unauthenticated";
  if (typeof d.detail === "string" && d.detail) return d.detail;
  return status === 429 ? "rate_limited" : "error";
}
