/**
 * AN ENROLMENT-ONLY SESSION (the API's H1 fix). A person whose role requires a second factor and
 * who has none signs in to a session that can do ONE thing: enrol. The API answers
 * `403 {"error":"mfa_enrolment_required"}` to every route except reading who they are
 * (GET /v1/identity/me, /me/permissions, /me/factors), TOTP enrolment, passkey registration and
 * sign-out, and /v1/identity/me carries the banner `mfa_enrolment_required`.
 *
 * The console sends such a person to /enrol instead of rendering pages whose every read is
 * refused. If enrolment itself is refused (`403 enrolment_grant_required`: they already had a
 * factor, or the window closed), only an admin resetting their second factor helps, and the
 * enrol screen says exactly that.
 */
import type { MeResponse } from "./contract";

export const ENROLMENT_REQUIRED = "mfa_enrolment_required";
export const ENROLMENT_GRANT_REQUIRED = "enrolment_grant_required";

export const ENROLMENT_GRANT_WORDS =
  "You can't add a second factor from this session. Ask an admin to reset your second factor, then sign in again.";

/** Must this signed-in person enrol before anything else? (The me banner, or an API refusal code.) */
export function needsEnrolment(me: Pick<MeResponse, "banners"> | null | undefined, problem?: string | null): boolean {
  if (problem === ENROLMENT_REQUIRED) return true;
  return Boolean(me?.banners?.some((b) => b.kind === ENROLMENT_REQUIRED));
}

/** /enrol, coming back to `next` (a same-origin path) once a factor is added. */
export function enrolHref(next: string | null | undefined): string {
  const back = next && next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/enrol") ? next : "/";
  return `/enrol?next=${encodeURIComponent(back)}`;
}

/**
 * In the browser: any identity call refused `mfa_enrolment_required` takes the person to enrol.
 * Never from the enrol screen itself (its own calls are the allowed ones), and never twice.
 */
export function sendToEnrolment(code: string, loc: Pick<Location, "pathname" | "search" | "assign"> | null =
  typeof window === "undefined" ? null : window.location): boolean {
  if (code !== ENROLMENT_REQUIRED || !loc) return false;
  if (loc.pathname.startsWith("/enrol") || loc.pathname.startsWith("/login") || loc.pathname.startsWith("/signin")) return false;
  loc.assign(enrolHref(`${loc.pathname}${loc.search}`));
  return true;
}

/**
 * Where a sign-in answer of `next: "enrol"` goes. With `enrolment_grant: false` this sign-in may
 * not add a factor (403 enrolment_grant_required if it tried): it lands on the sign-in page with
 * that reason in words, never on an enrol screen whose every step would be refused.
 */
export function afterEnrolStep(step: { enrolment_grant?: boolean }, next: string): string {
  if (step.enrolment_grant === false) return `/signin?error=${ENROLMENT_GRANT_REQUIRED}`;
  return enrolHref(next);
}
