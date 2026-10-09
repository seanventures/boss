"use server";

/**
 * App ▸ Links presses (APP_TEMPLATES_AND_SECURITY_LOOP.md §5.5): link this app to another of the
 * project, revoke a link, reveal the provider's generated test users (D9, step-up, audited).
 * The API decides every call; these only carry the press.
 */
import { API_URL, authHeaders } from "../../lib/client";
import { failResult, okResult, type ActionResult } from "../../lib/action-result";
import { apiWrite } from "../../lib/api-write";
import { declareAction } from "../../lib/identity/guarded-action";
import { act, apiFetch, refusalFrom } from "../../lib/server-action";

function field(form: FormData, key: string): string {
  const v = form.get(key);
  return typeof v === "string" ? v.trim() : "";
}

const app = (form: FormData) => encodeURIComponent(field(form, "app_key"));

/** A link refusal in the person's words, by its code. The API's 409 for a taken alias read as
 *  "changed meanwhile — reload" (tester P2): nothing changed, the alias is simply in use. */
function linkRefusalWords(status: number, body: { error?: unknown; detail?: unknown }, alias: string): string {
  const code = typeof body.error === "string" ? body.error : "";
  if (code === "alias_taken") return `${alias || "That alias"} is already used by another link of this app. Choose another alias.`;
  if (code === "bad_alias") return "An alias is 2 to 31 of A–Z, 0–9 and _, starting with a letter.";
  if (code === "self_link") return "An app cannot talk to itself.";
  if (code === "provider_not_in_project") return "Choose an app of this project.";
  if (code === "unknown_app") return "This app has no build yet to link from.";
  if (code === "no_gateway_port") return "No link port is free on this install. Revoke a link you no longer use.";
  const said = typeof body.detail === "string" ? body.detail : "";
  return said ? said[0].toUpperCase() + said.slice(1) : `The link was refused (HTTP ${status}).`;
}

/** POST /v1/apps/{app}/links. `new_app` names a consumer whose first build has not run yet. */
export async function createLinkAction(form: FormData): Promise<ActionResult> { return act(async () => {
  const alias = field(form, "alias").toUpperCase();
  const r = await apiWrite(`${API_URL}/v1/apps/${app(form)}/links`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({
      project_id: field(form, "project_id"), provider_app_key: field(form, "provider_app_key"),
      alias, env: field(form, "env") || "preview",
      credentials: field(form, "test_users") === "on" ? "test_users" : "none",
      ...(field(form, "new_app") ? { new_app: true } : {}),
    }),
  });
  if (!r.ok) {
    const clone = r.clone();
    const body = (await clone.json().catch(() => ({}))) as { error?: unknown; detail?: unknown };
    // Identity refusals (step-up, a missing permission) keep their own handling.
    if (r.status !== 401 && r.status !== 403) return failResult(linkRefusalWords(r.status, body, alias));
    throw await refusalFrom(r);
  }
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
