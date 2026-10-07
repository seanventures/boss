/**
 * LINK A SINGLE SIGN-ON ACCOUNT FROM MY ACCOUNT (apps/api/app/routers/identity_sso.py sso_start).
 *
 * GET /v1/auth/sso/{provider}/start?purpose=link answers a 302 to the identity provider — but only
 * for a fresh session (`401 step_up_required` otherwise) and never for a SAML provider. A browser
 * navigation would show that refusal as raw JSON, so the start is first PROBED without following
 * the redirect: a redirect means "go", a refusal is thrown as an IdentityError (so the step-up sheet
 * can open and the probe be retried), and only then does the page navigate to the same URL.
 */
import { IdentityError } from "./api";

export function ssoLinkStartPath(providerId: string): string {
  return `/v1/auth/sso/${encodeURIComponent(providerId)}/start?purpose=link`;
}

/** Resolves when the API would send this browser on to the provider; throws its refusal otherwise. */
export async function probeSsoLink(providerId: string, fetcher: typeof fetch = fetch): Promise<void> {
  let res: Response;
  try {
    res = await fetcher(ssoLinkStartPath(providerId), { redirect: "manual", credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } });
  } catch {
    throw new IdentityError(0, "identity_unavailable");
  }
  if (res.type === "opaqueredirect" || (res.status >= 300 && res.status < 400)) return;
  let body: Record<string, unknown> = {};
  try { body = (await res.json()) as Record<string, unknown>; } catch { body = {}; }
  const code = typeof body.error === "string" ? body.error : res.ok ? "sso_failed" : "error";
  throw new IdentityError(res.ok ? 409 : res.status, code, { body });
}
