import React from "react";

import { loadAuthOptionsState } from "../auth-options";
import { AuthUnavailable } from "../auth-unavailable";
import { ClaimFlow } from "./claim";

export const dynamic = "force-dynamic";
export const metadata = { title: "Claim this BOSS" };

/** Claim this BOSS: the first Owner, on the setup-link, legacy-password or network-bridge path. */
export default async function ClaimPage() {
  const state = await loadAuthOptionsState();
  if (!state.available) return <AuthUnavailable host={state.host} />;
  return <ClaimFlow options={state.options} />;
}
