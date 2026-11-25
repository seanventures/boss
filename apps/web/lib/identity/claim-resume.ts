/**
 * A RELOAD DURING THE CLAIM (stranger test r3). The claim signs the new Owner in, then asks for a
 * second factor and shows recovery codes. Reloading /claim in between used to say "Already
 * claimed / Sign in" to the person who had just claimed it, and the enrol screen they reached
 * from there said "Continue" and landed on Projects.
 *
 * The signed-in Owner now resumes: enrolment first (their session can do nothing else), then
 * Setup, the step that comes after a claim. Anyone else, or nobody signed in, sees the page.
 */
import type { MeResponse } from "./contract";
import { enrolHref, needsEnrolment } from "./enrolment";

/** Where the claim sends its new Owner when it is done (repository, model, first build). */
export const AFTER_CLAIM = "/?area=setup";

/** Where a signed-in visitor to /claim on a claimed install goes; null: show the page. */
export function claimResumeHref(me: Pick<MeResponse, "banners" | "access_roles"> | null): string | null {
  if (!me) return null;
  if (!(me.access_roles ?? []).includes("owner")) return null;
  // No second factor yet: the session can only enrol, and the codes come with the factor.
  if (needsEnrolment(me)) return enrolHref(AFTER_CLAIM);
  return AFTER_CLAIM;
}

/** The enrol screen's last button: "Continue setup" when it goes on to Setup after a claim. */
export function enrolContinueLabel(next: string): string {
  return next === AFTER_CLAIM ? "Continue setup" : "Continue";
}

/** The enrol screen's destination for an Owner adding their FIRST factor (just claimed) when
 *  nothing more specific was asked ("/" from Sign in): Setup. Null: keep what was asked. */
export function firstOwnerNext(me: Pick<MeResponse, "banners" | "access_roles"> | null, asked: string | undefined): string | null {
  if (asked && asked !== "/") return null;
  if (!me || !(me.access_roles ?? []).includes("owner") || !needsEnrolment(me)) return null;
  return AFTER_CLAIM;
}
