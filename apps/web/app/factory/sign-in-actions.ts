"use server";

/**
 * App ▸ Sign-in presses (APP_TEMPLATES_AND_SECURITY_LOOP.md §6.4): use Sign in with BOSS for an app
 * built from login-oauth, or stop using it. The API registers the client, its redirect URIs and the
 * app's OIDC_BOSS_* secrets; nothing secret ever comes back here.
 */
import { API_URL, authHeaders } from "../../lib/client";
import { okResult, type ActionResult } from "../../lib/action-result";
import { declareAction } from "../../lib/identity/guarded-action";
import { act, apiFetch } from "../../lib/server-action";

function field(form: FormData, key: string): string {
  const v = form.get(key);
  return typeof v === "string" ? v.trim() : "";
}

const path = (form: FormData) => `${API_URL}/v1/apps/${encodeURIComponent(field(form, "app_key"))}/sign-in-with-boss`;

/** POST /v1/apps/{app}/sign-in-with-boss: register (or refresh) the app's BOSS client. */
export async function useSignInWithBossAction(form: FormData): Promise<ActionResult> { return act(async () => {
  const pool = field(form, "pool_id");
  const r = await apiFetch(path(form), {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ project_id: field(form, "project_id"), ...(pool ? { pool_id: pool } : {}) }),
  });
  const body = (await r.json()) as { created?: boolean; secret_rotated?: boolean };
  const said = body.created ? "Sign in with BOSS is on" : body.secret_rotated
    ? "Sign in with BOSS is on (the client got a new secret)" : "Addresses brought up to date";
  return okResult(said, { data: body as Record<string, unknown> });
}); }

/** DELETE /v1/apps/{app}/sign-in-with-boss: the client goes, its sign-ins end, the secrets go. */
export async function stopSignInWithBossAction(form: FormData): Promise<ActionResult> { return act(async () => {
  await apiFetch(`${path(form)}?project_id=${encodeURIComponent(field(form, "project_id"))}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  return okResult("Sign in with BOSS is off for this app");
}); }

declareAction(useSignInWithBossAction, "oidc_client:manage");
declareAction(stopSignInWithBossAction, "oidc_client:manage");
