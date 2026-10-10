import React from "react";
import { redirect } from "next/navigation";

import { safeNext } from "../../../lib/console-session";
import { ME_PATH } from "../../../lib/identity/contract";
import { serverGet } from "../../../lib/identity/server";
import { loadAuthOptionsState } from "../auth-options";
import { AuthUnavailable } from "../auth-unavailable";
import { ssoErrorWords } from "../auth-flow";
import { SignIn } from "./sign-in";

export const dynamic = "force-dynamic";
export const metadata = { title: "Sign in · BOSS" };

type Props = { searchParams: Promise<{ next?: string; error?: string }> };

/** Sign in (mockup e). Already signed in: straight on to where you were going. */
export default async function LoginPage({ searchParams }: Props) {
  const params = await searchParams;
  const next = safeNext(params.next ?? null);
  const me = await serverGet(ME_PATH);
  if (me.ok) redirect(next);
  const state = await loadAuthOptionsState();
  if (!state.available) return <AuthUnavailable host={state.host} />;
  return <SignIn options={state.options} next={next} notice={ssoErrorWords(params.error)} />;
}
