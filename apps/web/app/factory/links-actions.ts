"use server";

/**
 * App ▸ Links presses (APP_TEMPLATES_AND_SECURITY_LOOP.md §5.5): link this app to another of the
 * project, revoke a link, reveal the provider's generated test users (D9, step-up, audited).
 * The API decides every call; these only carry the press.
 */
import { API_URL, authHeaders } from "../../lib/client";
import { okResult, type ActionResult } from "../../lib/action-result";
import { declareAction } from "../../lib/identity/guarded-action";
import { act, apiFetch } from "../../lib/server-action";

function field(form: FormData, key: string): string {
  const v = form.get(key);
  return typeof v === "string" ? v.trim() : "";
}

const app = (form: FormData) => encodeURIComponent(field(form, "app_key"));

/** POST /v1/apps/{app}/links */
export async function createLinkAction(form: FormData): Promise<ActionResult> { return act(async () => {
  const r = await apiFetch(`${API_URL}/v1/apps/${app(form)}/links`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({
      project_id: field(form, "project_id"), provider_app_key: field(form, "provider_app_key"),
      alias: field(form, "alias").toUpperCase(), env: field(form, "env") || "preview",
      credentials: field(form, "test_users") === "on" ? "test_users" : "none",
    }),
  });
  const link = (await r.json()) as { alias?: string };
  return okResult(`Linked as ${link.alias ?? "a new alias"}`);
}); }

/** DELETE /v1/apps/{app}/links/{id}: the gateway stops forwarding within seconds. */
export async function revokeLinkAction(form: FormData): Promise<ActionResult> { return act(async () => {
  await apiFetch(`${API_URL}/v1/apps/${app(form)}/links/${encodeURIComponent(field(form, "link_id"))}`
    + `?project_id=${encodeURIComponent(field(form, "project_id"))}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  return okResult("Link revoked");
}); }

/** POST /v1/apps/{provider}/test-users/reveal (D9): the values, once, for this person. */
export async function revealTestUsersAction(form: FormData): Promise<ActionResult> { return act(async () => {
  const r = await apiFetch(`${API_URL}/v1/apps/${app(form)}/test-users/reveal`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ project_id: field(form, "project_id"), env: field(form, "env") || "preview" }),
  });
  const body = (await r.json()) as { users?: { username: string; password: string; role: string }[] };
  return okResult(undefined, { data: { users: body.users ?? [] } });
}); }

declareAction(createLinkAction, "app_link:manage");
declareAction(revokeLinkAction, "app_link:manage");
declareAction(revealTestUsersAction, "app_secret:write");
