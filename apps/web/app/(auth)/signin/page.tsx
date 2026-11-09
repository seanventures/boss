import React from "react";
import { redirect } from "next/navigation";

import { safeNext } from "../../../lib/console-session";
import { ME_PATH } from "../../../lib/identity/contract";
import { serverGet } from "../../../lib/identity/server";
import type { SignInStep } from "../../../lib/identity/types";
import { loadAuthOptionsState } from "../auth-options";
import { AuthUnavailable } from "../auth-unavailable";
import { ssoErrorWords } from "../auth-flow";
import { SignIn } from "../login/sign-in";

export const dynamic = "force-dynamic";
export const metadata = { title: "Sign in, BOSS" };

type Props = { searchParams: Promise<{ step?: string; error?: string; next?: string }> };

/**
 * WHERE SINGLE SIGN-ON COMES BACK (apps/api/app/routers/identity_sso.py `_redirect`):
 *   /signin?step=mfa     , the IdP said who you are; BOSS still needs your second factor (the
 *                           pre-auth cookie carries the rest, so the code screen opens directly);
 *   /signin?error=<code> , it did not finish; say why, in words, and offer the other ways in.
 * Success goes to `/`, a link from My account to `/account?linked=1`, enrolment to `/enrol`.
 * Anything else here is the ordinary sign-in page.
 */
export default async function SsoLandingPage({ searchParams }: Props) {
  const params = await searchParams;
  const next = safeNext(params.next ?? null);
  const mfa = params.step === "mfa";
  if (!mfa && !params.error) redirect(`/login${params.next ? `?next=${encodeURIComponent(next)}` : ""}`);
  if (!mfa) {
    const me = await serverGet(ME_PATH);
    if (me.ok) redirect(next);
  }
  // Which second factors the person has is not known here (the API keeps it with the pre-auth
  // step), so every kind is offered; the API refuses a kind the person does not hold.
  const step: SignInStep | null = mfa ? { next: "mfa", methods: ["totp", "passkey", "recovery"], who: null } : null;
  const state = await loadAuthOptionsState();
  if (!state.available) return <AuthUnavailable host={state.host} />;
  return <SignIn options={state.options} next={next} initialStep={step} notice={mfa ? null : ssoErrorWords(params.error)} />;
}
