import React from "react";
import { redirect } from "next/navigation";

import { ME_PATH, type MeResponse } from "../../../lib/identity/contract";
import { serverGet } from "../../../lib/identity/server";
import { loadAuthOptionsState } from "../auth-options";
import { AuthUnavailable } from "../auth-unavailable";
import { claimResumeHref } from "../../../lib/identity/claim-resume";
import { ClaimFlow } from "./claim";

export const dynamic = "force-dynamic";
export const metadata = { title: "Claim this BOSS" };

/** Claim this BOSS: the first Owner, on the setup-link, legacy-password or network-bridge path.
 *  Once claimed, the Owner who is signed in (a reload during the second-factor step) resumes
 *  their enrolment, or goes on to Setup, instead of reading "Already claimed" (stranger test r3). */
export default async function ClaimPage() {
  const state = await loadAuthOptionsState();
  if (!state.available) return <AuthUnavailable host={state.host} />;
  if (!state.options.claim) {
    const me = await serverGet<MeResponse>(ME_PATH);
    const resume = claimResumeHref(me.ok ? me.data : null);
    if (resume) redirect(resume);
  }
  return <ClaimFlow options={state.options} />;
}
